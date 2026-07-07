# Solo Agent — Cold Start Briefing

## What This Project Is
A monitoring dashboard for a local llama.cpp coding agent. It watches server health, performance metrics, and agent activity. It does NOT run the agent — the agent runs separately.

## The Target Being Monitored
- **llama-server** at `http://localhost:8080`
  - Model: Qwen3.6-28B-REAP20-A3B-Q4_K_M (MoE, 28B total, 3B active)
  - Context: 262,144 tokens (256K)
  - Startup: `~/services/llama-tq/start-server.sh`
  - Service: `systemctl --user start llama-server`
  - Endpoints: `/health`, `/metrics` (Prometheus), `/slots`, `/props`

- **Agent** running separately (OpenCode, Aider, custom, whatever)
  - Writes to shared state files: tasks.md, journal.md, plan.md
  - Can POST activity to our API (optional)

## Key Performance Numbers (for display context)
```
Prefill: ~1,940-2,600 t/s depending on prompt size
Decode:  ~94 t/s
Context: 262,144 tokens
```

## What to Build
Read PLAN.md for the full architecture. In short:
1. FastAPI backend polling llama-server metrics
2. HTML dashboard with Chart.js charts
3. State file reader (tasks.md, journal.md parser)
4. Agent activity API (optional POST endpoint)
5. Docker container

## Files
- `PLAN.md` — full architecture and implementation plan
- `COLDSTART.md` — this file
- `src/` — all source code (to be created)
- `docker-compose.yml` — deployment (to be created)

## Code Repo
`~/repos/solo-agent/` — cloned from `git@github.com:jwvolschenk/solo-agent.git`
