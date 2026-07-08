# Real-time TUI-fidelity activity transcript

Status: approved (design phase) — 2026-07-08

## Problem

The dashboard's "Activity Feed" panel is neither real-time nor TUI-fidelity, despite recent commit history (`0ada8f8`, `77a2b58`, `1c09d01`) aiming at exactly that:

- **Not push, just fast polling.** `DashboardSnapshot.activity` is wired into the WebSocket broadcast plumbing (`src/models.py`) but `Collector.snapshot()` never populates it — dead code. The frontend instead runs a dedicated `setInterval(..., 1000)` hitting `GET /api/agent/activity`, independent of (and duplicating) the existing WS-primary/REST-fallback pattern every other panel uses.
- **Content is a thin summary, not a transcript.** `runner.py::_emit_tool_activity` skips all read-only tools (`read`, `grep`, `glob`, `list`), truncates shell commands to 100 chars, and never captures tool *output* (stdout, diffs) — only input. `text` events (the agent's reasoning) are truncated to 200 chars. None of this resembles what `opencode`'s own TUI shows a human watching the session live.
- **Not project-scoped.** `activity_log` has no `project_id` column (unlike `cycles`, which does). If two projects' loops ever produce activity close in time, switching the dashboard's active project doesn't guarantee you only see that project's events.

Solo Agent's whole premise is a 24/7 unattended loop — the activity feed is the primary way a human checks in on what the agent is actually doing without opening a terminal. It needs to read like watching `opencode` directly.

## Non-goals

- No change to the orchestrator loop logic itself (reflect/plan/execute/verify/record), guardrails, or backlog mechanics. Scoped strictly to activity capture, transport, and display.
- No durable storage of full tool output/diffs. Rich transcript detail is live-only (in-memory); only the existing thin curated summary persists to SQLite.
- No attempt to reproduce token-by-token streaming of assistant text — OpenCode's `text` events arrive as complete chunks, not deltas (per existing schema comments in `runner.py` and `tests/conftest.py`), so there is nothing to stream at sub-message granularity.

## Design

### 1. Two-track data model

**Track A — thin, durable (unchanged behavior, now project-scoped).** The existing `activity_log` table and `_emit_tool_activity` curation logic (skip read-only tools, truncate to ~100-200 chars) stay exactly as they are today. This remains the source of truth for history/reload and the WS-down fallback. Only change: add a `project_id` column, thread it through `ActivityEvent`, `insert_activity()`, and filter `GET /api/agent/activity` to the active project.

**Track B — rich, in-memory-only (new).** A new `TranscriptEvent` model captures what Track A discards:

```python
class TranscriptEvent(BaseModel):
    id: str                      # correlation id if the source event had one, else a generated uuid
    kind: Literal["tool", "text", "session_start", "session_end"]
    status: Literal["running", "completed", "error"] = "completed"
    tool: Optional[str] = None            # e.g. "bash", "edit", "read"
    readonly: bool = False                # read/grep/glob/list — renders compact, no expand
    input: Optional[str] = None           # full command / file path / pattern, capped
    output: Optional[str] = None          # stdout / diff / result, capped, best-effort
    text: Optional[str] = None            # full assistant reasoning text, capped
    title: Optional[str] = None           # OpenCode's human-readable summary, if present
    cycle: Optional[int] = None
    task: Optional[str] = None            # backlog task text this session is executing
    session_id: str                       # groups events under one run_goal() call
    timestamp: datetime
```

Field caps: `input`/`output`/`text` truncated at 8 KB each (append `"… [truncated]"`), so one giant bash dump or huge diff can't blow up memory or a WS frame.

Lives in a bounded ring buffer (`collections.deque(maxlen=400)`) in a new module, `src/orchestrator/transcript.py`:

```python
def record(event: TranscriptEvent) -> None: ...   # append or update-by-id, then broadcast
def snapshot() -> list[TranscriptEvent]: ...       # current buffer, for WS-connect backfill
def clear() -> None: ...                           # called on project switch
```

Not persisted — a server restart loses it (thin history remains in SQLite, which is fine: restarts are rare and the loop resumes cleanly from `orch_state` regardless).

### 2. Capture layer (`runner.py`)

`_process_live_event` (the per-line-as-it-arrives path — this is what makes it genuinely real-time, not `_parse_event_stream` which only runs after the process exits) gets a second emitter alongside the existing thin one:

- **Every `tool_use` event**, regardless of tool or status, is considered (today: only `completed` + a fixed tool allowlist).
- **Correlation id**, checked defensively across candidate field names since the exact OpenCode schema for this isn't confirmed from a captured trace: `part.callID`, `part.id`, `state.callID`, `state.id`. First one found wins.
  - **If found**: a non-`completed` status (`pending`/`running`/anything else) records a `status="running"` entry keyed by that id. A later `completed`/`error` event with the same id **updates that entry in place** (same `id`, new status/output) rather than appending a new one.
  - **If not found**: skip the running phase entirely — behave exactly like today, emit only on `completed`. This is a silent, safe degradation, not a broken half-rendered card.
- **Output/diff extraction** (new — today only `state.input` is read), tried in order, first non-empty wins: `state.output`, `state.metadata.diff`, `state.metadata.patch`, `state.metadata.stdout`, `part.output`. Dict/list values are JSON-stringified. If nothing is found, `output` stays `None` (card just shows input) and the raw keys present under `state` are logged at DEBUG once per tool name per process lifetime, so real traces can be inspected and this list tuned without guesswork.
- **Every `text` event** records a `kind="text"` entry with the full untruncated (8 KB-capped) content — today's 200-char cap is Track-A-only.
- `readonly` is set for `{read, grep, glob, list, tree, search}` — used by the frontend to render compact vs expandable.

Track A's existing `_emit_tool_activity` call and Track B's new emitter both fire off the same event, independently — one writes to SQLite (curated), one calls `transcript.record()` (uncurated). Neither depends on the other; either can be disabled without touching the other's logic.

### 3. Session boundaries (`controller.py`)

Each `run_goal()` call site in `_run_cycle` (execute-path per-task calls, reflect-path call) brackets its invocation with:

```python
sid = runner.new_session_id()   # runner's existing _new_session_id() made public, reused here
transcript.record(TranscriptEvent(kind="session_start", session_id=sid, cycle=cycle, task=task.text, ...))
res = await run_goal(prompt, title=..., session_id=sid)   # threaded through so runner tags events with it
transcript.record(TranscriptEvent(kind="session_end", session_id=sid, status="completed" if res.ok else "error", ...))
```

The id has to be known *before* `run_goal` starts streaming events (not just after, from `AgentResult.session_id`), since live events need tagging as they arrive. So `run_goal` gains an optional `session_id` parameter — if provided, it's used instead of calling `_new_session_id()` internally; if omitted, existing callers (and tests) get today's behavior unchanged.

### 4. Transport (`ws.py`, `routes/ws.py`)

`/ws` currently only ever sends bare `DashboardSnapshot` JSON on the collector's 2s cadence. Transcript events need to go out the instant `transcript.record()` is called, decoupled from that cadence. New message shapes, distinguished by a `kind` field the existing snapshot messages don't have (so frontend routing is a simple `if (msg.kind) ... else handleSnapshot(msg)` and nothing about the metrics/health/slots path changes):

```jsonc
{"kind": "transcript_backfill", "events": [...]}                  // sent once, right after connect
{"kind": "transcript_event", "op": "append", "event": {...}}      // new entry
{"kind": "transcript_event", "op": "update", "event": {...}}      // running -> completed, same id
```

`ConnectionManager` gains `broadcast_json(payload: dict)` (generic — reused for both message kinds) alongside the existing `broadcast_snapshot`. `transcript.record()` calls it via `asyncio.create_task(...)` fire-and-forget (matching the existing best-effort-broadcast pattern in `collector.py::_maybe_broadcast`), since `_process_live_event`'s caller (`_read_stdout`) is itself inside a running event loop.

The frontend's always-on 1s `/api/agent/activity` poll is removed. `GET /api/agent/activity` remains, now serving as the WS-down fallback via the existing `pollAll()`/reconnect mechanism every other panel already uses — it degrades to the thin curated feed while disconnected, which is visible and honest rather than silently stale.

### 5. Project scoping

- `activity_log` gets `project_id TEXT` (idempotent migration, same pattern as the existing `cycles.project_id` migration in `db.py`).
- `insert_activity()` / `ActivityEvent` / `GET /api/agent/activity` thread `project_id` through; the GET endpoint filters to the active project by default (mirrors `fetch_cycles(project_id=...)`).
- `transcript` module's ring buffer is a single global buffer (only one project's loop runs at a time); `OrchestratorController.switch_project()` calls `transcript.clear()` alongside its existing state resets, so a project switch can't leak stale transcript.

