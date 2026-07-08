"""State file watcher — polls STATE_DIR for changes and re-syncs the DB.

Uses simple mtime polling rather than inotify so it works identically in
containers and across mounted volumes (where inotify events may not propagate).

Triggers:
  - directives.md changed -> directives.sync_to_db() + broadcast
  - (tasks/journal/plan changes are read on demand by the API; no DB sync needed)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .config import settings
from .directives import directives_path, sync_to_db
from .models import DashboardSnapshot
from .ws import manager

log = logging.getLogger("solo.watcher")

POLL_INTERVAL = 5.0  # seconds


class StateWatcher:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_directives_mtime: float = 0.0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="state-watcher")
        log.info("state watcher started (watching %s)", settings.project_path)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._check_once()
            except Exception as e:
                log.exception("watcher iteration failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def _check_once(self) -> None:
        p = directives_path()
        if not p.exists():
            return
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return
        if mtime != self._last_directives_mtime:
            self._last_directives_mtime = mtime
            try:
                sync_to_db()
                log.info("directives.md changed; synced to DB")
            except Exception as e:
                log.warning("directives sync failed: %s", e)


watcher = StateWatcher()
