# Solo Agent — Monitoring Dashboard Plan

## What This Is
A containerized web dashboard that monitors a local llama.cpp coding agent. It watches server health, token throughput, context usage, and agent activity — providing oversight on whatever agent is grinding away.

## What This Is NOT
This is NOT the agent itself. The agent runs separately (e.g. OpenCode, Aider, custom loop). This dashboard is a passive observer and light manager.

> **Update (post-build):** The project grew two more subsystems beyond the original
> monitoring dashboard. See sections 10 and 11 below. The full picture is in README.md.

---

## 1. System Architecture

```
┌─────────────────────────────────────────────────────┐
│  Docker Compose (solo-agent)                        │
│                                                     │
│  ┌──────────────┐  ┌──────────────┐                 │
│  │  Dashboard    │  │  API Server  │                 │
│  │  (Nginx+SPA)  │  │  (FastAPI)   │                 │
│  │  :8090        │◀─│  :8091       │                 │
│  └──────────────┘  └──────┬───────┘                 │
│                           │                         │
│                    ┌──────┴───────┐                  │
│                    │  Collector   │                  │
│                    │  (polls 2s)  │                  │
│                    └──────┬───────┘                  │
└───────────────────────────┼─────────────────────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
    ┌──────────────┐ ┌────────────┐ ┌──────────────┐
    │ llama-server │ │ Agent      │ │ Shared       │
    │ :8080        │ │ Activity   │ │ State Files  │
    │ /health      │ │ Log API    │ │ tasks.md     │
    │ /metrics     │ │            │ │ journal.md   │
    │ /slots       │ │            │ │ plan.md      │
    │ /props       │ │            │ │ summaries/   │
    └──────────────┘ └────────────┘ └──────────────┘
```

The agent (OpenCode or whatever) runs independently. The dashboard:
- Polls llama-server endpoints for server metrics
- Reads shared state files the agent writes to
- Accepts activity log posts from the agent via API
- Displays everything in a real-time web dashboard

---

## 2. Data Sources

### 2.1 llama-server (poll every 2s)

| Endpoint   | Data                                                       |
|------------|------------------------------------------------------------|
| `/health`  | Status (ok/loading/error)                                  |
| `/metrics` | Prometheus counters: tokens processed, throughput, queue   |
| `/slots`   | Per-slot: n_ctx, is_processing, prompt tokens, gen tokens  |
| `/props`   | Model name, architecture, default generation params        |

Available Prometheus metrics:
```
llamacpp:prompt_tokens_total        — total prompt tokens processed
llamacpp:prompt_seconds_total       — total prefill time
llamacpp:tokens_predicted_total     — total generated tokens
llamacpp:tokens_predicted_seconds_total — total decode time
llamacpp:prompt_tokens_seconds      — current prefill throughput (t/s)
llamacpp:predicted_tokens_seconds   — current decode throughput (t/s)
llamacpp:requests_processing        — active requests
llamacpp:requests_deferred          — queued requests
llamacpp:n_busy_slots_per_decode    — average busy slots
llamacpp:n_tokens_max               — largest observed n_tokens
```

### 2.2 Shared State Files (read on change)

The agent writes these files as it works. Dashboard reads and displays them.

| File             | Content                                    |
|------------------|--------------------------------------------|
| `tasks.md`       | Task list with status (todo/done/blocked)  |
| `journal.md`     | Append-only log of decisions and actions   |
| `plan.md`        | Overall project plan                       |
| `summaries/*.md` | Context reset summaries                    |

### 2.3 Agent Activity API (agent posts to us)

The agent can POST activity events to our API:
```
POST /api/agent/activity
{
  "type": "task|tool|file|error|system",
  "message": "Wrote src/auth.py",
  "timestamp": "2026-07-07T14:32:00Z",
  "metadata": {"tool": "write_file", "file": "src/auth.py"}
}
```

---

## 3. Dashboard Layout

