# Solo Agent — Full Product Plan
## Local LLM Agent Monitor, Manager & Autonomous Runner

---

## 1. Product Vision

A self-contained system that runs a local coding agent 24/7 on a single GPU, given a goal it continuously plans, builds, tracks its own progress, manages its context window, and reports everything to a monitoring dashboard — all without human intervention unless it hits an unrecoverable blocker.

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Docker Compose Stack                                           │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │  Dashboard    │  │  Orchestrator│  │  State Store          │  │
│  │  (Nginx+SPA)  │  │  (Python)    │  │  (SQLite + files)     │  │
│  │  :8090        │◀─│  :8091       │──│  tasks.db             │  │
│  └──────────────┘  └──────┬───────┘  │  summaries/           │  │
│                           │          │  journal.md            │  │
│                           ▼          └───────────────────────┘  │
│                    ┌──────────────┐                              │
│                    │  Agent Loop  │                              │
│                    │  (tool exec) │                              │
│                    └──────┬───────┘                              │
└───────────────────────────┼─────────────────────────────────────┘
                            │ OpenAI-compatible API
                            ▼
                    ┌──────────────────┐
                    │  llama-server    │
                    │  :8080           │
                    │  Qwen3.6-28B     │
                    │  256K context     │
                    └──────────────────┘
```

---

## 3. Autonomous Agent Engine

### 3.1 Why Not Existing Frameworks?

| System        | Verdict   | Why                                                  |
|---------------|-----------|------------------------------------------------------|
| Aider         | Partial   | Great code editing but no autonomous loop mode. No context auto-management. Needs human prompts. |
| OpenHands     | Too heavy | Full web UI + Docker sandbox. Overkill. Designed for cloud models. |
| SWE-agent     | Too narrow| One-issue-at-a-time. No continuous operation.        |
| Cline         | IDE-bound | Needs VS Code. No headless/terminal mode.            |
| CrewAI        | Overkill  | Multi-agent framework. We need one agent, minimal overhead. |
| LangGraph     | Overkill  | General orchestration. Too much abstraction for a single coding agent. |

**Decision: Build a lightweight custom orchestrator.** Our model (28B MoE, 3B active) excels at coding but isn't GPT-4 level. Every token of overhead system prompt costs us quality. A custom loop keeps it lean and gives us full control over context management.

### 3.2 The Agent Loop

```
┌─────────────────────────────────────────────────┐
│                AGENT LOOP                        │
│                                                  │
│  1. READ state (current task, progress, journal) │
│  2. BUILD prompt (system + task + context)        │
│  3. CALL LLM (chat completion)                   │
│  4. PARSE response (reasoning + tool calls)       │
│  5. EXECUTE tools (read/write files, shell, git)  │
│  6. UPDATE state (task progress, journal)         │
│  7. CHECK context usage                           │
│     ├─ < 80% of 256K → goto 1                    │
│     └─ >= 80% → SUMMARIZE → RESET → goto 1       │
│  8. CHECK for blockers                            │
│     ├─ 3 retries on same error → PAUSE, alert     │
│     └─ otherwise → continue                       │
└─────────────────────────────────────────────────┘
```

### 3.3 Context Management Strategy

This is the hardest problem for a 24/7 agent. With 256K context we have room, but it will fill up during long coding sessions.

**Context Budget (256K tokens):**
```
System prompt (tools + role + project):     ~4K tokens   (fixed)
Task tracker + plan:                        ~2K tokens   (updated each turn)
Project journal (recent entries):           ~2K tokens   (rolling window)
Working context (conversation + code):     ~200K tokens  (the actual work)
Reserve:                                   ~50K tokens  (safety margin)
```

**Auto-Summary at 80% threshold:**
1. When context hits ~200K working tokens, pause the loop
2. Ask the LLM: "Summarize our progress. What have we done? What's the current state? What should we do next?"
3. Save summary to `summaries/session-{n}.md`
4. Update the task tracker and journal
5. Reset context with: system prompt + summary + current task + journal
6. Continue working

**State that persists across resets:**
- `tasks.md` — current task list with status (todo/done/blocked)
- `journal.md` — append-only log of decisions and actions
- `summaries/` — per-session summaries
- `plan.md` — the overall project plan
- Git history — all code changes are committed

### 3.4 Tool System

The agent needs these tools (defined in system prompt, called via structured output):

| Tool             | Description                                    |
|------------------|------------------------------------------------|
| `read_file`      | Read a file (with line ranges for large files) |
| `write_file`     | Write/overwrite a file                         |
| `edit_file`      | Find-and-replace edit in a file                |
| `list_files`     | List directory contents                        |
| `search_files`   | Grep/regex search across files                 |
| `run_command`    | Execute a shell command (sandboxed)            |
| `git_commit`     | Stage and commit changes with message          |
| `git_log`        | View recent git history                        |
| `update_task`    | Mark a task as done/blocked/add new task       |
| `journal`        | Write an entry to the project journal          |
| `request_help`   | Signal that human intervention is needed       |

**Tool execution is on the orchestrator side**, not in the LLM. The LLM outputs structured JSON like:
```json
{"tool": "write_file", "path": "src/auth.py", "content": "..."}
```
The orchestrator executes it and feeds the result back.

### 3.5 Prompt Structure

```
SYSTEM: You are an autonomous coding agent. You work independently.
        [Project plan]
        [Current task]
        [Tool definitions]
        [Project conventions]

