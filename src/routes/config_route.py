"""GET/PUT /api/config — runtime configuration the dashboard can change.

Currently just project_path (where the orchestrator + OpenCode work). Kept
separate from the env-driven settings so the dashboard can point the loop at a
folder without restarting the server.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings

api_router = APIRouter(prefix="/api/config", tags=["config"])


class ProjectPathUpdate(BaseModel):
    project_path: str


@api_router.get("")
async def get_config() -> dict:
    """Return the current runtime config."""
    return {
        "project_path": str(settings.project_path),
        "verify_command": settings.verify_command,
        "work_branch": settings.work_branch,
        "base_branch": settings.base_branch,
        "agent_command": settings.agent_command,
        "agent_model": settings.agent_model,
    }


@api_router.put("")
async def set_config(body: ProjectPathUpdate) -> dict:
    """Update project_path at runtime (where the orchestrator works).

    The orchestrator picks this up on its next cycle; a running cycle finishes
    on the old path first. Path must exist and be a directory.
    """
    p = Path(body.project_path).expanduser()
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=400, detail=f"{body.project_path} is not a directory")
    settings.project_path = p
    return {"status": "ok", "project_path": str(p)}