```
┌─────────────────────────────────────────────────────────────┐
│  SOLO AGENT MONITOR                         ● HEALTHY       │
├──────────────────────┬──────────────────────────────────────┤
│                      │                                      │
│  PERFORMANCE         │  CONTEXT USAGE                       │
│  ┌────────────────┐  │  ████████████░░░░░░  78% (198K/256K) │
│  │ Prefill t/s     │  │                                      │
│  │ ~~~~~/\~~~/\~~~ │  │  SERVER                              │
│  └────────────────┘  │  Uptime: 4h 23m                      │
│  ┌────────────────┐  │  Model: qwen36-reap                  │
│  │ Decode t/s      │  │  Context: 262,144                    │
│  │ ~~~~~~~~~~~~~~~ │  │  Slots: 1/1 (0 processing)          │
│  └────────────────┘  │                                      │
│                      │  CURRENT TASK (from tasks.md)         │
│  THROUGHPUT          │  Implementing user authentication     │
│  Prefill: 2,450 t/s  │  Task 3/12                           │
│  Decode:   94 t/s    │                                      │
│  Queue:    0         │  RECENT ACTIVITY                     │
│  Tokens:   1.2M total│  14:32 wrote src/auth.py              │
│                      │  14:31 edited src/models.py            │
│                      │  14:29 shell: npm test                 │
│                      │  14:28 git commit: "add auth module"  │
│                      │                                      │
│                      │  JOURNAL (last 5)                     │
│                      │  Chose JWT over sessions              │
│                      │  DB schema designed                   │
├──────────────────────┴──────────────────────────────────────┤
│  [Task List] [Full Journal] [Summaries] [Model Config]      │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. API Endpoints

| Method | Path                     | Description                          |
|--------|--------------------------|--------------------------------------|
| GET    | `/api/health`            | Dashboard + llama-server health      |
| GET    | `/api/metrics`           | Current metrics snapshot             |
| GET    | `/api/metrics/history`   | Historical metrics (last N minutes)  |
| GET    | `/api/slots`             | Slot status from llama-server        |
| GET    | `/api/props`             | Model info + generation params       |
| GET    | `/api/agent/activity`    | Recent agent activity entries        |
| POST   | `/api/agent/activity`    | Agent posts its activity here        |
| GET    | `/api/state/tasks`       | Current task list (parsed tasks.md)  |
| GET    | `/api/state/journal`     | Recent journal entries               |
| GET    | `/api/state/summaries`   | List of context reset summaries      |
| WS     | `/ws`                    | WebSocket for live dashboard updates |

---

## 5. Tech Stack

| Component   | Choice              | Why                                |
|-------------|---------------------|------------------------------------|
| Backend     | Python + FastAPI    | Async, lightweight, auto-docs      |
| Frontend    | Single HTML + JS    | No build step, served by Nginx     |
| Charts      | Chart.js (CDN)      | 60KB, no dependencies              |
| Styling     | Tailwind (CDN)      | Fast dark-theme prototyping        |
| Storage     | SQLite + files      | Metrics in SQLite, state in files  |
| Container   | Docker Compose      | One command to deploy              |
| Polling     | 2s interval         | Low overhead, responsive enough    |

---

## 6. File Structure

```
solo-agent/
├── README.md
├── PLAN.md
├── COLDSTART.md
├── AGENTS.md
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── src/
│   ├── main.py              # FastAPI entry point
│   ├── config.py            # Configuration (llama-server URL, paths)
│   ├── collector.py         # Metrics collector (polls llama-server)
│   ├── state_reader.py      # Reads shared state files (tasks, journal)
│   ├── models.py            # Pydantic data models
│   ├── routes/
│   │   ├── health.py        # Health proxy
│   │   ├── metrics.py       # Metrics API (historical + live)
│   │   ├── agent.py         # Agent activity API (POST + GET)
│   │   └── state.py         # State files API (tasks, journal, summaries)
│   └── static/
│       └── index.html       # Single-page dashboard (HTML+JS+CSS)
└── tests/
    └── test_collector.py
```

---

## 7. Docker Compose

```yaml
version: "3.8"
services:
  dashboard:
    build: .
    ports:
      - "8090:8090"    # Dashboard UI
      - "8091:8091"    # API
    environment:
      - LLAMA_SERVER_URL=http://host.docker.internal:8080
      - POLL_INTERVAL=2
      - STATE_DIR=/state
    volumes:
      - /path/to/agent/workspace:/state    # Agent's task/journal files
      - ./data:/data                        # SQLite metrics storage
    extra_hosts:
      - "host.docker.internal:host-gateway"
    restart: unless-stopped
