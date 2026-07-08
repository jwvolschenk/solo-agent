"""Metrics collector — async background task polling llama-server every 2s.

Design:
  - One httpx.AsyncClient reused across polls.
  - On any network error or timeout, marks the server offline and keeps the
    last-known snapshot (never crashes the dashboard).
  - Stores successful snapshots to SQLite (ring buffer) and broadcasts a
    DashboardSnapshot over the WebSocket via the provided callback.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import httpx

from .config import settings
from .db import insert_metrics
from .models import DashboardSnapshot, HealthState, MetricsSnapshot, SlotInfo
from .parsers import parse_health, parse_prometheus, parse_slots

log = logging.getLogger("solo.collector")

# Type of the broadcast callback: ws.ConnectionManager.broadcast_snapshot
BroadcastFn = Callable[[DashboardSnapshot], Awaitable[None]]


class Collector:
    """Polls llama-server and holds the latest snapshot in memory."""

    def __init__(self, broadcast: Optional[BroadcastFn] = None) -> None:
        self._broadcast = broadcast
        self._client: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

        # latest in-memory state (served by /api/* without hitting the DB)
        self.health: HealthState = HealthState(status="offline", message="not started")
        self.metrics: Optional[MetricsSnapshot] = None
        self.slots: list[SlotInfo] = []

    # ---- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._client = httpx.AsyncClient(timeout=settings.http_timeout, headers=_auth_headers())
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="collector")
        log.info("collector started (poll=%.1fs, target=%s)", settings.poll_interval, settings.llama_server_url)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        log.info("collector stopped")

    # ---- main loop -----------------------------------------------------------

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.collect_once()
            except Exception as e:  # never let the loop die
                log.exception("collector iteration failed: %s", e)
                self.health = HealthState(status="offline", message=f"loop error: {e}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.poll_interval)
            except asyncio.TimeoutError:
                pass  # interval elapsed, poll again

    async def collect_once(self) -> None:
        """Fetch all four endpoints. Update in-memory state + broadcast."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=settings.http_timeout, headers=_auth_headers())

        base = settings.llama_server_url.rstrip("/")
        # Probe health first — if unreachable, short-circuit to offline.
        try:
            h_resp = await self._client.get(f"{base}/health")
            self.health = parse_health(h_resp.status_code, _maybe_json(h_resp))
        except (httpx.HTTPError, OSError) as e:
            self.health = HealthState(status="offline", http_status=0, message=str(e))
            await self._maybe_broadcast()
            return

        # Parallel fetch of the rest. Each is independent and best-effort.
        results = await asyncio.gather(
            self._fetch_text("/metrics"),
            self._fetch_json("/slots"),
            self._fetch_json("/props"),
            return_exceptions=True,
        )
        metrics_text, slots_json, _props_json = results

        if isinstance(metrics_text, str):
            try:
                snap = parse_prometheus(metrics_text)
                self.metrics = snap
                # persist only when we actually got metrics
                try:
                    insert_metrics(snap)
                except Exception as e:
                    log.warning("metrics insert failed: %s", e)
            except Exception as e:
                log.warning("metrics parse failed: %s", e)
        else:
            # keep previous metrics snapshot; don't blank it out on a blip
            log.debug("metrics fetch failed: %s", metrics_text)

        if isinstance(slots_json, (list, dict)):
            try:
                self.slots = parse_slots(slots_json)
            except Exception as e:
                log.warning("slots parse failed: %s", e)
        elif isinstance(slots_json, Exception):
            log.debug("slots fetch failed: %s", slots_json)

        # (props fetched lazily by /api/props, not held in memory — it rarely changes)

        await self._maybe_broadcast()

    # ---- helpers -------------------------------------------------------------

    async def _fetch_text(self, path: str) -> str | Exception:
        assert self._client is not None
        try:
            r = await self._client.get(f"{settings.llama_server_url.rstrip('/')}{path}")
            r.raise_for_status()
            return r.text
        except (httpx.HTTPError, OSError) as e:
            return e

    async def _fetch_json(self, path: str):
        assert self._client is not None
        try:
            r = await self._client.get(f"{settings.llama_server_url.rstrip('/')}{path}")
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, OSError, ValueError) as e:
            return e

    async def _maybe_broadcast(self) -> None:
        if self._broadcast is None:
            return
        try:
            await self._broadcast(self.snapshot())
        except Exception as e:
            log.debug("broadcast failed: %s", e)

    def snapshot(self) -> DashboardSnapshot:
        """Build a partial DashboardSnapshot with just the monitor data."""
        return DashboardSnapshot(
            health=self.health,
            metrics=self.metrics,
            slots=self.slots,
        )


# Module-level singleton. main.py wires its broadcast callback before starting.
collector = Collector()


def _maybe_json(resp: httpx.Response):
    """Decode a response body as JSON if possible, else return the text."""
    try:
        return resp.json()
    except (ValueError, httpx.DecodingError):
        return resp.text


def _auth_headers() -> dict[str, str]:
    """Build the Authorization header if an API key is configured.

    llama-server started with --api-key requires a Bearer token on every
    endpoint except /health. Without this, /metrics, /slots, /props all 401.
    """
    if settings.llama_api_key:
        return {"Authorization": f"Bearer {settings.llama_api_key}"}
    return {}
