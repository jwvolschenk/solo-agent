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


class ConfigUpdate(BaseModel):
    project_path: str | None = None
    goal: str | None = None
    verify_command: str | None = None


@api_router.get("")
async def get_config() -> dict:
    """Return the current runtime config."""
    return {
        "project_path": str(settings.project_path),
        "goal": settings.goal,
        "verify_command": settings.verify_command,
        "work_branch": settings.work_branch,
        "base_branch": settings.base_branch,
        "agent_command": settings.agent_command,
        "agent_model": settings.agent_model,
    }


@api_router.put("")
async def set_config(body: ConfigUpdate) -> dict:
    """Update runtime config. Any of project_path / goal / verify_command.

    The orchestrator picks up changes on its next cycle. project_path must be a
    directory (it'll be git-init'd on start if not already a repo). An empty
    verify_command disables the orchestrator gate (agent self-verifies).
    """
    if body.project_path is not None:
        p = Path(body.project_path).expanduser()
        if not p.exists() or not p.is_dir():
            raise HTTPException(status_code=400, detail=f"{body.project_path} is not a directory")
        settings.project_path = p
    if body.goal is not None:
        settings.goal = body.goal
    if body.verify_command is not None:
        settings.verify_command = body.verify_command
    return {
        "status": "ok",
        "project_path": str(settings.project_path),
        "goal": settings.goal,
        "verify_command": settings.verify_command,
    }
