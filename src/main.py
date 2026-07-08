"""Solo Agent — FastAPI application entry point.

Wires together all three subsystems:
  - Monitoring: collector polls llama-server, serves metrics/health
  - Directives: human -> agent feedback via directives.md
  - Orchestrator: Ralph loop driving OpenCode (background task)

Run:
    uvicorn src.main:app --host 0.0.0.0 --port 8090
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .collector import collector
from .config import settings
from .db import init_db
from .ws import manager
from .watcher import watcher
from .orchestrator.controller import controller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("solo.main")

# Wire the collector's broadcast to the WebSocket manager.
collector._broadcast = manager.broadcast_snapshot


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background tasks on boot, stop them on shutdown."""
    log.info("solo-agent starting (llama=%s, project=%s)", settings.llama_server_url, settings.project_path)
    init_db()
    await collector.start()
    await watcher.start()
    if settings.autostart_orchestrator:
        await controller.start()
    try:
        yield
    finally:
        log.info("solo-agent shutting down")
        await controller.stop()
        await watcher.stop()
        await collector.stop()


app = FastAPI(
    title="Solo Agent",
    description="Monitoring dashboard + directive queue + Ralph orchestrator for a local coding agent.",
    version="0.1.0",
    lifespan=lifespan,
)

# Register all routers.
from .routes import (  # noqa: E402 (import after app for circular-free wiring)
    agent,
    config_route,
    directives,
    health,
    metrics,
    orchestrator,
    projects,
    server,
    state,
    ws as ws_route,
)

for mod in (health, metrics, server, agent, directives, state, orchestrator, config_route, projects, ws_route):
    app.include_router(mod.api_router)


# --- Static dashboard (served at /) ------------------------------------------
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def index():
    """Serve the single-page dashboard."""
    idx = STATIC_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return JSONResponse(
        {"detail": "dashboard not built; static/index.html missing"}, status_code=404
    )


# Mount static assets (if any beyond index.html) at /static.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