CONTEXT:
  [Project journal - last 20 entries]
  [Previous summary if resuming]

USER: [Current task description + any tool results from last action]

ASSISTANT: [Reasoning + tool call]

USER: [Tool result]

ASSISTANT: [Next action...]
```

### 3.6 Brick Wall Detection

The agent pauses and requests human intervention when:
- Same error occurs 3 times in a row after different fix attempts
- A task has been "in_progress" for >50 turns without completion
- A tool execution fails with permission/auth errors
- The agent explicitly calls `request_help`
- Context summary reveals confusion (detected by checking if summary contradicts plan)

---

## 4. Monitoring Dashboard

### 4.1 Data Sources

**From llama-server (poll every 2s):**
- `/health` → status
- `/metrics` → Prometheus counters (tokens, throughput, requests)
- `/slots` → per-slot state (n_ctx used, is_processing)
- `/props` → model info

**From orchestrator (poll every 1s):**
- Task status (current task, progress %, queue depth)
- Agent activity log (what it's doing right now)
- Context usage (tokens used / 256K)
- Session info (uptime, context resets, errors)
- Recent journal entries

### 4.2 Dashboard Layout

```
┌─────────────────────────────────────────────────────────────┐
│  SOLO AGENT                                    ● HEALTHY    │
├──────────────────────┬──────────────────────────────────────┤
│                      │                                      │
│  PERFORMANCE         │  CONTEXT USAGE                       │
│  ┌────────────────┐  │  ████████████░░░░░░  78% (198K/256K) │
│  │ Prefill chart   │  │                                      │
│  │ ~~~~~/\~~~/\~~~ │  │  CURRENT TASK                        │
│  └────────────────┘  │  Implementing user authentication     │
│  ┌────────────────┐  │  Task 3/12 | ETA: ~45 min            │
│  │ Decode chart    │  │                                      │
│  │ ~~~~~~~~~~~~~~~ │  │  RECENT ACTIVITY                     │
│  └────────────────┘  │  14:32 wrote src/auth.py              │
│                      │  14:31 edited src/models.py            │
│  THROUGHPUT          │  14:29 shell: npm test                 │
│  Prefill: 2,450 t/s  │  14:28 git commit: "add auth module"  │
│  Decode:   94 t/s    │  14:25 read src/routes/api.py         │
│  Queue:    0         │                                      │
│                      │  JOURNAL (last 5)                     │
│  SERVER              │  Chose JWT over sessions for stateless │
│  Uptime: 4h 23m     │  DB schema designed for users table    │
│  Model: qwen36-reap  │  Added bcrypt for password hashing    │
│  Context: 262,144    │                                      │
│  Slots: 1/1          │                                      │
├──────────────────────┴──────────────────────────────────────┤
│  [View Full Journal] [View Task List] [View Conversations]  │
└─────────────────────────────────────────────────────────────┘
```

### 4.3 Tech Stack

| Component     | Choice           | Why                                  |
|--------------|------------------|--------------------------------------|
| Dashboard     | Single HTML + JS | No build step, served by Nginx       |
| Charts        | Chart.js (CDN)   | 60KB, no dependencies                |
| Styling       | Tailwind (CDN)   | Fast dark-theme prototyping           |
| Backend API   | FastAPI (Python) | Async, auto-docs, lightweight         |
| Database      | SQLite           | Single file, zero config              |
| Container     | Docker Compose   | One command to deploy                 |

---

## 5. Bigger MoE Models — What Could We Run?

### Current Model
- **Qwen3.6-28B-REAP20-A3B-Q4_K_M** — 17GB, 28B total, 3B active, 256K ctx
- Performance: ~2600 t/s prefill, ~94 t/s decode on RTX 5060 Ti

### The Hardware Constraint
- VRAM: 16GB (RTX 5060 Ti)
- System RAM: 32GB (DDR4)
- The model's expert weights live in system RAM (MoE offloading)
- Larger models need more RAM to hold expert weights

### Candidate Models (investigate next)

| Model                              | Total | Active | Q4_K_M Size | Feasible? | Notes                                     |
|------------------------------------|-------|--------|-------------|-----------|-------------------------------------------|
| Qwen3.5-35B-A3B (current family)   | 35B   | 3B     | ~18GB       | ✓ Running | Same arch, possibly better trained         |
| Qwen-AgentWorld-35B-A3B            | 35B   | 3B     | ~18GB       | ✓ Worth testing | Agent-optimized variant from Unsloth   |
| Qwen3.6-27B (dense)                | 27B   | 27B    | ~15GB       | ✓ Tight   | Dense model, all params active, fast decode |
| Qwen3.5-14B                        | 14B   | 14B    | ~8GB        | ✓ Fast    | Dense, fits entirely in VRAM, very fast    |
| Llama 4 Scout                      | 109B  | 17B    | ~60GB       | ✗ Too big | Needs 60GB+ RAM for experts                |
| DeepSeek-V4-Flash                  | 284B  | 13B    | ~170GB      | ✗ Too big | Needs 170GB+ RAM                           |
| Qwen3-235B-A22B                    | 235B  | 22B    | ~130GB      | ✗ Too big | Way beyond our RAM                         |
| Qwen3.5-7B-A1B (if exists)         | ~7B   | ~1B    | ~4GB        | ✓ Ultra fast | Tiny MoE, fits fully in VRAM              |
| GLM-4.7-Flash-REAP-23B-A3B         | 23B   | 3B     | ~13GB       | ✓ Good fit | Video creator recommended, smaller than current |

### Realistic Next Steps
1. **Qwen-AgentWorld-35B-A3B** — Same architecture, agent-optimized. Drop-in replacement.
2. **GLM-4.7-Flash-REAP-23B-A3B** — Smaller, faster, recommended by the video creator.
3. **Qwen3.6-27B (dense)** — All 27B params active. Better quality but needs full VRAM.
4. **Upgrade system RAM to 64GB** — Would unlock Llama 4 Scout (109B/17B active).

---

## 6. File Structure

```
solo-agent/
├── README.md
├── PLAN.md                          # This file
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── src/
│   ├── main.py                      # FastAPI entry point
│   ├── config.py                    # Configuration
│   ├── collector.py                 # llama-server metrics poller
│   ├── orchestrator/
│   │   ├── __init__.py
│   │   ├── loop.py                  # The main agent loop
│   │   ├── context.py               # Context management (budget, summarize)
│   │   ├── tools.py                 # Tool definitions and executor
│   │   ├── prompts.py               # System prompt builder
│   │   └── state.py                 # State persistence (tasks, journal)
│   ├── models.py                    # Pydantic data models
│   ├── routes/
│   │   ├── health.py                # Health proxy
│   │   ├── metrics.py               # Metrics API
│   │   ├── agent.py                 # Agent control (start/stop/pause)
│   │   └── sessions.py              # Conversation/session viewer
│   └── static/
│       ├── index.html               # Dashboard SPA
│       ├── app.js                   # Dashboard logic
│       └── style.css                # Custom styles
├── workspace/                       # Mount point for agent's working directory
│   ├── plan.md                      # Project plan (agent maintains)
│   ├── tasks.md                     # Task tracker (agent maintains)
│   ├── journal.md                   # Decision journal (agent maintains)
│   └── summaries/                   # Context reset summaries
└── tests/
    ├── test_collector.py
    ├── test_context.py
    └── test_tools.py
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
      - "8091:8091"    # API + orchestrator
    environment:
      - LLAMA_SERVER_URL=http://host.docker.internal:8080
      - POLL_INTERVAL=2
      - CONTEXT_THRESHOLD=0.8
      - MAX_RETRIES=3
      - WORKSPACE=/workspace
    volumes:
      - ./workspace:/workspace
      - ./data:/data
    extra_hosts:
      - "host.docker.internal:host-gateway"
    restart: unless-stopped
