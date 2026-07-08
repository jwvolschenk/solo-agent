"""In-memory ring buffer for rich, TUI-fidelity agent activity (Track B).

This is the live, uncurated counterpart to the thin activity_log persisted
in db.py (Track A). It is NOT persisted -- a server restart loses it. Only
one project's orchestrator loop ever runs at a time, so this is a single
global buffer rather than one per project; OrchestratorController.switch_project
calls clear() on every switch so stale transcript can't leak across projects.

See docs/superpowers/specs/2026-07-08-realtime-tui-transcript-design.md.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Awaitable, Callable, Optional

from .models import TranscriptEvent

log = logging.getLogger("solo.transcript")

MAX_EVENTS = 400

BroadcastFn = Callable[[dict], Awaitable[None]]

_buffer: "deque[TranscriptEvent]" = deque(maxlen=MAX_EVENTS)
_broadcast: Optional[BroadcastFn] = None

# Spawned WS-broadcast tasks, retained until they finish. record() is called
# from the agent's stdout-reading loop (orchestrator/runner.py) -- awaiting a
# slow or stalled WS client's send there would block reading further
# subprocess output, which can back up the subprocess's stdout pipe and stall
# the agent itself. Firing the broadcast as a background task decouples
# delivery from capture; this set keeps each task alive until it completes (a
# detached asyncio task with no other reference can be garbage-collected
# mid-flight).
_bg_tasks: set[asyncio.Task] = set()


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

    The ring buffer itself is updated synchronously (callers and tests rely
    on snapshot() reflecting the change the instant record() returns), but
    the WS broadcast is fired via a background task -- see _spawn_broadcast.
    """
    if op == "update":
        for i, existing in enumerate(_buffer):
            if existing.id == event.id:
                _buffer[i] = event
                _spawn_broadcast(event, "update")
                return
        op = "append"
    _buffer.append(event)
    _spawn_broadcast(event, "append")


def _spawn_broadcast(event: TranscriptEvent, op: str) -> None:
    """Fire the WS broadcast without blocking the caller. See module-level
    comment on _bg_tasks for why this must not be awaited inline."""
    if _broadcast is None:
        return
    task = asyncio.create_task(_broadcast_event(event, op))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


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


async def notify_cleared() -> None:
    """Broadcast an empty transcript_backfill so already-connected dashboard
    clients reset their rendered state immediately after clear() -- used when
    the active project switches, so a client can't keep showing the previous
    project's session cards until it happens to reconnect. The frontend's
    existing handleTranscriptBackfill already clears all client-side state on
    receipt of any transcript_backfill message, empty or not."""
    if _broadcast is None:
        return
    try:
        await _broadcast({"kind": "transcript_backfill", "events": []})
    except Exception as e:
        log.debug("transcript clear-notify broadcast failed: %s", e)
