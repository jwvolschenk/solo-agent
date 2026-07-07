# Solo Agent — Cold Start Briefing
## Everything a new session needs to know

---

## The Hardware
- **GPU**: RTX 5060 Ti 16GB VRAM (Blackwell sm_120)
- **CPU**: AMD Ryzen 7 3700X (8 cores, 16 threads)
- **RAM**: 32GB DDR4
- **OS**: Linux (jwv-mint)

## The Model Server
- **Fork**: TurboQuant llama.cpp at `~/services/llama-tq/llama-cpp-turboquant/`
- **Model**: `/home/jwvolschenk/models/Qwen3.6-28B-REAP20-A3B-Q4_K_M.gguf`
  - Architecture: qwen35moe (Qwen3.5 MoE + SSM hybrid)
  - 40 layers, 205 experts, 8 active per token
  - 28B total params, 3B active per token
  - Full attention every 4th layer, SSM (Mamba) for the rest
  - Native context: 262,144 tokens (256K)
- **Startup script**: `~/services/llama-tq/start-server.sh`
- **Systemd service**: `systemctl --user start llama-server`
- **Endpoint**: `http://localhost:8080/v1/chat/completions` (OpenAI-compatible)
- **Health**: `http://localhost:8080/health`
- **Metrics**: `http://localhost:8080/metrics` (Prometheus format)
- **Slots**: `http://localhost:8080/slots`

## Key Server Settings (all benchmarked)
```
-ngl -1              # Auto-fit: per-tensor split (attention on GPU, experts on CPU)
-fitt 500            # 500 MiB VRAM margin
-ctk turbo4          # 4-bit KV keys (TurboQuant)
-ctv turbo2          # 2-bit KV values (TurboQuant)
--no-mmap            # Pinned memory for faster PCIe transfers
--mlock              # Prevent swapping
-ub 2048             # Micro-batch (biggest prefill win)
-b 4096              # Batch size
-c 262144            # 256K context
-t 8                 # CPU threads
```

## Performance (benchmarked)
```
Prefill (pp512):   1,935-1,979 t/s
Prefill (pp4096):  2,603 t/s
Prefill (pp8192):  2,563 t/s
Prefill (pp16384): 2,463 t/s
Decode (tg128):    93-97 t/s
Context:           262,144 tokens (256K) — zero perf penalty
```

## The Product (what we're building)
A containerized monitoring + autonomous agent system:
1. **Dashboard** (HTML/JS at :8090) — real-time monitoring of server + agent
2. **Orchestrator** (Python/FastAPI at :8091) — runs the agent loop
3. **Agent Loop** — given a goal, plans → builds → tracks → summarizes → continues

## How the Agent Works
- Receives a goal via API or config file
- Breaks it into tasks (stored in `workspace/tasks.md`)
- Executes one task at a time via LLM calls
- Uses tools: read/write/edit files, shell commands, git
- Tracks progress in `workspace/journal.md`
- When context hits 80% of 256K: auto-summarize, reset, continue
- Pauses for human help when stuck (3 retries on same error)

## Code Repo
- `~/repos/solo-agent/` — cloned from `git@github.com:jwvolschenk/solo-agent.git`
- Read `PLAN.md` for full architecture and implementation phases

## Key Files
- `PLAN.md` — full product plan with architecture, phases, file structure
- `COLDSTART.md` — this file
- `docker-compose.yml` — deployment config (to be created)
- `src/` — all source code (to be created)

## What to Build First
1. Read PLAN.md
2. Start llama-server if not running
3. Build Phase 1: metrics collector + dashboard
4. Build Phase 2: agent orchestrator + tool system
5. Build Phase 3: context management
6. Build Phase 4: dashboard integration
7. Build Phase 5: resilience

## External References
- llama-server API: `http://localhost:8080` (OpenAI-compatible)
- Prometheus metrics format at `/metrics`
- TurboQuant fork: `~/services/llama-tq/llama-cpp-turboquant/`
- Fable fork (alternative, no turbo KV): `~/services/custom-llamacpp/llama.cpp/`