```

---

## 8. Implementation Phases

### Phase 1: Core Monitoring
- [ ] FastAPI backend with llama-server health/metrics/slots polling
- [ ] Metrics history in SQLite (ring buffer, last 24h)
- [ ] HTML dashboard with Chart.js (prefill/decode throughput)
- [ ] Server health indicator, model info, slot status
- [ ] Docker container

### Phase 2: State File Reader
- [ ] Read and parse tasks.md, journal.md, plan.md
- [ ] Display current task, task list, journal entries
- [ ] File watcher for live updates (inotify or polling)

### Phase 3: Agent Activity API
- [ ] POST /api/agent/activity endpoint
- [ ] Activity feed display in dashboard
- [ ] Activity history in SQLite

### Phase 4: Polish
- [ ] WebSocket for live dashboard updates
- [ ] Dark theme refinement
- [ ] Summary viewer
- [ ] Model config display (/props)
- [ ] Historical charts (last 1h, 6h, 24h)

---

## 9. Cold Start Checklist

1. Read this PLAN.md
2. All code in ~/repos/solo-agent/
3. llama-server runs at localhost:8080 (managed separately)
4. The agent runs separately (OpenCode at localhost or wherever)
5. Dashboard just watches — it doesn't control the agent
6. The agent CAN post activity to our API, but doesn't have to
7. State files (tasks.md etc.) are in the agent's workspace, mounted into the container

---

## 10. Directive Queue (human → agent feedback)

A second subsystem added during implementation: a feedback channel that lets a
human queue guidance or direction for the running agent.

- Lives in `<STATE_DIR>/directives.md`, a sibling of tasks.md/journal.md.
- Full lifecycle: `pending → acknowledged → done`.
- Two convergent channels: the agent edits the `status:` line in the file, **or**
  PATCHes `/api/agent/directives/{id}`. Both stay in sync (file ↔ SQLite).
- Priorities: `high | normal | low`.
- IDs are dashboard-minted (`d1`, `d2`, …) and stable.

Endpoints: `GET/POST /api/agent/directives`, `GET/PATCH /api/agent/directives/{id}`.

Agent integration contract: read directives.md each loop; advance pending items.

---

## 11. Ralph Orchestrator (autonomous self-improvement loop)

A third subsystem: a 24/7 loop that drives OpenCode headlessly to continuously
improve a target repo. Implements the [Ralph loop](https://ghuntley.com/loop/)
pattern (fresh-context-per-goal orchestrator) extended with a Reflexion-style
reflect→plan→execute outer cycle.

### State machine
```
IDLE ──start──▶ REFLECT ──▶ PLAN ──▶ EXECUTE ──▶ VERIFY ──▶ RECORD ──┐
 ▲                                                                   │
 └──pause/stop◀──────────────────────────────────────────────────────┘
```

### Design rules (non-negotiable)
- **Fresh context per goal** — each `opencode run` is a new session; no context bleed.
- **Done is externally verified** — the orchestrator runs `VERIFY_COMMAND` itself; the agent cannot self-attest.
- **Git isolation** — all work on `WORK_BRANCH`; never touches `BASE_BRANCH`; snapshot + auto-revert on red.
- **Orchestrator owns the loop** — the agent cannot spawn the next cycle, raise its budget, or bypass the gate.

### Circuit breakers
Per-goal hard timeout (runs can hang), token budget governor (per-cycle + per-day),
loop/no-progress/diminishing-returns detectors, kill switch honored mid-cycle.
OpenCode `doom_loop: deny` + `question: deny` required for unattended runs.

### Configurability
`TARGET_REPO` (defaults to solo-agent itself — self-improving), `VERIFY_COMMAND`
(default `pytest -q`), `AGENT_COMMAND` (default `opencode run --auto …`, swappable).

Endpoints: `GET /api/orchestrator/state`, `POST /api/orchestrator/{start,pause,resume,stop}`, `GET /api/orchestrator/cycles`.
