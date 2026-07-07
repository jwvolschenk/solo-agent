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

### 3.1 Agent System Options (Research Results)

| System        | Verdict   | Context Mgmt | Autonomous? | Weight   | Notes                                         |
|---------------|-----------|--------------|-------------|----------|-----------------------------------------------|
| **OpenCode**  | ✅ Top pick | Auto-compact (built-in) + todo preservation across resets | Yes, sub-agents + multi-agent workflows | Go binary, minimal | Proven with llama.cpp + Qwen3.5-35B-A3B at 262K ctx. `--provider openai-compatible`. Has `compaction-todo-preserver` hook. |
| Aider         | Fallback  | Tree-sitter repo map (token budget), no auto-summarize | No autonomous loop | Python, light | Best as executor inside a custom loop. `--openai-api-base` for local. |
| Goose         | Partial   | Session-based, no built-in compaction | Task mode for longer runs | Rust binary | Needs external context management. |
| OpenHands     | Too heavy | RAG over history (32K default) | Multi-agent delegation | Docker + web UI | Overkill for single local agent. |
| SWE-agent     | Too narrow| ACI + collapsing old observations | Issue-driven, not continuous | Python | Single-issue resolver, not 24/7 builder. |

**Decision: Use OpenCode as the agent runner** with its built-in auto-compact and todo preservation. Wrap it with a thin Python orchestrator for:
- Goal injection (read goal from file, send to OpenCode)
- Monitoring integration (parse OpenCode output, feed to dashboard)
- Brick wall detection (parse errors, pause after N retries)
- Restart on context reset (OpenCode handles the reset internally)

**Fallback:** If OpenCode's tool calling doesn't work well with our 28B model, build a custom Python loop (~200 lines) using the STATE.md pattern:
1. Agent maintains `STATE.md` in the repo
2. After each work chunk: update checkpoint (done/next/blockers)
3. Before context compaction: summarize into STATE.md
4. After context reset: agent reads STATE.md and continues
5. Todo lists survive compaction

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

### Candidate Models (Research Results)

#### Tier 1 — Proven / Drop-in Upgrade (3B active, same arch)

| Model                         | Total | Active | Q4_K_M Size | TurboQuant? | Notes                                     |
|-------------------------------|-------|--------|-------------|-------------|-------------------------------------------|
| **Qwen3.6-35B-A3B** (full)    | 35B   | 3B     | ~22GB       | ✅ UD-Q4_K_M + TQ3 | **Best upgrade path.** Full experts (no REAP pruning loss). Same arch. MTP speculative decoding support. Community reports 70 t/s on RTX 5080 with turbo3 + auto-fit. |
| **Qwen3.5-35B-A3B**           | 35B   | 3B     | ~22GB       | ✅ UD-Q4_K_M + TQ3 | Previous gen, same arch. Proven with turboquant + auto-fit. |
| **GLM-4.7-Flash-REAP-23B-A3B**| 23B   | 3B     | ~10GB       | ✗ No TQ variant | Fits entirely in VRAM! Video creator recommended. Different architecture. |

#### Tier 2 — Stretch Goals (More active params)

| Model                         | Total | Active | Q4_K_M Size | Notes                                     |
|-------------------------------|-------|--------|-------------|-------------------------------------------|
| **Qwen3.5-122B-A10B**         | 122B  | 10B    | ~74GB       | ⚠️ Marginal. 10B active = 3x more compute. Attention alone needs 12-14GB. Reddit: "rarely beat 27B TG speeds." |
| **Qwen-AgentWorld-35B-A3B**   | 35B   | 3B     | ~22GB       | Agent-optimized variant from Unsloth. Worth testing. |

#### Tier 3 — Too Large (128GB+ RAM needed)

| Model                         | Total | Active | Q4_K_M Size | Notes                                     |
|-------------------------------|-------|--------|-------------|-------------------------------------------|
| DeepSeek-V4-Flash             | 284B  | 13B    | ~180GB      | Needs 2×48GB minimum                      |
| Qwen3.5-397B-A17B             | 397B  | 17B    | ~216GB      | Needs 128GB+ unified memory               |
| Llama 4 Scout                 | 109B  | 17B    | ~60GB       | Needs 64GB+ RAM                           |

### Key Findings
1. **3B active param sweet spot is real** — attention layers fit in 16GB VRAM while expert weights live in RAM. Going to 10B+ active breaks this balance.
2. **Qwen3.6-35B-A3B (full, non-REAP) is the clear next step** — 22GB, same arch, same flags, better quality experts.
3. **REAP pruning at scale doesn't help 16GB VRAM** — reduces total params but active params stay same. A 504B model still needs 200GB+ even at Q2.
4. **TurboQuant GGUF variants exist primarily for Qwen 3.5/3.6 models** — DeepSeek/MiniMax/GLM lack dedicated TQ builds.
5. **64GB system RAM upgrade** would unlock Llama 4 Scout (109B/17B active) — worth considering.

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