### 6. Frontend (`static/index.html`)

The existing "Activity Feed" panel is replaced in place with the transcript view:

- Entries grouped into session cards (one per `session_start`/`session_end` pair), labeled with cycle number + task text.
- Each tool entry: monospace row, command/input on top; output/diff in a `<pre>` below, collapsed past ~15 lines with "show more"; diff lines colored by leading `+`/`-` character (plain string check, no diff library dependency).
- `readonly` tool entries render as a single compact line, no expand affordance — keeps the now-uncurated "show everything" feed scannable.
- A `status="running"` entry renders with a distinct visual marker (e.g. pulsing indicator); when the matching `update` message arrives it re-renders in place using a `Map<id, entry>` the frontend already needs to maintain for this purpose.
- `text` entries render as reasoning blocks between tool rows, full content (no 200-char cap).

## Risks / open questions

- **Tool output/diff field names are guessed, not confirmed** against a real OpenCode trace (no captured sample exists in this repo beyond the schema comments in `runner.py`/`conftest.py`, which don't cover output/diff fields at all). The defensive multi-field lookup + DEBUG-logged raw keys is the mitigation — expect to tune the candidate list after the first real 24/7 run.
- **Correlation id for running→completed is similarly unconfirmed.** Degrades safely to today's completed-only behavior if absent, per above — but if OpenCode's real stream has no such id, the "running" indicator simply won't appear for anything, which is a silent no-op rather than a failure, and worth knowing going in.
- Field caps (8 KB) and buffer size (400 events) are reasonable starting defaults, not measured against real memory/frame-size behavior — may need tuning once observed live.

## Testing

- `tests/test_runner.py`: extend the mock agent stub (`conftest.py::mock_agent_script`) with `tool_use` events carrying `state.output`/`state.metadata.diff` and a `running`→`completed` pair sharing an id, to exercise the new extraction and update-in-place paths. Add a stub variant with no correlation id to confirm safe degradation.
- New `tests/test_transcript.py`: ring buffer append/update/clear, field-size capping, `snapshot()` ordering.
- `tests/test_db.py` (or wherever activity tests live): `project_id` column migration is idempotent on an existing DB; `fetch_activity`/`insert_activity` round-trip with project scoping.
- `tests/test_ws.py` or equivalent: `/ws` sends `transcript_backfill` on connect; `ConnectionManager.broadcast_json` fans out to multiple clients.
