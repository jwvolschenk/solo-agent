# Solo Agent — Agent Instructions

You are an autonomous coding agent working on the Solo Agent project.
This is a Python/FastAPI monitoring dashboard and orchestrator for a local llama.cpp coding agent.

Read PLAN.md for the full architecture. Read COLDSTART.md for environment details.

## Project Structure
- `src/main.py` — FastAPI entry point
- `src/orchestrator/` — Agent loop, context management, tools
- `src/routes/` — API endpoints
- `src/static/` — Dashboard HTML/JS/CSS
- `workspace/` — Agent's working directory (plan.md, tasks.md, journal.md)
- `docker-compose.yml` — Deployment config

## Key Constraints
- Target: localhost:8080 OpenAI-compatible API (llama-server)
- Model: Qwen3.6-28B MoE, 256K context, ~94 t/s decode
- Container runs on Docker, dashboard at :8090, API at :8091
- Keep it lean — this model is great at coding but not GPT-4 class
- Minimize system prompt overhead, every token counts

## Running
```bash
pip install -r requirements.txt
uvicorn src.main:app --host 0.0.0.0 --port 8091
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
- Async where possible (httpx for HTTP calls)
- SQLite for persistence, markdown for human-readable state
- Dark theme for dashboard (Tailwind via CDN)
- Commit messages: conventional commits (feat:, fix:, etc.)
