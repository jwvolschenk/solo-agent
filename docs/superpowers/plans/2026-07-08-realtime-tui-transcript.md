# Real-time TUI-fidelity Activity Transcript Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the dashboard's "Activity Feed" into a real-time, TUI-fidelity transcript — full tool input/output/diffs and untruncated reasoning text, pushed over the WebSocket the instant OpenCode emits it, grouped by session, correctly scoped per project.

**Architecture:** Two parallel tracks off the same live NDJSON stream from OpenCode's stdout. Track A (existing, unchanged behavior) is the thin curated summary persisted to `activity_log` in SQLite — now project-scoped. Track B (new) is a rich, uncurated, in-memory-only ring buffer (`TranscriptEvent`s) pushed over `/ws` the moment each event is captured, independent of the collector's 2s poll cadence. The frontend renders Track B primary (session-grouped cards, tool input+output, live running→completed updates) and falls back to Track A's thin one-liners when the WebSocket is down.

**Tech Stack:** FastAPI, Pydantic, SQLite (stdlib `sqlite3`), vanilla JS (no build step, single `index.html`), pytest + pytest-asyncio.

## Global Constraints

- Rich transcript fields (`input`/`output`/`text`) are capped at 8192 chars each — append `"… [truncated]"` if cut.
- The rich ring buffer holds at most 400 `TranscriptEvent`s (`transcript.MAX_EVENTS`) — oldest evicted first.
- Track B is never persisted to disk — a server restart loses it; Track A (SQLite) is unaffected and remains the durable history.
- Only one project's orchestrator loop runs at a time — the rich transcript is a single global buffer, cleared on project switch.
- Tool output/diff field names in OpenCode's real event schema are **not confirmed** from a captured trace — extraction is defensive (tries several candidate field names) and must degrade gracefully (no output shown, not a crash) when none match.
- Correlating a tool call's running→completed transition requires a per-call id in the event stream, which is also unconfirmed — degrade gracefully to completed-only display when no id is found, never render a broken half-updated card.

---

### Task 1: Project-scope the thin activity log

**Files:**
- Modify: `src/db.py:43-50` (activity_log schema), `src/db.py:114-116` (migrations), `src/db.py:234-266` (insert_activity/fetch_activity)
- Modify: `src/models.py:96-103` (ActivityEvent)
- Modify: `src/routes/agent.py` (GET/POST /api/agent/activity)
- Modify: `src/orchestrator/controller.py:156` (activity hook registration)
- Test: `tests/test_routes.py`

**Interfaces:**
- Produces: `fetch_activity(limit: int = 50, project_id: str = "") -> list[sqlite3.Row]` — empty string means unfiltered (all projects).
- Produces: `ActivityEvent.project_id: Optional[str]` field.
- Produces: `GET /api/agent/activity` defaults to the active project (via `db.get_active_project()`) when `project_id` isn't passed explicitly.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_routes.py`:

```python
def test_activity_scoped_to_project(client, tmp_settings):
    from datetime import datetime
    from src.db import insert_project, set_active_project

    now = datetime.utcnow().isoformat()
    for pid, name in (("proj-a", "A"), ("proj-b", "B")):
        insert_project({
            "id": pid, "name": name, "goal": "", "project_path": str(tmp_settings.project_path),
            "verify_command": "", "work_branch": "solo-agent/auto", "stop_after_cycle": 0,
            "created_at": now, "updated_at": now,
        })

    set_active_project("proj-a")
    r = client.post("/api/agent/activity", json={"type": "file", "message": "edited a.py"})
    assert r.status_code == 201

    set_active_project("proj-b")
    r2 = client.post("/api/agent/activity", json={"type": "file", "message": "edited b.py"})
    assert r2.status_code == 201

    # active project is proj-b -- default GET should only see b's event
    got = client.get("/api/agent/activity").json()["events"]
    messages = {e["message"] for e in got}
    assert "edited b.py" in messages
    assert "edited a.py" not in messages

    # explicit project_id still reachable
    got_a = client.get("/api/agent/activity?project_id=proj-a").json()["events"]
    assert any(e["message"] == "edited a.py" for e in got_a)


def test_activity_log_schema_migration_is_idempotent(tmp_settings):
    """The project_id column migration must not raise on a DB that already has it."""
    from src.db import init_db

    init_db()
    init_db()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest tests/test_routes.py -k activity_scoped_to_project -v`
Expected: FAIL — `project_id` query filter has no effect yet (both events visible in every query), or a `TypeError`/`KeyError` if the response shape doesn't include `project_id`.

- [ ] **Step 3: Add `project_id` to the schema, migration, and DB functions**

In `src/db.py`, update the `activity_log` table in `SCHEMA` (around line 43-50):

```python
CREATE TABLE IF NOT EXISTS activity_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT NOT NULL,
    type         TEXT NOT NULL,
    message      TEXT NOT NULL,
    metadata_json TEXT,
    project_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(timestamp DESC);
```

Add to `_MIGRATIONS` (around line 114-116):

```python
_MIGRATIONS = [
    "ALTER TABLE cycles ADD COLUMN project_id TEXT",
    "ALTER TABLE activity_log ADD COLUMN project_id TEXT",
]
```

Replace `insert_activity` (around line 234-249):

```python
def insert_activity(event) -> int:
    import json

    with write_conn() as c:
        cur = c.execute(
            "INSERT INTO activity_log (timestamp, type, message, metadata_json, project_id) VALUES (?,?,?,?,?)",
            (
                event.timestamp.isoformat(),
                event.type,
                event.message,
                json.dumps(event.metadata),
                event.project_id,
            ),
        )
        _prune_activity(c)
        c.commit()
        return int(cur.lastrowid)
```

Replace `fetch_activity` (around line 263-266):

```python
def fetch_activity(limit: int = 50, project_id: str = "") -> list[sqlite3.Row]:
    if project_id:
        return query_all(
            "SELECT * FROM activity_log WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        )
    return query_all(
        "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
    )
```

- [ ] **Step 4: Add `project_id` to `ActivityEvent`**

In `src/models.py`, replace the `ActivityEvent` class (around line 96-103):

```python
class ActivityEvent(BaseModel):
    """An event posted by (or observed about) the running agent."""

    type: Literal["task", "tool", "file", "error", "system"] = "system"
    message: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)
    id: Optional[int] = None  # DB row id when read back
    project_id: Optional[str] = None
```

- [ ] **Step 5: Scope the routes to the active project**

Replace `src/routes/agent.py` in full:

