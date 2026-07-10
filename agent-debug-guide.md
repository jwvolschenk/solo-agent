# Agent Debug Guide — Solo Agent Orchestrator

Use this guide when investigating a stuck, failed, or unexpected orchestrator
session. It tells you **where to look**, **how sources correlate**, and **what
each log event means**.

---

## What you are debugging

Solo Agent runs three subsystems in one process:

| Subsystem | What it does | Your focus when debugging |
|---|---|---|
| **Orchestrator** (`src/orchestrator/`) | Ralph loop: reflect → plan → execute → verify → record | **Start here** — state machine, cycle decisions, git revert |
| **Agent runner** (`src/orchestrator/runner.py`) | Spawns OpenCode (or `AGENT_COMMAND`) as a subprocess | Agent stdout/stderr, timeouts, JSON event stream |
| **Dashboard / API** | Observes and commands the loop | Live state, cycle history, activity feed |

The orchestrator is the **control plane**. OpenCode is the **worker**. Failures
can originate in either layer — use the trace log to see which.

---

## Triage checklist (do this first)

1. **Get live state**
   ```bash
   curl -s http://localhost:8090/api/orchestrator/state | jq
   ```
   Note: `phase`, `running`, `cycle_number`, `current_task`, `last_error`,
   `last_outcome`, `agent_session_id`, `project_path`.

2. **Read the trace log for that cycle**
   ```bash
   # Default path (local dev)
   tail -200 data/orch-trace.log

   # Filter to a specific cycle (replace N)
   grep 'cycle=N' data/orch-trace.log

   # Failures only
   grep -E 'ok=false|event=guardrail|event=cycle_crashed|VERIFY FAIL' data/orch-trace.log
   ```

3. **Pull cycle history from SQLite (via API)**
   ```bash
   curl -s 'http://localhost:8090/api/orchestrator/cycles?limit=10' | jq
   ```
   Each row has `outcome`, `error`, `summary`, `agent_session_id`, `snapshot_sha`,
   `head_sha`, `lines_changed`, `tokens_used`.

4. **Correlate the session ID** — every agent invocation gets a `session=solo-…`
   in the trace log. Match it to:
   - `agent_session_id` in `/api/orchestrator/state` or cycle rows
   - The dashboard **transcript** panel (tool calls + reasoning for that session)
   - `GET /api/agent/activity?limit=100` (thin curated feed: file edits, shell)

5. **Read project artifacts** in `PROJECT_PATH` (not the solo-agent repo unless
   that *is* the target):
   - `reflections.md` — what the loop learned recently
   - `backlog.md` — pending executor tasks (`- [ ]` / `- [x]`)
   - `backlog-candidates.md` — planner inbox (reflect output)
   - `SOLO_AGENT.md` — protocol the worker reads each session

6. **Check git state** in `PROJECT_PATH`:
   ```bash
   git -C "$PROJECT_PATH" status
   git -C "$PROJECT_PATH" log --oneline -5
   ```
   On verify/agent failure the orchestrator **reverts** to the cycle's
   `snapshot_sha`. Unexpected missing changes often mean a revert happened.

---

## Primary data sources

### 1. Orchestrator trace log (`data/orch-trace.log`)

Structured `event=… key=value` lines from logger `solo.orch`. This is the
**best handoff artifact** — copy the relevant cycle's lines to another agent.

| Setting | Default | Purpose |
|---|---|---|
| `ORCH_TRACE_FILE` | `./data/orch-trace.log` | Trace file path |
| `LOG_LEVEL` | `INFO` | Set `DEBUG` for stdout/stderr snippets on failures |

Rotates at 5 MB (3 backups). In Docker, `./data` is mounted at `/data`.

**Context fields** appear on most lines when known:

| Field | Meaning |
|---|---|
| `cycle` | Ralph loop iteration number |
| `phase` | Current state-machine phase (see below) |
| `project` | Active project id |
| `task` | Backlog task text or `reflect` / `plan` |
| `session` | Agent session id (`solo-…`) |

### 2. HTTP API

| Endpoint | Use when |
|---|---|
| `GET /api/orchestrator/state` | Live phase, errors, stall counters |
| `GET /api/orchestrator/cycles` | Historical outcomes per cycle |
| `GET /api/config` | `project_path`, `verify_command`, goal |
| `GET /api/agent/activity` | Curated tool/file events (persisted 7d) |
| `POST /api/orchestrator/stop` | Hard stop (kill switch) |

### 3. SQLite (`data/solo-agent.db`)

| Table | Contents |
|---|---|
| `orch_state` | Persisted controller state per project (survives restart) |
| `cycles` | One row per cycle: outcome, SHAs, tokens, summary, session id |
| `activity_log` | Thin activity feed (file edits, shell commands) |

Direct query example:
```bash
sqlite3 data/solo-agent.db \
  "SELECT cycle_number, outcome, error, summary, agent_session_id
   FROM cycles ORDER BY cycle_number DESC LIMIT 5;"
```

