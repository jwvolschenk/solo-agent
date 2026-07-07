# Solo Agent

A self-contained control plane for a **local LLM coding agent**. Three subsystems in one process:

1. **Monitoring dashboard** — passively watches a local llama-server (health, token throughput, context usage, slots) and the agent's shared state files.
2. **Directive queue** — a human → agent feedback channel. Queue guidance or direction; the agent acknowledges and completes it (full lifecycle: `pending → acknowledged → done`).
3. **Ralph orchestrator** — an autonomous self-improvement loop that drives [OpenCode](https://opencode.ai) headlessly: it reflects on the codebase, plans small verifiable tasks, executes each in a fresh context, verifies with the real test suite, and commits or reverts. Runs 24/7 with budget governors, git isolation, and circuit breakers.

> **This is not the agent.** The agent (OpenCode, Aider, custom) runs separately. Solo Agent monitors it, feeds it direction, and — if you enable the orchestrator — drives it in a continuous improvement loop.

---

## Quick start

```bash
# 1. Create a venv and install deps
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 2. Run it (llama-server expected at http://localhost:8080)
./venv/bin/uvicorn src.main:app --host 0.0.0.0 --port 8090
```

Open **http://localhost:8090**.

With Docker:

```bash
docker compose up -d --build   # dashboard at :8090
```

### What you'll see

- **Health badge** (`HEALTHY` / `OFFLINE` / `LOADING`) from llama-server `/health`.
- **Prefill & decode t/s** sparklines from `/metrics` (Prometheus, `llamacpp:` prefix).
- **Server panel** — context size, slot status, queue depth, total tokens.
- **Orchestrator console** — phase, cycle count, tokens used, current task, Start/Pause/Stop.
- **Activity feed**, **directive composer**, **journal**, **task list**, **cycle history table**.

---

## Configuration

All settings are environment variables (defaults shown):

| Variable | Default | Purpose |
|---|---|---|
| `LLAMA_SERVER_URL` | `http://localhost:8080` | llama-server base URL |
| `POLL_INTERVAL` | `2` | seconds between metric polls |
| `STATE_DIR` | `./workspace` | agent's shared state files (tasks.md, directives.md, …) |
| `DB_PATH` | `./data/solo-agent.db` | SQLite persistence |
| `TARGET_REPO` | (this repo) | repo the Ralph loop improves |
| `VERIFY_COMMAND` | `./venv/bin/python -m pytest -q` | test gate run by the orchestrator (not the agent) |
| `BASE_BRANCH` | `main` | protected branch; orchestrator never commits here directly |
| `WORK_BRANCH` | `solo-agent/auto` | where orchestrator work lands |
| `AGENT_COMMAND` | `opencode run --auto …` | agent invocation template |
| `CYCLE_TOKEN_BUDGET` | `200000` | per-cycle token ceiling |
| `DAILY_TOKEN_BUDGET` | `2000000` | per-day token ceiling |
| `AUTOSTART_ORCHESTRATOR` | `false` | auto-start the loop on boot |

---

## API reference

All routes return JSON. The dashboard polls these and also receives live updates over `/ws`.

### Monitoring

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | llama-server health (always 200; payload carries status) |
| `GET` | `/api/metrics` | current metrics snapshot |
| `GET` | `/api/metrics/history?range=1h` | historical points (`1h`/`6h`/`24h`) |
| `GET` | `/api/slots` | per-slot status |
| `GET` | `/api/props` | model info + generation params |
| `WS`  | `/ws` | live dashboard snapshots |

### Agent activity

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/agent/activity?limit=50` | recent activity events |
| `POST` | `/api/agent/activity` | post an activity event |

### Directives (human → agent)

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/agent/directives` | list all directives |
| `POST` | `/api/agent/directives` | queue a new directive `{priority, text}` |
| `GET` | `/api/agent/directives/{id}` | fetch one directive |
| `PATCH` | `/api/agent/directives/{id}` | advance status `{status: acknowledged\|done}` |

### Shared state files

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/state/tasks` | parsed `tasks.md` |
| `GET` | `/api/state/journal` | parsed `journal.md` |
| `GET` | `/api/state/plan` | raw `plan.md` |
| `GET` | `/api/state/summaries` | list context-reset summaries |

### Orchestrator

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/orchestrator/state` | phase, cycle, budget, stall counters |
| `POST` | `/api/orchestrator/start` | begin the Ralph loop |
| `POST` | `/api/orchestrator/pause` | pause between cycles |
| `POST` | `/api/orchestrator/resume` | resume from pause |
| `POST` | `/api/orchestrator/stop` | hard stop (kill switch) |
| `GET` | `/api/orchestrator/cycles` | cycle history |

---

## Agent integration contract

### Directive queue

The directive queue lives in **`<STATE_DIR>/directives.md`**, a sibling of the agent's other state files. The agent consumes it each loop:

1. **Read `directives.md`** at the start of each iteration.
2. For each directive with `status: pending`:
   - Either edit its `status:` line to `acknowledged`, then `done` when complete;
   - **Or** `PATCH /api/agent/directives/{id}` with `{"status": "acknowledged"|"done"}`.
3. The body text is your instruction. `priority: high` directives should take precedence.

File format:

```markdown
## DIRECTIVE d1  2026-07-07T14:32:00Z  priority:high
status: pending

Refactor the auth module to use JWT before touching the API routes.
```

Both channels converge: a file edit is detected by the watcher and mirrored to SQLite; an HTTP PATCH rewrites the file. Either works.

### Ralph loop (if you enable the orchestrator)

The orchestrator drives OpenCode via subprocess. It requires:

- **OpenCode** on `PATH` (or set `AGENT_COMMAND` to your agent's headless invocation).
- **A git repo** at `TARGET_REPO` with a clean working tree on `BASE_BRANCH`.
- **A verification command** (`VERIFY_COMMAND`) that exits non-zero on failure — the orchestrator runs this itself; the agent cannot self-attest completion.
- **OpenCode permissions**: for unattended runs, set `doom_loop: deny` and `question: deny` in the target's `.opencode/opencode.json` (or via `OPENCODE_PERMISSION` env), or the agent will deadlock waiting for a human.

The loop's contract with the agent:
- Each goal runs in a **fresh context** (no session bleed — this is the Ralph principle).
- The agent reads `backlog.md` and `reflections.md` (persistent memory) itself.
- The agent **never** marks tasks done or merges to `BASE_BRANCH` — only the orchestrator's verify gate does.
- On verify failure, the cycle's changes are **git-reverted** automatically.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Solo Agent (single FastAPI process, :8090)                  │
│                                                              │
│  Dashboard (SPA)  ←WS→  API routers  ←→  Background tasks    │
│   - monitor panels         /api/health...      collector     │
│   - directive composer     /api/agent/*        state watcher │
│   - orchestrator console   /api/orchestrator/* controller ◀──┼── Ralph loop
│                                                              │
│  SQLite: metrics, activity, directives, cycles, state        │
└───────────────┬──────────────────────────────────────────────┘
                │ subprocess (asyncio)
                ▼
        opencode run --auto --format json --dir <TARGET_REPO> …
                │
                ▼ verification gate (orchestrator-owned):
        <VERIFY_COMMAND> in TARGET_REPO
                │
                ▼ git ops: work branch, snapshot, pass→keep, fail→revert
```

### Orchestrator state machine

```
IDLE ──start──▶ REFLECT ──▶ PLAN ──▶ EXECUTE ──▶ VERIFY ──▶ RECORD ──┐
 ▲                                                                   │
 └──pause/stop◀──────────────────────────────────────────────────────┘
                       (loops forever until stopped or budget hit)
```

### Circuit breakers

| Risk | Breaker |
|---|---|
| Infinite loops | per-goal timeout + `doom_loop: deny` + action-sequence hash detector |
| Token runaway | per-cycle + per-day budget governor; breach → pause |
| Silent "success" | orchestrator runs verify itself; never trusts agent self-report |
| Regressions | git snapshot per cycle; auto-revert on red |
| Destructive commands | work-branch-only; deny-list in opencode permissions |
| Context rot | fresh session per goal |
| No forward progress | diff-budget + no-progress detector → escalate to human |
| Stuck at 3am | kill switch (dashboard button / API) honored mid-cycle |

---

## Development

```bash
./venv/bin/pip install -r requirements.txt   # includes pytest
./venv/bin/python -m pytest -q               # 61 tests
./venv/bin/uvicorn src.main:app --port 8090  # run locally
```

### Project layout

```
src/
├── main.py              FastAPI app + lifespan (starts background tasks)
├── config.py            Settings (env-driven)
├── models.py            Pydantic models (all subsystems)
├── parsers.py           Defensive llama-server parsers (both schemas)
├── collector.py         Async metrics poll loop
├── state_reader.py      Workspace markdown parser
├── directives.py        Directive queue (file + DB)
├── db.py                SQLite layer
├── ws.py                WebSocket connection manager
├── watcher.py           State file change watcher
├── orchestrator/
│   ├── controller.py    Ralph state machine
│   ├── runner.py        OpenCode subprocess runner (timeout + JSON parse)
│   ├── verify.py        Orchestrator-owned test gate
│   ├── git_ops.py       Branch isolation + snapshot/revert
│   ├── budget.py        Token governor
│   ├── guardrails.py    Loop/no-progress/kill-switch detectors
│   ├── artifacts.py     backlog.md, reflections.md, skill index
│   └── prompts.py       Reflect/plan/execute prompt templates
├── routes/              API routers (one file per subsystem)
└── static/index.html    Single-page dashboard (Tailwind + Chart.js via CDN)
```

### CodeDB

This repo is indexed with [codedb](https://github.com/sst/codedb) for symbol-aware navigation. The `.codedbignore` excludes runtime data, venvs, and binaries.

```bash
codedb index .                 # build/rebuild the index
codedb symbol parse_prometheus # jump to a definition
codedb outline src/orchestrator/controller.py
codedb callers run_goal        # find call sites
```

---

## Background

The **Ralph loop** is [Geoffrey Huntley's](https://ghuntley.com/loop/) orchestrator pattern: spawn fresh-context agent sessions against a persistent backlog, verify completion externally, repeat. Solo Agent extends it with a **Reflexion**-style outer phase (reflect → plan → execute) so the backlog regenerates each cycle, enabling continuous self-improvement.

`/goal` is a Claude Code feature, not an OpenCode built-in. Solo Agent passes structured goal prompts from the orchestrator directly — no custom OpenCode command required.

---

## License

MIT