```python
"""GET/POST /api/agent/activity — agent activity feed."""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..models import ActivityEvent
from ..db import fetch_activity, get_active_project, insert_activity

api_router = APIRouter(prefix="/api/agent", tags=["agent"])


@api_router.get("/activity")
async def get_activity(limit: int = Query(50, ge=1, le=500), project_id: Optional[str] = None) -> dict:
    """Recent agent activity entries, newest first. Defaults to the active
    project; pass project_id explicitly to look at a different one, or an
    empty active project (no project set yet) sees everything unfiltered."""
    pid = project_id if project_id is not None else (get_active_project() or "")
    rows = fetch_activity(limit=limit, project_id=pid)
    events = []
    for r in rows:
        try:
            meta = json.loads(r["metadata_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        events.append(
            {
                "id": r["id"],
                "type": r["type"],
                "message": r["message"],
                "timestamp": r["timestamp"],
                "metadata": meta,
                "project_id": r["project_id"],
            }
        )
    return {"count": len(events), "events": events}


@api_router.post("/activity", status_code=201)
async def post_activity(event: ActivityEvent) -> dict:
    """Agent (or anything) posts an activity event. Returns the stored id."""
    if not event.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    if event.project_id is None:
        event.project_id = get_active_project()
    row_id = insert_activity(event)
    return {"status": "ok", "id": row_id}
```

- [ ] **Step 6: Stamp the orchestrator's own activity hook with the active project**

In `src/orchestrator/controller.py`, replace line 156:

```python
        set_activity_hook(lambda t, m, meta: insert_activity(ActivityEvent(type=t, message=m, metadata=meta)))  # type: ignore[arg-type]
```

with:

```python
        set_activity_hook(
            lambda t, m, meta: insert_activity(
                ActivityEvent(type=t, message=m, metadata=meta, project_id=self.active_project_id)
            )
        )  # type: ignore[arg-type]
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest tests/test_routes.py -v`
Expected: PASS (all activity tests, including the two new ones)

- [ ] **Step 8: Run the full suite to check for regressions**

Run: `./venv/bin/python -m pytest -q`
Expected: PASS, no failures introduced

- [ ] **Step 9: Commit**

```bash
git add src/db.py src/models.py src/routes/agent.py src/orchestrator/controller.py tests/test_routes.py
git commit -m "feat: scope agent activity log to the active project"
```

---

### Task 2: Rich transcript model + in-memory ring buffer

**Files:**
- Modify: `src/models.py` (add `TranscriptEvent`)
- Create: `src/transcript.py`
- Test: `tests/test_transcript.py`

**Interfaces:**
- Produces: `TranscriptEvent` pydantic model (see below) in `src/models.py`.
- Produces (`src/transcript.py`):
  - `MAX_EVENTS: int = 400`
  - `set_broadcast(fn: Optional[Callable[[dict], Awaitable[None]]]) -> None`
  - `async def record(event: TranscriptEvent, op: str = "append") -> None`
  - `def snapshot() -> list[TranscriptEvent]`
  - `def clear() -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transcript.py`:

```python
"""Transcript ring buffer tests — Track B (rich, in-memory-only) activity."""

import pytest

from src import transcript
from src.models import TranscriptEvent


@pytest.fixture(autouse=True)
def reset_transcript():
    """Isolate each test's buffer + broadcast hook, restoring whatever
    broadcast callback was registered before (e.g. by importing src.main
    in another test module) so this file can't leak state into others."""
    saved_broadcast = transcript._broadcast
    transcript.clear()
    transcript.set_broadcast(None)
    yield
    transcript.clear()
    transcript.set_broadcast(saved_broadcast)


def _event(id="e1", **kw) -> TranscriptEvent:
    defaults = dict(id=id, kind="tool", status="completed", session_id="s1")
    defaults.update(kw)
    return TranscriptEvent(**defaults)


@pytest.mark.asyncio
async def test_record_appends_and_snapshot_returns_in_order():
    await transcript.record(_event(id="e1"))
    await transcript.record(_event(id="e2"))
    ids = [e.id for e in transcript.snapshot()]
    assert ids == ["e1", "e2"]


@pytest.mark.asyncio
async def test_update_replaces_existing_entry_by_id():
    await transcript.record(_event(id="e1", status="running"), op="append")
    await transcript.record(_event(id="e1", status="completed", output="ok"), op="update")
    snap = transcript.snapshot()
    assert len(snap) == 1
    assert snap[0].status == "completed"
    assert snap[0].output == "ok"


@pytest.mark.asyncio
async def test_update_with_unknown_id_falls_back_to_append():
    await transcript.record(_event(id="never-seen", status="completed"), op="update")
    snap = transcript.snapshot()
    assert len(snap) == 1
    assert snap[0].id == "never-seen"


@pytest.mark.asyncio
async def test_buffer_is_bounded():
    for i in range(transcript.MAX_EVENTS + 50):
        await transcript.record(_event(id=f"e{i}"))
    assert len(transcript.snapshot()) == transcript.MAX_EVENTS
    # oldest events evicted -- the first surviving one is e50
    assert transcript.snapshot()[0].id == "e50"


def test_clear_empties_buffer():
    transcript._buffer.append(_event(id="e1"))
    transcript.clear()
    assert transcript.snapshot() == []


@pytest.mark.asyncio
async def test_record_broadcasts_via_registered_callback():
    received = []

    async def fake_broadcast(payload):
        received.append(payload)

    transcript.set_broadcast(fake_broadcast)
    await transcript.record(_event(id="e1"))
    assert len(received) == 1
    assert received[0]["kind"] == "transcript_event"
    assert received[0]["op"] == "append"
    assert received[0]["event"]["id"] == "e1"


@pytest.mark.asyncio
async def test_broadcast_failure_is_swallowed():
    async def bad_broadcast(payload):
        raise RuntimeError("ws gone")

    transcript.set_broadcast(bad_broadcast)
    # must not raise
    await transcript.record(_event(id="e1"))
    assert len(transcript.snapshot()) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest tests/test_transcript.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.transcript'` (and `TranscriptEvent` not found in `src.models`)

- [ ] **Step 3: Add `TranscriptEvent` to `src/models.py`**

Add directly after the `ActivityEvent` class in `src/models.py`:

```python
class TranscriptEvent(BaseModel):
    """A rich, TUI-fidelity transcript entry — Track B, live-only, never
    persisted. The uncurated counterpart to ActivityEvent (Track A, which
    stays curated/truncated and durable in activity_log).

    See docs/superpowers/specs/2026-07-08-realtime-tui-transcript-design.md.
    """

    id: str
    kind: Literal["tool", "text", "session_start", "session_end"] = "tool"
    status: Literal["running", "completed", "error"] = "completed"
    tool: Optional[str] = None
    readonly: bool = False
    input: Optional[str] = None
    output: Optional[str] = None
    text: Optional[str] = None
    title: Optional[str] = None
    cycle: Optional[int] = None
    task: Optional[str] = None
    session_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
```