### 4. Rich transcript (in-memory, live only)

Full tool calls + reasoning text. **Lost on server restart.**

- Dashboard transcript panel
- WebSocket `/ws` → `kind: transcript_backfill` on connect
- Not written to disk — use trace log + activity log for persistence

### 5. Project artifacts (`PROJECT_PATH`)

Written by the orchestrator and the worker agent:

| File | Written by | Read when |
|---|---|---|
| `backlog.md` | PLAN / EXECUTE | Why a task ran or what's queued next |
| `backlog-candidates.md` | REFLECT | Whether reflect found work |
| `reflections.md` | Orchestrator | Recent cycle summaries |
| `backlog-history/` | Orchestrator | Archived completed tasks |

---

## State machine phases

```
idle → execute → verify → record → idle     (when backlog has pending tasks)
idle → reflect → plan → record → idle       (when backlog is clear)
```

| Phase | Meaning | Stuck here? |
|---|---|---|
| `idle` | Between cycles, or waiting to start | Normal breather (`inter_cycle_delay_sec`) |
| `execute` | Running OpenCode on a backlog task | Check agent timeout, OpenCode permissions |
| `verify` | Running `VERIFY_COMMAND` | Check test output in trace (`verify_stdout`) |
| `record` | Writing cycle row + reflection | Should be brief |
| `reflect` | Agent surveying for new work | Agent may be slow or hung |
| `plan` | Agent decomposing candidates → backlog | Check `backlog-candidates.md` |
| `paused` | Auto-pause (stall) or manual pause | Read `last_error` — often stall counters |
| `stopped` | Kill switch or manual stop | Expected |
| `error` | Unhandled exception in controller | Read `last_error` + Python traceback in process logs |

**Paused with stall message** — guardrail tripped after **3 cycles with no completed task**
and fewer than `STALL_MIN_LINES_CHANGED` (default 5) lines changed:

```
stalled: N low-change cycles, M fail cycles
```

Successful execute cycles **do not** count toward low-change stall — checking off
`backlog.md` (2-line diffs) is normal. Stall only fires when tasks fail to
complete. Counters reset when you **Start** or **Resume** the loop.

The loop needs human intervention before resume only when stall was real (failures).

---

## Trace event catalog

Grep for `event=<name>` in `data/orch-trace.log`.

| Event | Level | Meaning |
|---|---|---|
| `trace_ready` | INFO | Trace file initialized |
| `lifecycle_start` | INFO | Loop started |
| `lifecycle_pause` / `lifecycle_stop` / `lifecycle_resume` | INFO | User or API lifecycle |
| `lifecycle_switch_project` | INFO | Active project changed |
| `resume_state` | INFO | State restored from SQLite on boot |
| `phase_transition` | INFO | `from_phase` → `to_phase` (+ `reason`) |
| `cycle_start` | INFO | New cycle: `path=execute\|reflect`, `pending_tasks`, `snapshot_sha` |
| `execute_task` | INFO | Which backlog item is running |
| `agent_result` | INFO/WARN | Subprocess finished: `ok`, `error`, `tokens`, `session` |
| `verify_result` | INFO/WARN | Test gate: `returncode`; failures include output snippets at DEBUG |
| `verify_revert` | WARN | Tests failed → git revert |
| `git_revert` / `git_commit` | INFO | Git operations |
| `execute_failed` | WARN | Agent failed before verify |
| `reflect_failed` / `plan_failed` | WARN | Planning phase agent error |
| `reflect_path` | INFO | Backlog was empty → reflect/plan cycle |
| `backlog_archived` | INFO | Completed tasks moved to history |
| `reflect_fallback_seed` | INFO | Reflect found nothing; orchestrator seeded a candidate |
| `cycle_end` | INFO | Cycle finished: `outcome`, `lines`, `tokens`, `tasks_passed` |
| `guardrail` | WARN | Circuit breaker: `kind=kill_switch\|stall\|loop\|no_progress` |
| `cycle_crashed` | ERROR | Unhandled exception in controller |

**DEBUG-only snippets** (set `LOG_LEVEL=DEBUG`):
- `agent_stdout` / `agent_stderr` — on agent failure or timeout
- `verify_stdout` / `verify_stderr` — on verify failure

---

## Common failure patterns

### Agent failed (`agent_result ok=false`)

1. Grep trace for that `session=…`
2. At DEBUG, read `agent_stderr` / `agent_stdout` snippets
3. **`Separator is found, but chunk is longer than limit`** — OpenCode emitted a
   single NDJSON line larger than asyncio's stream buffer. Fixed in runner via
   `AGENT_STDOUT_LINE_LIMIT` (default 4 MiB). Restart the server after updating.
4. Check OpenCode permissions in `PROJECT_PATH/.opencode/opencode.json`:
   - `doom_loop: deny` and `question: deny` required for unattended runs
