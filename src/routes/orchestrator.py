"""GET /api/orchestrator/* and POST /api/orchestrator/{start,pause,resume,stop}."""

from __future__ import annotations

from fastapi import APIRouter

from ..config import settings
from ..db import fetch_cycles
from ..orchestrator.controller import controller

api_router = APIRouter(prefix="/api/orchestrator", tags=["orchestrator"])


@api_router.get("/state")
async def get_state() -> dict:
    """Current orchestrator phase, cycle count, token usage, stall counters."""
    s = controller.state
    return {
        "phase": s.phase,
        "running": s.running,
        "cycle_number": s.cycle_number,
        "current_task": s.current_task,
        "last_outcome": s.last_outcome,
        "last_error": s.last_error,
        "last_snapshot_sha": s.last_snapshot_sha,
        "cycle_tokens_used": s.cycle_tokens_used,
        "daily_tokens_used": s.daily_tokens_used,
        "consecutive_low_change_cycles": s.consecutive_low_change_cycles,
        "consecutive_fail_cycles": s.consecutive_fail_cycles,
        "agent_session_id": s.agent_session_id,
        "project_path": str(settings.project_path),
        "updated_at": s.updated_at.isoformat(),
    }


@api_router.post("/start")
async def start_loop() -> dict:
    msg = await controller.start()
    return {"status": msg, "phase": controller.state.phase}


@api_router.post("/pause")
async def pause_loop() -> dict:
    msg = await controller.pause()
    return {"status": msg, "phase": controller.state.phase}


@api_router.post("/resume")
async def resume_loop() -> dict:
    msg = await controller.resume()
    return {"status": msg, "phase": controller.state.phase}


@api_router.post("/stop")
async def stop_loop() -> dict:
    msg = await controller.stop()
    return {"status": msg, "phase": controller.state.phase}


@api_router.get("/cycles")
async def get_cycles(limit: int = 50) -> dict:
    rows = fetch_cycles(limit=limit)
    return {
        "count": len(rows),
        "cycles": [
            {
                "id": r["id"],
                "cycle_number": r["cycle_number"],
                "phase": r["phase"],
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "outcome": r["outcome"],
                "snapshot_sha": r["snapshot_sha"],
                "head_sha": r["head_sha"],
                "lines_changed": r["lines_changed"],
                "tokens_used": r["tokens_used"],
                "tasks_attempted": r["tasks_attempted"],
                "tasks_passed": r["tasks_passed"],
                "error": r["error"],
                "summary": r["summary"],
                "agent_session_id": r["agent_session_id"],
            }
            for r in rows
        ],
    }