- [ ] **Step 4: Create `src/transcript.py`**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest tests/test_transcript.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add src/models.py src/transcript.py tests/test_transcript.py
git commit -m "feat: add TranscriptEvent model + in-memory ring buffer for rich activity"
```

---

### Task 3: WebSocket transport for transcript events

**Files:**
- Modify: `src/ws.py`
- Modify: `src/routes/ws.py`
- Modify: `src/main.py`
- Test: `tests/test_ws.py`

**Interfaces:**
- Consumes: `transcript.snapshot() -> list[TranscriptEvent]`, `transcript.set_broadcast(fn)` from Task 2.
- Produces: `ConnectionManager.broadcast_json(payload: dict) -> None` (async).
- Produces: `/ws` sends, in order on connect: (1) the existing bare `DashboardSnapshot` JSON, (2) `{"kind": "transcript_backfill", "events": [...]}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ws.py`:

```python
"""WebSocket endpoint tests — initial snapshot + transcript backfill on connect."""

import pytest

from src import transcript
from src.models import TranscriptEvent


@pytest.fixture(autouse=True)
def reset_transcript_buffer():
    transcript.clear()
    yield
    transcript.clear()


def test_ws_sends_snapshot_then_transcript_backfill(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        # first message is the DashboardSnapshot -- has no "kind" field
        assert "kind" not in first
        second = ws.receive_json()
        assert second["kind"] == "transcript_backfill"
        assert second["events"] == []


def test_ws_backfill_reflects_current_transcript_buffer(client):
    # direct buffer append (no broadcast needed) is enough to test backfill
    transcript._buffer.append(TranscriptEvent(id="e1", kind="tool", session_id="s1"))

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # snapshot
        backfill = ws.receive_json()
        assert backfill["kind"] == "transcript_backfill"
        assert len(backfill["events"]) == 1
        assert backfill["events"][0]["id"] == "e1"
```

Note: a third test asserting a *live* `manager.broadcast_json()` call reaches a connected `TestClient` websocket was considered and deliberately left out — `TestClient.websocket_connect` runs the app in a background thread with its own event loop, and `ConnectionManager`'s `asyncio.Lock` is bound to whatever loop existed at construction time, so awaiting `broadcast_json` from the main pytest-asyncio loop risks a flaky "Future attached to a different loop" failure. The broadcast plumbing itself is already covered by `test_transcript.py::test_record_broadcasts_via_registered_callback`, which doesn't need a real socket.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest tests/test_ws.py -v`
Expected: FAIL — no second message arrives (or times out) since transcript backfill isn't sent yet.

- [ ] **Step 3: Add `broadcast_json` to `ConnectionManager`**

Replace `src/ws.py` in full:

```python
"""WebSocket ConnectionManager.

Holds the set of connected clients and broadcasts DashboardSnapshots to all of
them. Used by the collector (metrics updates), the state watcher (file changes),
and the orchestrator (phase transitions). Also fans out transcript events
(src/transcript.py) via broadcast_json, independent of the collector's poll
cadence -- transcript events are pushed the instant they happen.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket

from .models import DashboardSnapshot

log = logging.getLogger("solo.ws")


class ConnectionManager:
    """Tracks live WebSocket clients and fans out snapshots + events."""

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
        await self._broadcast_text(snapshot.model_dump_json())

    async def broadcast_json(self, payload: dict) -> None:
        """Send an arbitrary JSON-serializable payload to every connected client."""
        await self._broadcast_text(json.dumps(payload, default=str))

    async def _broadcast_text(self, payload: str) -> None:
        if not self._clients:
            return
        dead: list[WebSocket] = []
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
```

- [ ] **Step 4: Send transcript backfill on connect**

Replace `src/routes/ws.py` in full:

```python
"""WS /ws — live dashboard updates.

On connect, the client receives the current snapshot immediately, then a
backfill of the current rich transcript buffer, then gets pushed updates as
the collector / state watcher / orchestrator / transcript emit them. Falls
back gracefully: if the WS is closed, the frontend can poll the REST API.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import transcript
from ..collector import collector
from ..ws import manager

log = logging.getLogger("solo.ws.route")

api_router = APIRouter(tags=["ws"])


@api_router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    # send an immediate snapshot so the client isn't blank until the next poll
    try:
        initial = collector.snapshot()
        await ws.send_text(initial.model_dump_json())
    except Exception as e:
        log.debug("initial ws send failed: %s", e)
    # then replay whatever's currently in the rich transcript buffer
    try:
        backfill = [e.model_dump(mode="json") for e in transcript.snapshot()]
        await ws.send_json({"kind": "transcript_backfill", "events": backfill})
    except Exception as e:
        log.debug("transcript backfill send failed: %s", e)

    try:
        # We don't expect inbound messages, but we must read to detect disconnects.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("ws loop ended: %s", e)
    finally:
        await manager.disconnect(ws)
```

- [ ] **Step 5: Wire transcript's broadcast to the WS manager**

In `src/main.py`, add the import and wiring line. Change:

```python
from .collector import collector
from .config import settings
from .db import init_db
from .ws import manager
from .watcher import watcher
from .orchestrator.controller import controller
```

to:

```python
from . import transcript
from .collector import collector
from .config import settings
from .db import init_db
from .ws import manager
from .watcher import watcher
from .orchestrator.controller import controller
```

and change:

```python
# Wire the collector's broadcast to the WebSocket manager.
collector._broadcast = manager.broadcast_snapshot
```

to:

```python
# Wire the collector's broadcast to the WebSocket manager.
collector._broadcast = manager.broadcast_snapshot
# Wire the rich transcript ring buffer's broadcast to the same manager.
transcript.set_broadcast(manager.broadcast_json)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest tests/test_ws.py -v`
Expected: PASS (2 tests)

- [ ] **Step 7: Run the full suite to check for regressions**

Run: `./venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/ws.py src/routes/ws.py src/main.py tests/test_ws.py
git commit -m "feat: push transcript events over the WebSocket, backfill on connect"
```

---

### Task 4: Rich capture in the runner + fix double-emission/double-token-count bug

**Files:**
- Modify: `src/orchestrator/runner.py`
- Modify: `tests/conftest.py` (`mock_agent_script` fixture — add a `tool` mode)
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `transcript.record(event, op="append")` from Task 2.
- Produces: `runner.new_session_id() -> str` (renamed from private `_new_session_id`).
- Produces: `run_goal(prompt, *, timeout=None, title=..., session_id: Optional[str] = None) -> AgentResult` — new optional `session_id` param, used by Task 5.
- Note: while reading this code closely to add the above, a pre-existing bug was found — `_parse_event_stream` (the post-hoc full-buffer parse run after the process exits) re-emits the exact same thin activity events that `_process_live_event` already emitted live, per line, as the process ran. It also causes `run_goal` to call `budget.budget.add(result.tokens_used)` a second time on top of the live per-`step_finish` adds inside `_process_live_event` — so every goal's token usage was counted twice. Both are fixed here since they're in the exact functions this task rewrites, and the double-activity symptom is very likely why `renderActivity` in the frontend carries a "deduplicate consecutive identical messages" workaround today.

- [ ] **Step 1: Add a `tool` mode to the mock agent stub**

In `tests/conftest.py`, inside `mock_agent_script`'s embedded script (the `textwrap.dedent(...)` string, after the `if mode == "error":` block and before the `# ok:` comment), add a new branch. The full script body becomes:

```python
    script = tmp_path / "mock_agent.py"
    script.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import os, sys, time, json
        mode = os.environ.get("AGENT_MODE", "ok")
        if mode == "hang":
            time.sleep(10000)
            sys.exit(0)
        if mode == "error":
            print(json.dumps({"type": "error", "part": {"error": {"message": "boom"}}}))
            sys.exit(0)
        if mode == "tool":
            # a correlated bash call: running -> completed with output
            print(json.dumps({
                "type": "tool_use",
                "part": {"tool": "bash", "callID": "call-1",
                         "state": {"status": "running", "input": {"command": "npm test"}}}
            }))
            print(json.dumps({
                "type": "tool_use",
                "part": {"tool": "bash", "callID": "call-1",
                         "state": {"status": "completed", "input": {"command": "npm test"},
                                   "output": "5 passed"}}
            }))
            # a read-only call with no callID -- completed straight away
            print(json.dumps({
                "type": "tool_use",
                "part": {"tool": "read", "state": {"status": "completed",
                                                     "input": {"filePath": "README.md"}}}
            }))
            print(json.dumps({"type": "text", "part": {"type": "text", "text": "DONE: ran tests"}}))
            print(json.dumps({"type": "step_finish", "part": {"reason": "stop", "tokens": {"total": 80, "input": 50, "output": 30}}}))
            sys.exit(0)
        # ok: emit assistant text + a clean stop step_finish with token counts
        print(json.dumps({"type": "text", "part": {"type": "text", "text": "DONE: added a test"}}))
        print(json.dumps({"type": "step_finish", "part": {"reason": "stop", "tokens": {"total": 150, "input": 100, "output": 50}}}))
        sys.exit(0)
        """))
    script.chmod(0o755)
    return script
```

Update the fixture's docstring line listing modes to add `tool`.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_runner.py`:

```python
@pytest.mark.asyncio
async def test_runner_does_not_double_count_tokens(monkeypatch):
    """Regression: tokens used to be counted twice -- once live per step_finish,
    once again from the post-hoc full-buffer parse."""
    monkeypatch.setenv("AGENT_MODE", "ok")
    from src.orchestrator import runner
    from src.orchestrator import budget as budgetmod
    budgetmod.budget.cycle_tokens = 0

    result = await runner.run_goal("do something")
    assert result.tokens_used == 150
    assert budgetmod.budget.cycle_tokens == 150  # not 300


@pytest.mark.asyncio
async def test_runner_does_not_double_emit_thin_activity(monkeypatch):
    """Regression: activity used to be emitted once live and once again from
    the post-hoc full-buffer parse."""
    monkeypatch.setenv("AGENT_MODE", "tool")
    from src.orchestrator import runner

    seen = []
    runner.set_activity_hook(lambda t, m, meta: seen.append(m))
    try:
        await runner.run_goal("do something")
    finally:
        runner.set_activity_hook(None)

    bash_msgs = [m for m in seen if m.startswith("$ npm test")]
    assert len(bash_msgs) == 1


@pytest.mark.asyncio
async def test_runner_emits_running_then_completed_transcript_for_correlated_tool(monkeypatch):
    """A tool_use event with a callID gets a 'running' entry, upgraded in
    place to 'completed' with output once the matching completed event
    arrives -- not two separate entries."""
    monkeypatch.setenv("AGENT_MODE", "tool")
    from src.orchestrator import runner
    from src import transcript

    transcript.clear()
    result = await runner.run_goal("do something")
    assert result.ok is True

    snap = transcript.snapshot()
    tool_entries = [e for e in snap if e.id == "call-1"]
    assert len(tool_entries) == 1  # updated in place, not appended twice
    assert tool_entries[0].status == "completed"
    assert tool_entries[0].output == "5 passed"


@pytest.mark.asyncio
async def test_runner_records_readonly_tool_without_correlation_id(monkeypatch):
    """A completed tool_use with no callID still gets recorded -- no running
    phase, straight to completed -- graceful degradation, not a dropped event."""
    monkeypatch.setenv("AGENT_MODE", "tool")
    from src.orchestrator import runner
    from src import transcript

    transcript.clear()
    await runner.run_goal("do something")

    snap = transcript.snapshot()
    read_entries = [e for e in snap if e.tool == "read"]
    assert len(read_entries) == 1
    assert read_entries[0].readonly is True
    assert read_entries[0].status == "completed"


@pytest.mark.asyncio
async def test_runner_records_full_text_event_uncapped_by_thin_limit(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "tool")
    from src.orchestrator import runner
    from src import transcript

    transcript.clear()
    await runner.run_goal("do something")
    text_entries = [e for e in transcript.snapshot() if e.kind == "text"]
    assert any(e.text == "DONE: ran tests" for e in text_entries)


@pytest.mark.asyncio
async def test_runner_tags_transcript_events_with_provided_session_id(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "tool")
    from src.orchestrator import runner
    from src import transcript

    transcript.clear()
    await runner.run_goal("do something", session_id="fixed-session-123")
    snap = transcript.snapshot()
    assert snap
    assert all(e.session_id == "fixed-session-123" for e in snap)


def test_new_session_id_is_public_and_prefixed():
    from src.orchestrator import runner
    assert runner.new_session_id().startswith("solo-")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest tests/test_runner.py -v`
Expected: FAIL — `AGENT_MODE=tool` doesn't exist yet in some tests (that part now passes after Step 1), but `transcript` has no entries (nothing wired), `run_goal` doesn't accept `session_id`, `runner.new_session_id` doesn't exist, and the token/activity double-count regressions still reproduce.

- [ ] **Step 4: Rewrite `src/orchestrator/runner.py`**

Update the imports at the top (replace the existing `from __future__ import annotations` block through `log = logging.getLogger(...)`):

```python
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

from ..config import settings
from ..models import TranscriptEvent
from .. import transcript
from . import budget

log = logging.getLogger("solo.runner")
```

Rename `_new_session_id` to `new_session_id` (make it public — used by the controller in Task 5 to generate the id *before* `run_goal` starts, so live events can be tagged as they arrive):

```python
def new_session_id() -> str:
    """Generate a stable session id. Used to correlate {session} template
    substitution AND to group this goal's transcript events (see transcript.py)
    under one session card in the dashboard."""
    return f"solo-{uuid.uuid4().hex[:12]}"
```

Update `run_goal`'s signature and the two lines that used the old name:

```python
async def run_goal(
    prompt: str,
    *,
    timeout: Optional[float] = None,
    title: str = "solo-agent goal",
    session_id: Optional[str] = None,
) -> AgentResult:
    """Run one agent goal to completion (or timeout). Returns an AgentResult.

    Spawns the agent as a subprocess in its own process group so we can kill
    the whole tree on timeout (opencode may spawn children).

    Reads stdout LINE BY LINE as it arrives — not buffered until exit — so
    activity events flow to the dashboard in real time while the agent works.

    session_id, if provided, tags every live transcript event this goal
    produces (see transcript.py) so the dashboard can group them under one
    session card. If omitted, a fresh one is generated (existing callers/tests
    are unaffected).
    """
    timeout = timeout or settings.per_goal_timeout_sec
    session_id = session_id or new_session_id()
    argv = build_command(prompt, session_id, title)
    log.info("spawning agent: %s", _redact(argv))
```

Update the `_read_stdout` closure's call site (it already has `session_id` in scope via closure):

```python
    async def _read_stdout() -> None:
        """Read stdout line-by-line, processing each JSON event as it arrives."""
        nonlocal stopped
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break  # EOF — process closed stdout
            decoded = line.decode("utf-8", errors="replace").rstrip()
            stdout_lines.append(decoded)
            # process the event immediately for live activity
            ev_stopped = await _process_live_event(decoded, session_id)
            if ev_stopped:
                stopped = True
```

Remove the double token-count. Find:

```python
    result.events, result.final_message, result.tokens_used, parse_stopped = _parse_event_stream(result.stdout)
    if parse_stopped:
        stopped = True
    budget.budget.add(result.tokens_used)
```

Replace with:

```python
    result.events, result.final_message, result.tokens_used, parse_stopped = _parse_event_stream(result.stdout)
    if parse_stopped:
        stopped = True
    # NOTE: tokens are already counted live, per step_finish event, inside
    # _process_live_event -- do NOT budget.budget.add(result.tokens_used) here.
    # That used to double-count every goal's usage (see test_runner_does_not_double_count_tokens).
```

Replace `_process_live_event` and add `_handle_tool_use_event` (this replaces the old `_process_live_event` function body entirely):

```python
async def _process_live_event(line: str, session_id: str) -> bool:
    """Process a single NDJSON line as it arrives from the agent's stdout.

    Drives BOTH activity tracks in real time as data arrives (not after the
    process exits):
      - Track A (thin, durable): _emit_tool_activity -> db.insert_activity via
        the activity hook, unchanged curated behavior (skips read-only tools,
        truncates).
      - Track B (rich, live-only): every tool call (any tool, any status) and
        every full-text reasoning message -> transcript.record().

    Returns True if this event signals completion (step_finish, reason=stop).

    This is the ONLY place activity/transcript events are emitted from the
    live stream. _parse_event_stream (run on the full buffer after the process
    exits) only computes the final AgentResult summary and must not re-emit —
    it used to, which double-logged every tool call/message and double-counted
    tokens (see _parse_event_stream's docstring).
    """
    line = line.strip()
    if not line or not line.startswith("{"):
        return False
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False

    etype = event.get("type", "")
    part = event.get("part") or {}

    if etype == "tool_use":
        await _handle_tool_use_event(part, session_id)

    # emit activity for assistant text messages (the agent's reasoning/output)
    if etype == "text":
        text = part.get("text") or ""
        if isinstance(text, str) and text.strip():
            if _activity_hook is not None:
                _activity_hook("system", text.strip()[:200], {})
            await transcript.record(
                TranscriptEvent(
                    id=str(uuid.uuid4()),
                    kind="text",
                    status="completed",
                    text=_cap(text.strip()),
                    session_id=session_id,
                )
            )

    # count tokens as they come in
    if etype == "step_finish":
        tokens = part.get("tokens") or {}
        budget.budget.add(budget.extract_tokens_from_event({"usage": tokens}))
        if part.get("reason") == "stop":
            return True

    return False


_READONLY_TOOLS = {"read", "grep", "glob", "list", "tree", "search"}


async def _handle_tool_use_event(part: dict, session_id: str) -> None:
    """Fires on every tool_use event, any tool, any status.

    Track A (thin) only fires for completed, allowlisted tools — unchanged
    _emit_tool_activity behavior. Track B (rich) records every tool call: a
    "running" entry when a correlation id is available and the tool isn't
    already completed, upgraded in place to "completed"/"error" once it is.
    If no correlation id can be found, the running phase is skipped entirely
    and only the completed entry is recorded — same trigger point as before,
    just with fuller detail.
    """
    tool = part.get("tool", "unknown")
    state = part.get("state") or {}
    status_raw = state.get("status") or ""
    inp = state.get("input") or part.get("input") or {}
    title = state.get("title") or ""

    if status_raw == "completed" and _activity_hook is not None:
        _emit_tool_activity(tool, inp, title)

    if status_raw == "completed":
        status: Literal["running", "completed", "error"] = (
            "error" if state.get("error") else "completed"
        )
    elif status_raw:
        status = "running"
    else:
        return  # no status at all -- nothing meaningful to record yet

    call_id = _extract_call_id(part, state)
    if status == "running" and call_id is None:
        return  # can't correlate a later completion back to this entry

    output = None if status == "running" else _extract_tool_output(state, part)
    await transcript.record(
        TranscriptEvent(
            id=call_id or str(uuid.uuid4()),
            kind="tool",
            status=status,
            tool=tool,
            readonly=tool in _READONLY_TOOLS,
            input=_cap(_stringify(inp)),
            output=_cap(output) if output else None,
            title=title or None,
            session_id=session_id,
        ),
        op="append" if status == "running" else "update",
    )


def _extract_call_id(part: dict, state: dict) -> Optional[str]:
    """Best-effort correlation id so a later completed event can update the
    same transcript entry a running event created. Field name isn't confirmed
    against a real OpenCode trace -- checks several plausible candidates and
    returns None (safe degradation) if none match."""
    for src, key in ((part, "callID"), (part, "id"), (state, "callID"), (state, "id")):
        val = src.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _extract_tool_output(state: dict, part: dict) -> Optional[str]:
    """Best-effort tool output/diff extraction. Field name isn't confirmed
    against a real OpenCode trace -- tries several plausible candidates in
    order and logs at DEBUG (cheap, no-op when disabled) if none match, so
    real traces can be inspected and this list tuned later."""
    metadata = state.get("metadata") or {}
    candidates = [
        state.get("output"), metadata.get("diff"), metadata.get("patch"),
        metadata.get("stdout"), part.get("output"),
    ]
    for c in candidates:
        if c:
            return _stringify(c)
    log.debug("no output field found for tool_use, state keys=%s", sorted(state.keys()))
    return None


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


def _cap(text: Optional[str], limit: int = 8192) -> Optional[str]:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + "… [truncated]"
```

Replace `_parse_event_stream` to remove the duplicate activity emission (delete the `# emit activity events for notable tool calls...` block entirely — everything else stays the same):

```python
def _parse_event_stream(stdout: str) -> tuple[list[dict], str, int, bool]:
    """Parse newline-delimited JSON events from the FULL accumulated stdout,
    once the process has exited, to compute the final AgentResult summary
    (events/final_message/tokens_used/stopped).

    This does NOT emit activity or transcript events — that already happened
    live, per-line, in _process_live_event as the process ran. Re-emitting
    here used to double-log every tool call/message and double-count tokens
    (this function's total_tokens used to also get added to the budget a
    second time in run_goal, on top of the live per-step_finish adds) — fixed
    by making this purely a read-only summary pass.

    OpenCode's actual event schema (verified against v1.17.x):
      {"type": "text",        "part": {"type":"text", "text": "<msg>"}}    — assistant message
      {"type": "step_finish", "part": {"reason":"stop"|"tool-calls"|..., "tokens": {"total":N,"input":N,"output":N}}}
      {"type": "tool_use",    "part": {"tool":"<name>", "state":{"status":"completed",...}}}
      {"type": "error",       "part": {"error":{"message":"..."}}} or {"message":"..."}
    Clean completion = a step_finish with part.reason == "stop".
    """
    events: list[dict] = []
    final_message = ""
    total_tokens = 0
    stopped = False

    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(event)

        etype = event.get("type", "")
        part = event.get("part") or {}

        # token accounting — step_finish carries cumulative-ish per-step tokens
        if etype == "step_finish":
            tokens = part.get("tokens") or {}
            total_tokens += budget.extract_tokens_from_event({"usage": tokens})
            if part.get("reason") == "stop":
                stopped = True

        # capture the latest assistant text message
        if etype == "text":
            text = part.get("text") or event.get("text")
            if isinstance(text, str) and text.strip():
                final_message = text.strip()

    # fallback: if no JSON events at all, treat stdout (minus noise) as the message
    if not events and stdout.strip():
        final_message = stdout.strip()[-2000:]
        stopped = True  # assume completion for non-JSON agents (e.g. a stub)

    return events, final_message, total_tokens, stopped
```

Leave `AgentResult`, `build_command`, `_kill_process_group`, `_redact`, and `_emit_tool_activity` unchanged otherwise — the only remaining call site of the old `_new_session_id` was inside `run_goal`, already updated to `new_session_id` above.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest tests/test_runner.py -v`
Expected: PASS (all tests including the 7 new ones)

- [ ] **Step 6: Run the full suite to check for regressions**

Run: `./venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/runner.py tests/conftest.py tests/test_runner.py
git commit -m "feat: rich per-tool-call transcript capture; fix double activity/token counting"
```

---

### Task 5: Session boundaries in the controller

**Files:**
- Modify: `src/orchestrator/controller.py`
- Test: `tests/test_controller.py`

**Interfaces:**
- Consumes: `runner.new_session_id()`, `run_goal(..., session_id=...)` from Task 4; `transcript.record(event)`, `transcript.clear()` from Task 2.
- Produces: every `run_goal()` call in `_run_cycle` is now bracketed by `session_start`/`session_end` `TranscriptEvent`s sharing one `session_id`.
- Produces: `switch_project()` clears the rich transcript buffer.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_controller.py`:

```python
@pytest.mark.asyncio
async def test_run_cycle_brackets_task_execution_with_transcript_session_markers(controller_with_tmp_repo, tmp_target_repo):
    """Each backlog task execution should show up in the transcript as a
    session_start/session_end pair, so the dashboard can group tool calls."""
    from src import transcript

    c = controller_with_tmp_repo
    (tmp_target_repo / "backlog.md").write_text("- [ ] do the thing\n")
    transcript.clear()

    await c._run_cycle()

    assert c.state.phase == "idle"
    snap = transcript.snapshot()
    starts = [e for e in snap if e.kind == "session_start"]
    ends = [e for e in snap if e.kind == "session_end"]
    assert len(starts) == 1
    assert len(ends) == 1
    assert starts[0].task == "do the thing"
    assert starts[0].session_id == ends[0].session_id
    assert starts[0].status == "running"
    assert ends[0].status in ("completed", "error")


@pytest.mark.asyncio
async def test_switch_project_clears_transcript(controller_with_tmp_repo, tmp_path):
    from datetime import datetime

    from src import transcript
    from src.db import insert_project
    from src.models import TranscriptEvent

    c = controller_with_tmp_repo
    transcript._buffer.append(TranscriptEvent(id="e1", kind="tool", session_id="s1"))

    other = tmp_path / "other-project"
    other.mkdir()
    now = datetime.utcnow().isoformat()
    insert_project({
        "id": "other", "name": "Other", "goal": "build x", "project_path": str(other),
        "verify_command": "", "work_branch": "solo-agent/auto", "stop_after_cycle": 0,
        "created_at": now, "updated_at": now,
    })

    await c.switch_project("other")
    assert transcript.snapshot() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest tests/test_controller.py -v`
Expected: FAIL — no `session_start`/`session_end` entries appear, and `switch_project` doesn't clear the buffer.

- [ ] **Step 3: Update imports in `src/orchestrator/controller.py`**

Change:

```python
from ..config import settings
from ..db import (
    get_active_project, get_orch_state, insert_activity, insert_cycle,
    set_orch_state, update_cycle, fetch_project, set_active_project, update_project,
)
from ..models import ActivityEvent, CycleRecord, OrchestratorState
from ..state_reader import parse_tasks
from . import artifacts, budget, guardrails, prompts
from .runner import set_activity_hook
```

to:

```python
from ..config import settings
from ..db import (
    get_active_project, get_orch_state, insert_activity, insert_cycle,
    set_orch_state, update_cycle, fetch_project, set_active_project, update_project,
)
from ..models import ActivityEvent, CycleRecord, OrchestratorState, TranscriptEvent
from ..state_reader import parse_tasks
from .. import transcript
from . import artifacts, budget, guardrails, prompts
from .runner import new_session_id, set_activity_hook
```

(the existing `from .runner import run_goal` line further down stays as-is)

- [ ] **Step 4: Clear the transcript on project switch**

In `switch_project`, find:

```python
        # reset transient state
        budget.reset_cycle()
        guardrails.loop_detector.reset()
        guardrails.no_progress.reset()
```

Replace with:

```python
        # reset transient state
        budget.reset_cycle()
        guardrails.loop_detector.reset()
        guardrails.no_progress.reset()
        transcript.clear()  # a new project's loop starts with a clean rich transcript
```

- [ ] **Step 5: Bracket the execute-path task loop with session markers**

In `_run_cycle`, find the `for task in pending[:max_tasks]:` block:

```python
            for task in pending[:max_tasks]:
                if guardrails.kill_switch.engaged:
                    break
                tasks_attempted += 1
                self.state.current_task = task.text
                self._persist()
                log.info("[cycle %d] EXECUTE: %s", cycle, task.text[:80])

                task_snap = await snapshot()
                res = await run_goal(
                    prompts.execute_prompt(cycle, task.text),
                    title=f"solo cycle {cycle} task",
                )
                self.state.cycle_tokens_used = budget.budget.cycle_tokens
                self.state.agent_session_id = res.session_id
                self._persist()

                if not res.ok:
```

Replace with:

```python
            for task in pending[:max_tasks]:
                if guardrails.kill_switch.engaged:
                    break
                tasks_attempted += 1
                self.state.current_task = task.text
                self._persist()
                log.info("[cycle %d] EXECUTE: %s", cycle, task.text[:80])

                sid = new_session_id()
                await transcript.record(TranscriptEvent(
                    id=sid, kind="session_start", status="running",
                    session_id=sid, cycle=cycle, task=task.text,
                ))
                task_snap = await snapshot()
                res = await run_goal(
                    prompts.execute_prompt(cycle, task.text),
                    title=f"solo cycle {cycle} task",
                    session_id=sid,
                )
                await transcript.record(TranscriptEvent(
                    id=f"{sid}-end", kind="session_end",
                    status="completed" if res.ok else "error",
                    session_id=sid, cycle=cycle, task=task.text,
                ))
                self.state.cycle_tokens_used = budget.budget.cycle_tokens
                self.state.agent_session_id = res.session_id
                self._persist()

                if not res.ok:
```

- [ ] **Step 6: Bracket the reflect-path call with session markers**

Find:

```python
            # Step 2: reflect + plan to find new work
            self.state.phase = "reflect"
            self._persist()
            reflect = await run_goal(
                prompts.reflect_prompt(cycle), title=f"solo cycle {cycle} reflect"
            )
            self.state.cycle_tokens_used = budget.budget.cycle_tokens
            self.state.agent_session_id = reflect.session_id
            self._persist()
```

Replace with:

```python
            # Step 2: reflect + plan to find new work
            self.state.phase = "reflect"
            self._persist()
            sid = new_session_id()
            await transcript.record(TranscriptEvent(
                id=sid, kind="session_start", status="running",
                session_id=sid, cycle=cycle, task="reflect + plan",
            ))
            reflect = await run_goal(
                prompts.reflect_prompt(cycle), title=f"solo cycle {cycle} reflect",
                session_id=sid,
            )
            await transcript.record(TranscriptEvent(
                id=f"{sid}-end", kind="session_end",
                status="completed" if reflect.ok else "error",
                session_id=sid, cycle=cycle, task="reflect + plan",
            ))
            self.state.cycle_tokens_used = budget.budget.cycle_tokens
            self.state.agent_session_id = reflect.session_id
            self._persist()
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest tests/test_controller.py -v`
Expected: PASS (all tests including the 2 new ones)

- [ ] **Step 8: Run the full suite to check for regressions**

Run: `./venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add src/orchestrator/controller.py tests/test_controller.py
git commit -m "feat: bracket each agent goal with transcript session_start/session_end markers"
```

---

### Task 6: Frontend — real-time transcript rendering

**Files:**
- Modify: `src/static/index.html`
- Test: `tests/test_routes.py` (smoke test — no JS test runner exists in this repo)

**Interfaces:**
- Consumes: WS messages `{"kind": "transcript_backfill", "events": [...]}` and `{"kind": "transcript_event", "op": "append"|"update", "event": {...}}` from Task 3; each event matches the `TranscriptEvent` shape from Task 2.
- Removes: the dedicated 1-second `/api/agent/activity` poll (replaced by WS push; the existing `pollAll()` WS-down fallback already covers the disconnected case).

- [ ] **Step 1: Write the failing smoke test**

Add to `tests/test_routes.py`:

```python
def test_dashboard_html_includes_transcript_rendering(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "renderTranscript" in r.text
    assert "transcript_backfill" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest tests/test_routes.py -k transcript_rendering -v`
Expected: FAIL — neither string exists in the served HTML yet.

- [ ] **Step 3: Route WebSocket messages by `kind`**

In `src/static/index.html`, find `connectWS`'s `ws.onmessage`:

```js
  ws.onmessage = (ev) => {
    try { handleSnapshot(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
  };
```

Replace with:

```js
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.kind === 'transcript_backfill') { handleTranscriptBackfill(msg.events || []); }
      else if (msg.kind === 'transcript_event') { handleTranscriptEvent(msg.event); }
      else { handleSnapshot(msg); }
    } catch (e) { /* ignore */ }
  };
```

- [ ] **Step 4: Add transcript state + renderer**

Find this existing block:

```js
// Track the last activity event id we've seen so we can animate new entries
let _lastActivityId = 0;
```

Replace it with the block below — it ends with those same two lines unchanged (`renderActivity` right after stays exactly as it is; it remains the WS-down fallback):

```js
// ============================================================================
// Rich transcript (Track B, WS-pushed) — TUI-fidelity session-grouped feed.
// Keyed by TranscriptEvent.id so a running -> completed update patches the
// same entry instead of appending a duplicate. Falls back to renderActivity
// (thin, REST-polled) when the WS is down -- see pollAll().
// ============================================================================
const _transcriptEntries = new Map();   // id -> TranscriptEvent
const _transcriptOrder = [];            // ids, oldest first
const _transcriptSessions = new Map();  // session_id -> {cycle, task, status}

function handleTranscriptBackfill(events) {
  _transcriptEntries.clear();
  _transcriptOrder.length = 0;
  _transcriptSessions.clear();
  events.forEach(_ingestTranscriptEvent);
  renderTranscript();
}

function handleTranscriptEvent(event) {
  _ingestTranscriptEvent(event);
  renderTranscript();
}

function _ingestTranscriptEvent(e) {
  if (e.kind === 'session_start') {
    _transcriptSessions.set(e.session_id, { cycle: e.cycle, task: e.task, status: 'running' });
  } else if (e.kind === 'session_end') {
    const s = _transcriptSessions.get(e.session_id) || {};
    s.status = e.status;
    _transcriptSessions.set(e.session_id, s);
  }
  if (!_transcriptEntries.has(e.id)) _transcriptOrder.push(e.id);
  _transcriptEntries.set(e.id, e);
  // cap client-side memory to roughly the server's ring buffer size
  while (_transcriptOrder.length > 400) {
    _transcriptEntries.delete(_transcriptOrder.shift());
  }
}

function _diffLines(text) {
  return esc(text).split('\n').map(line => {
    if (line.startsWith('+')) return `<span class="text-green-400">${line}</span>`;
    if (line.startsWith('-')) return `<span class="text-red-400">${line}</span>`;
    return line;
  }).join('\n');
}

function _renderTranscriptEntry(e) {
  if (e.kind === 'text') {
    return `<div class="pl-2 py-1 border-l-2 border-[#2a2a36] text-gray-300 whitespace-pre-wrap">${esc(e.text || '')}</div>`;
  }
  const badge = e.status === 'running' ? '<span class="pulse-dot text-yellow-400">●</span>'
    : e.status === 'error' ? '<span class="text-red-400">✗</span>'
    : '<span class="text-green-400">✓</span>';
  const header = `<div class="flex items-center gap-2 text-gray-300"><span class="mono text-purple-400">${esc(e.tool || 'tool')}</span> ${badge} <span class="mono text-[11px] text-gray-500 break-all">${esc(e.title || e.input || '')}</span></div>`;
  if (e.readonly) {
    return `<div class="log-line tool pl-2 py-1">${header}</div>`;
  }
  const body = e.output
    ? `<pre class="mono text-[11px] text-gray-400 bg-[#0d0d12] rounded p-2 mt-1 overflow-x-auto max-h-48 overflow-y-auto whitespace-pre-wrap">${_diffLines(e.output)}</pre>`
    : '';
  return `<div class="log-line ${e.status === 'error' ? 'error' : 'tool'} pl-2 py-1">${header}${body}</div>`;
}

function renderTranscript() {
  const toolAndTextIds = _transcriptOrder.filter(id => {
    const e = _transcriptEntries.get(id);
    return e.kind === 'tool' || e.kind === 'text';
  });
  if (!toolAndTextIds.length) { $('activityFeed').innerHTML = '<div class="text-gray-600">no activity yet</div>'; return; }
  // group consecutive entries by session_id into cards
  const cards = [];
  let current = null;
  for (const id of toolAndTextIds) {
    const e = _transcriptEntries.get(id);
    if (!current || current.sid !== e.session_id) {
      current = { sid: e.session_id, entries: [] };
      cards.push(current);
    }
    current.entries.push(e);
  }
  $('activityFeed').innerHTML = cards.map(card => {
    const s = _transcriptSessions.get(card.sid) || {};
    const label = s.cycle != null ? `cycle ${s.cycle} · ${esc(s.task || '')}` : esc(card.sid);
    const statusCls = s.status === 'error' ? 'text-red-400' : s.status === 'running' ? 'text-yellow-400' : 'text-green-400';
    return `<div class="border border-[#23232e] rounded mb-2">
      <div class="px-2 py-1 text-[11px] font-semibold ${statusCls} bg-[#15151d] rounded-t">${label}</div>
      <div class="p-1 space-y-0.5">${card.entries.map(_renderTranscriptEntry).join('')}</div>
    </div>`;
  }).join('');
}

// Track the last activity event id we've seen so we can animate new entries
let _lastActivityId = 0;
```

- [ ] **Step 5: Remove the now-redundant dedicated 1-second activity poll**

Find and delete this block entirely (the existing `pollAll()`, invoked whenever the WS is down, already fetches `/api/agent/activity` and calls `renderActivity` — that remains the WS-down fallback path):

```js
// dedicated fast activity poll (1s) — always runs, even when WS is connected,
// so the agent's live actions appear in real time
setInterval(async () => {
  const a = await getJSON('/api/agent/activity?limit=40');
  if (a) renderActivity(a.events || []);
}, 1000);
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest tests/test_routes.py -v`
Expected: PASS (all activity/dashboard tests including the new smoke test)

- [ ] **Step 7: Run the full suite to check for regressions**

Run: `./venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/static/index.html tests/test_routes.py
git commit -m "feat: render real-time TUI-fidelity transcript, drop redundant activity poll"
```

---

### Task 7: End-to-end manual verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full automated suite one more time**

Run: `./venv/bin/python -m pytest -q`
Expected: PASS, all tests (original + new) green.

- [ ] **Step 2: Start the app against a scratch project with a fake OpenCode-shaped agent**

```bash
mkdir -p /tmp/solo-agent-manual-check
cd /tmp/solo-agent-manual-check
git init -q -b main
git config user.name test && git config user.email test@test
echo "# scratch" > README.md && git add -A && git commit -q -m init

cat > /tmp/solo-agent-manual-check/fake-opencode.py <<'EOF'
#!/usr/bin/env python3
import json, sys, time
print(json.dumps({"type":"tool_use","part":{"tool":"bash","callID":"c1","state":{"status":"running","input":{"command":"npm test"}}}})); sys.stdout.flush()
time.sleep(2)
print(json.dumps({"type":"tool_use","part":{"tool":"bash","callID":"c1","state":{"status":"completed","input":{"command":"npm test"},"output":"5 passed, 0 failed"}}})); sys.stdout.flush()
print(json.dumps({"type":"tool_use","part":{"tool":"read","state":{"status":"completed","input":{"filePath":"README.md"}}}})); sys.stdout.flush()
print(json.dumps({"type":"text","part":{"type":"text","text":"DONE: verified the scratch repo"}})); sys.stdout.flush()
print(json.dumps({"type":"step_finish","part":{"reason":"stop","tokens":{"total":42,"input":30,"output":12}}}))
EOF
chmod +x /tmp/solo-agent-manual-check/fake-opencode.py
```

Back in the solo-agent repo root:

```bash
AGENT_COMMAND="/tmp/solo-agent-manual-check/fake-opencode.py {prompt}" \
PROJECT_PATH=/tmp/solo-agent-manual-check \
VERIFY_COMMAND="" \
GOAL="keep the scratch repo tidy" \
./venv/bin/uvicorn src.main:app --port 8090 &
```

- [ ] **Step 3: Watch the transcript live in a browser**

Open `http://localhost:8090`, start the orchestrator loop (Start button). Confirm:
- A session card appears labeled with the cycle number and task text.
- The `bash` tool call first shows a pulsing "running" indicator, then updates **in place** (not a duplicate row) to a green checkmark with `5 passed, 0 failed` in the output block once it completes.
- The `read` tool call renders as a single compact line (no expandable output — it's `readonly`).
- The full `DONE: verified the scratch repo` reasoning text appears untruncated (not cut to 200 chars).
- Disconnect the network tab (or stop/restart the server) and confirm the panel falls back to the thin one-liner feed, then recovers to the rich transcript on reconnect.

- [ ] **Step 4: Stop the manual server and clean up**

```bash
kill %1
rm -rf /tmp/solo-agent-manual-check
```

- [ ] **Step 5: Final full-suite confirmation**

Run: `./venv/bin/python -m pytest -q`
Expected: PASS

No commit for this task — it's verification only, not code changes.