4. Check `per_goal_timeout_sec` (default 30 min) — `timed_out=true` in trace
5. OpenCode exit code is **unreliable** — success is determined from JSON
   events (`step_finish` with `reason=stop`), not `$?`

### Verify failed (`verify_result ok=false`)

1. Trace shows `verify_revert` → changes were **discarded**
2. Run the verify command manually:
   ```bash
   cd "$PROJECT_PATH" && eval "$VERIFY_COMMAND"
   ```
3. `VERIFY_COMMAND` comes from `/api/config` or project settings

### Stuck in `paused` with stall error

- `consecutive_low_change_cycles` — agent ran but changed < `stall_min_lines_changed` lines
- `consecutive_fail_cycles` — multiple execute cycles with 0 tasks passed
- Resume only after addressing root cause: `POST /api/orchestrator/resume`

### Kill switch engaged

- `event=guardrail kind=kill_switch`
- Triggered by `POST /api/orchestrator/stop` or internal stop
- Loop stops at next check; may finish current subprocess first

### Cycle path confusion

- **`path=execute`** in `cycle_start` → backlog had pending `- [ ]` tasks
- **`path=reflect`** → backlog empty; archiving + reflect/plan cycle
- One task per execute cycle — remaining backlog items wait

### Missing transcript after restart

Transcript is in-memory only. Use:
- `data/orch-trace.log` (persistent)
- `cycles` table / `/api/orchestrator/cycles`
- `activity_log` / `/api/agent/activity`

---

## Useful grep recipes

```bash
# Full story for cycle 12
grep 'cycle=12' data/orch-trace.log

# All phase transitions
grep 'event=phase_transition' data/orch-trace.log

# Agent failures
grep 'event=agent_result' data/orch-trace.log | grep 'ok=false'

# Verify failures
grep 'event=verify_result' data/orch-trace.log | grep 'ok=false'

# Guardrail trips
grep 'event=guardrail' data/orch-trace.log

# Follow live
tail -f data/orch-trace.log | grep --line-buffered solo.orch
```

---

## Handoff bundle (give this to another agent)

When escalating, include:

1. **Cycle number** and **session id** (`solo-…`)
2. **Grep output**: `grep 'cycle=N' data/orch-trace.log`
3. **`/api/orchestrator/state`** JSON snapshot
4. **Relevant cycle row** from `/api/orchestrator/cycles`
5. **`reflections.md`** excerpt for that cycle (in `PROJECT_PATH`)
6. **`backlog.md`** current state
7. **Git log**: `git -C "$PROJECT_PATH" log --oneline -3`
8. **Config**: `curl -s http://localhost:8090/api/config | jq`

Optional at DEBUG level: re-run with `LOG_LEVEL=DEBUG`, reproduce once, and
attach the new trace lines with stdout/stderr snippets.

---

## Key source files (for code investigation)

| File | Responsibility |
|---|---|
| `src/orchestrator/controller.py` | State machine, cycle routing, pause/stop |
| `src/orchestrator/trace.py` | Structured trace logging |
| `src/orchestrator/runner.py` | Agent subprocess, JSON event parsing |
| `src/orchestrator/verify.py` | External test gate |
| `src/orchestrator/git_ops.py` | Snapshot, revert, commit |
| `src/orchestrator/guardrails.py` | Kill switch, stall, loop detection |
| `src/orchestrator/artifacts.py` | backlog.md, reflections.md, SOLO_AGENT.md |
| `src/db.py` | SQLite persistence |
| `src/transcript.py` | In-memory rich transcript (live only) |

---

## Environment reference

| Variable | Default | Debug relevance |
|---|---|---|
| `PROJECT_PATH` | repo root | Where git + artifacts live |
| `VERIFY_COMMAND` | *(empty)* | Test gate; empty = no orchestrator verify phase |
| `AGENT_COMMAND` | `opencode run …` | Subprocess template |
| `PER_GOAL_TIMEOUT_SEC` | `1800` | Agent hang limit |
| `STALL_DETECTION_CYCLES` | `3` | Cycles before auto-pause |
| `STALL_MIN_LINES_CHANGED` | `5` | Low-change threshold |
| `LOG_LEVEL` | `INFO` | `DEBUG` for failure snippets |
| `ORCH_TRACE_FILE` | `data/orch-trace.log` | Trace output path |
| `DB_PATH` | `data/solo-agent.db` | SQLite location |

---

## Quick recovery commands

```bash
# Stop the loop immediately
curl -X POST http://localhost:8090/api/orchestrator/stop

# Pause gracefully (finishes current cycle phase)
curl -X POST http://localhost:8090/api/orchestrator/pause

# Resume after fixing stall
curl -X POST http://localhost:8090/api/orchestrator/resume

# Finish current cycle then stop (soft stop)
curl -X POST http://localhost:8090/api/orchestrator/stop-after-cycle
```
