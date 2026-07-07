"""WebSocket ConnectionManager.

Holds the set of connected clients and broadcasts DashboardSnapshots to all of
them. Used by the collector (metrics updates), the state watcher (file changes),
and the orchestrator (phase transitions).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket

from .models import DashboardSnapshot

log = logging.getLogger("solo.ws")


class ConnectionManager:
    """Tracks live WebSocket clients and fans out snapshots."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        log.info("ws client connected (%d total)", len(self._clients))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
        log.info("ws client disconnected (%d total)", len(self._clients))

    async def broadcast_snapshot(self, snapshot: DashboardSnapshot) -> None:
        """Send a snapshot to every connected client. Swallows per-client errors."""
        if not self._clients:
            return
        payload = snapshot.model_dump_json()
        dead: list[WebSocket] = []
        # copy to avoid mutating during iteration
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception as e:
                log.debug("dropping ws client: %s", e)
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    def client_count(self) -> int:
        return len(self._clients)


# Singleton, wired into the FastAPI app at startup.
manager = ConnectionManager()
