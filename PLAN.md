# Solo Agent — Local LLM Agent Monitor & Manager

## Overview
A containerized dashboard for monitoring and managing a local llama.cpp coding agent. Provides real-time visibility into server health, token throughput, context usage, and agent activity — everything you need for oversight of an AI agent grinding away 24/7.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Docker Container (solo-agent)                      │
│                                                     │
│  ┌───────────┐    ┌──────────────┐    ┌──────────┐  │
│  │  FastAPI   │───▶│  Metrics     │───▶│  HTML    │  │
│  │  Backend   │    │  Collector   │    │  Dashboard│  │
│  │  :8090     │    │  (in-memory) │    │  (SPA)   │  │
│  └─────┬─────┘    └──────────────┘    └──────────┘  │
│        │                                            │
└────────┼────────────────────────────────────────────┘
         │ polls
         ▼
┌─────────────────────┐     ┌─────────────────────┐
│  llama-server       │     │  Agent Activity     │
│  :8080              │     │  Log (file/API)     │
│  /health            │     │                     │
│  /metrics           │     │                     │
│  /slots             │     │                     │
│  /props             │     │                     │
│  /v1/chat/completions│    │                     │
└─────────────────────┘     └─────────────────────┘
```

## Data Sources

### From llama-server (already available):
| Endpoint       | Data                                          |
|----------------|-----------------------------------------------|
| `/health`      | Status (ok/loading/error)                     |
| `/metrics`     | Prometheus metrics — token counts, throughput, |
|                | requests processing/deferred, busy slots      |
| `/slots`       | Per-slot state: n_ctx, is_processing,         |
|                | prompt tokens, generation tokens, timing      |
| `/props`       | Model info, default generation parameters     |

### Agent activity (to build):
- Conversation log (chat history per session)
- Tool calls (what tools the agent invoked)
- Code changes (files modified, diffs)
- Task status (what the agent is working on)
- Error log (failures, retries, timeouts)

## Dashboard Sections

### 1. Server Health (top bar)
- Status indicator: green/yellow/red
- Uptime
- Model name + quantization
- VRAM usage estimate

### 2. Performance Metrics (main panel)
- **Prefill throughput** — tokens/s, rolling chart (last 1h)
- **Decode throughput** — tokens/s, rolling chart
- **Request rate** — requests/min
- **Queue depth** — requests_processing + requests_deferred
- **Context utilization** — gauge showing % of 256K used

### 3. Agent Activity Feed (side panel)
- Live feed of what the agent is doing
- Timestamped entries: "Working on X", "Tool call: Y", "File modified: Z"
- Color-coded by type (task/tool/error/system)

### 4. Conversation Viewer
- Click a session to see the full conversation
- Show user messages, assistant responses, tool calls
- Token count per conversation

### 5. System Info
- GPU model, VRAM, driver version
- Model architecture, layer count, expert count
- Current generation parameters (temp, top_p, etc.)

## Tech Stack

| Component    | Choice                | Why                                |
|-------------|-----------------------|------------------------------------|
| Backend      | Python + FastAPI      | Async, lightweight, easy to deploy |
| Frontend     | Single HTML + vanilla JS | No build step, simple           |
| Charts       | Chart.js              | Lightweight, no dependencies       |
| Styling      | Tailwind (CDN)        | Fast to prototype                  |
| Storage      | In-memory ring buffer | No DB for a single agent           |
| Container    | Docker + Compose      | Standard deployment                |
| Polling      | 2s interval           | Low overhead, responsive enough    |

## File Structure

```
solo-agent/
├── README.md
├── PLAN.md
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── src/
│   ├── main.py              # FastAPI app entry point
│   ├── config.py             # Configuration (llama-server URL, etc.)
│   ├── collector.py          # Metrics collector (polls llama-server)
│   ├── models.py             # Data models (metrics, sessions, etc.)
│   ├── routes/
│   │   ├── health.py         # Health proxy + dashboard health
│   │   ├── metrics.py        # Metrics API (historical + live)
│   │   ├── agent.py          # Agent activity API
│   │   └── config.py         # Server config/props API
│   └── static/
│       └── index.html        # Single-page dashboard
└── tests/
    └── test_collector.py
```

## API Endpoints (solo-agent backend)

| Method | Path                  | Description                        |
|--------|-----------------------|------------------------------------|
| GET    | `/api/health`         | Dashboard + llama-server health    |
| GET    | `/api/metrics`        | Current metrics snapshot           |
| GET    | `/api/metrics/history`| Historical metrics (last N mins)   |
| GET    | `/api/slots`          | Slot status from llama-server      |
| GET    | `/api/props`          | Model info + generation params     |
| GET    | `/api/agent/activity` | Recent agent activity entries      |
| GET    | `/api/agent/sessions` | List of agent sessions             |
| GET    | `/api/agent/session/:id` | Full conversation for session   |
| POST   | `/api/agent/log`      | Agent posts its activity here      |
| WS     | `/ws`                 | WebSocket for live updates         |

## Agent Integration
The coding agent posts its activity to `/api/agent/log` as it works:
```json
{
  "type": "task|tool|file|error|system",
  "message": "Analyzing src/auth.py for security issues",
  "session_id": "abc123",
  "metadata": {
    "tool": "read_file",
    "file": "src/auth.py",
    "tokens_used": 1234
  }
}
```

## Implementation Phases

### Phase 1: Core Monitoring (MVP)
- FastAPI backend polling llama-server health/metrics/slots
- HTML dashboard with real-time charts
- Docker container
- Basic agent activity log (file-based)

### Phase 2: Agent Integration
- WebSocket for live updates
- Conversation viewer
- Agent activity API for the coding agent to report in
- Session management

### Phase 3: Polish
- Historical data persistence (SQLite)
- Alerts (agent stuck, high error rate, OOM)
- Dark mode theme
- Mobile-responsive layout

## Docker Compose

```yaml
version: "3.8"
services:
  solo-agent:
    build: .
    ports:
      - "8090:8090"
    environment:
      - LLAMA_SERVER_URL=http://host.docker.internal:8080
      - POLL_INTERVAL=2
    extra_hosts:
      - "host.docker.internal:host-gateway"
    restart: unless-stopped
```

## Design Direction
- Dark theme (matches the terminal/hacker aesthetic)
- Monospace fonts for metrics
- Neon accent colors (green for healthy, amber for warning, red for error)
- Minimal, information-dense layout
- Auto-refresh, no manual reload needed
