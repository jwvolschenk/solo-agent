# Solo Agent — Agent Instructions

You are an autonomous coding agent working on the Solo Agent project.
This is a Python/FastAPI control plane for a local llama.cpp coding agent, with
three subsystems: a monitoring dashboard, a directive queue, and a Ralph
orchestrator (autonomous self-improvement loop).

Read PLAN.md for the full architecture. Read COLDSTART.md for environment details.
Read README.md for the API reference and agent-integration contracts.

## Project Structure
- `src/main.py` — FastAPI entry point (lifespan starts background tasks)
- `src/config.py` — env-driven settings (all subsystems)
- `src/models.py` — Pydantic models (monitoring, directives, orchestrator)
- `src/parsers.py` — defensive llama-server parsers (classic + current schemas)
- `src/collector.py` — async metrics poll loop (2s)
- `src/state_reader.py` — workspace markdown parser (tasks/journal/plan)
- `src/directives.py` — directive queue (file + DB, dual channel)
- `src/db.py` — SQLite persistence layer
- `src/ws.py` — WebSocket connection manager
- `src/watcher.py` — state file change watcher
- `src/orchestrator/` — Ralph loop (controller, runner, verify, git_ops, budget, guardrails, artifacts, prompts)
- `src/routes/` — API endpoints (one file per subsystem)
- `src/static/index.html` — single-page dashboard (Tailwind + Chart.js via CDN)
- `workspace/` — shared state files (tasks.md, journal.md, directives.md, plan.md)
- `docker-compose.yml` — deployment config

## Key Constraints
- Target: localhost:8080 OpenAI-compatible API (llama-server)
- Model: Qwen3.6-28B MoE, 256K context, ~94 t/s decode
- Container runs on Docker, dashboard + API at :8090 (single port)
- Keep it lean — this model is great at coding but not GPT-4 class
- Minimize system prompt overhead, every token counts
- The dashboard is a passive observer + light commander; it does NOT replace the agent
- The orchestrator never commits to BASE_BRANCH; all work on WORK_BRANCH with snapshot/revert
- Verification is orchestrator-owned — the agent cannot self-attest completion

## Running
```bash
pip install -r requirements.txt
uvicorn src.main:app --host 0.0.0.0 --port 8090
```

## Testing
```bash
python -m pytest -q    # 61 tests (parsers, routes, directives, runner, verify, git_ops, controller)
```

## Using CodeDB
codedb is a symbol-aware code navigation MCP server. Use it for:
- `codedb_word` — exact identifier lookup (fastest)
- `codedb_symbol` — find definitions (class, method, etc.)
- `codedb_search` — substring/regex search
- `codedb_outline` — file structure (run before reading large files)
- `codedb_read` — read file lines (use outline to pick range)
- `codedb_callers` — find who calls a function
- `codedb_deps` — dependency graph (blast radius)
- `codedb_query` — chain ops in one call to save round-trips

Prefer codedb over grep/find — it's indexed and symbol-ranked.

## Conventions
- Python 3.12, type hints required
- FastAPI for all API routes
- Async where possible (httpx for HTTP calls, asyncio.create_subprocess_exec for git/opencode)
- SQLite for persistence, markdown for human-readable state
- Dark theme for dashboard (Tailwind via CDN)
- Commit messages: conventional commits (feat:, fix:, etc.)
