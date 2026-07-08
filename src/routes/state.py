"""GET /api/state/* — parsed shared-state files (tasks, journal, plan, summaries)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..state_reader import (
    list_summaries,
    read_backlog,
    read_plan,
    read_reflections,
    read_summary,
    read_tasks,
    read_journal,
)

api_router = APIRouter(prefix="/api/state", tags=["state"])


def _state_dict(sf) -> dict:
    return {
        "name": sf.name,
        "path": sf.path,
        "exists": sf.exists,
        "mtime": sf.mtime.isoformat() if sf.mtime else None,
        "size": sf.size,
        "content": sf.content,
        "tasks": [t.model_dump(mode="json") for t in sf.tasks],
        "entries": [e.model_dump(mode="json") for e in sf.entries],
    }


@api_router.get("/tasks")
async def get_tasks() -> dict:
    return _state_dict(read_tasks())


@api_router.get("/journal")
async def get_journal() -> dict:
    return _state_dict(read_journal())


@api_router.get("/plan")
async def get_plan() -> dict:
    return _state_dict(read_plan())


@api_router.get("/backlog")
async def get_backlog() -> dict:
    """The agent's live task list (backlog.md in project_path)."""
    return _state_dict(read_backlog())


@api_router.get("/reflections")
async def get_reflections() -> dict:
    """The agent's episodic memory (reflections.md in project_path)."""
    return _state_dict(read_reflections())


@api_router.get("/summaries")
async def get_summaries() -> dict:
    """List summary files (names + sizes only; fetch individually for content)."""
    return {
        "summaries": [
            {"name": s.name, "size": s.size, "mtime": s.mtime.isoformat() if s.mtime else None}
            for s in list_summaries()
        ]
    }


@api_router.get("/summaries/{name}")
async def get_summary(name: str) -> dict:
    sf = read_summary(name)
    if not sf.exists:
        raise HTTPException(status_code=404, detail=f"summary '{name}' not found")
    return _state_dict(sf)
