"""In-memory ring buffer for rich, TUI-fidelity agent activity (Track B).

This is the live, uncurated counterpart to the thin activity_log persisted
in db.py (Track A). It is NOT persisted -- a server restart loses it. Only
one project's orchestrator loop ever runs at a time, so this is a single
global buffer rather than one per project; OrchestratorController.switch_project
calls clear() on every switch so stale transcript can't leak across projects.

See docs/superpowers/specs/2026-07-08-realtime-tui-transcript-design.md.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Awaitable, Callable, Optional

from .models import TranscriptEvent

log = logging.getLogger("solo.transcript")

MAX_EVENTS = 400

BroadcastFn = Callable[[dict], Awaitable[None]]

_buffer: "deque[TranscriptEvent]" = deque(maxlen=MAX_EVENTS)
_broadcast: Optional[BroadcastFn] = None


def set_broadcast(fn: Optional[BroadcastFn]) -> None:
    """Wire (or clear) the WS fan-out callback. Set once at app startup in
    main.py, the same pattern as Collector's broadcast wiring."""
    global _broadcast
    _broadcast = fn


async def record(event: TranscriptEvent, op: str = "append") -> None:
    """Append a new event, or update an existing one in place by id.

    op="update" looks for a buffered entry with the same event.id (the
    running -> completed transition on a correlated tool call) and replaces
    it. If none is found -- never seen, or evicted by the ring buffer --
    falls back to a plain append so nothing is silently dropped.
    """
    if op == "update":
        for i, existing in enumerate(_buffer):
            if existing.id == event.id:
                _buffer[i] = event
                await _broadcast_event(event, "update")
                return
        op = "append"
    _buffer.append(event)
    await _broadcast_event(event, "append")


async def _broadcast_event(event: TranscriptEvent, op: str) -> None:
    if _broadcast is None:
        return
    try:
        await _broadcast(
            {"kind": "transcript_event", "op": op, "event": event.model_dump(mode="json")}
        )
    except Exception as e:
        log.debug("transcript broadcast failed: %s", e)


def snapshot() -> list[TranscriptEvent]:
    """Current buffer contents, oldest first -- used for the WS-connect backfill."""
    return list(_buffer)


def clear() -> None:
    _buffer.clear()