```

---

## 8. Implementation Phases

### Phase 1: Core Monitoring (Day 1)
- [ ] FastAPI backend with llama-server health/metrics/slots polling
- [ ] HTML dashboard with real-time charts (Chart.js)
- [ ] Docker container
- [ ] Basic metrics history (in-memory ring buffer)

### Phase 2: Agent Orchestrator (Day 2)
- [ ] Agent loop (prompt → LLM → parse → execute → update)
- [ ] Tool system (file ops, shell, git)
- [ ] State persistence (SQLite + markdown files)
- [ ] Start/stop/pause controls via API

### Phase 3: Context Management (Day 3)
- [ ] Context token counting (tiktoken or model-specific)
- [ ] Auto-summarize at threshold
- [ ] Context reset with state restoration
- [ ] Session management (multiple sessions over time)

### Phase 4: Dashboard Integration (Day 4)
- [ ] Agent activity feed in dashboard
- [ ] Task list viewer
- [ ] Journal viewer
- [ ] Conversation history browser
- [ ] WebSocket for live updates

### Phase 5: Resilience (Day 5)
- [ ] Brick wall detection (retry limits, stuck detection)
- [ ] Error recovery and logging
- [ ] Alert system (webhook/log when stuck)
- [ ] Graceful shutdown and resume

---

## 9. Cold Start Checklist

For a new session to pick up and build this:
1. Read this PLAN.md fully
2. llama-server is running at localhost:8080 (start with `~/services/llama-tq/start-server.sh`)
3. Model: Qwen3.6-28B-REAP20-A3B-Q4_K_M, 256K context, ~94 t/s decode
4. The agent communicates via OpenAI-compatible API at /v1/chat/completions
5. The agent needs tool calling via structured JSON output (not function calling)
6. Workspace mount point: ./workspace/ contains plan.md, tasks.md, journal.md
7. All code goes in ~/repos/solo-agent/
8. Docker Compose is the deployment target
9. Dashboard at :8090, API at :8091
