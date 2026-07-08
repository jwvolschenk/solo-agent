"""GET/POST/PUT/DELETE /api/projects, POST /api/projects/{id}/activate."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..db import (
    delete_project,
    fetch_project,
    fetch_projects,
    get_orch_state,
    insert_project,
    update_project,
)
from ..models import Project, ProjectCreate, ProjectUpdate
from ..orchestrator.controller import controller

api_router = APIRouter(prefix="/api/projects", tags=["projects"])


def _slugify(name: str) -> str:
    """Make a URL-safe id from a project name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        slug = "project"
    # ensure uniqueness
    base = slug
    n = 2
    while fetch_project(slug) is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _row_to_project(row, active_id: str | None) -> Project:
    is_active = active_id == row["id"]
    # denormalize orch phase for the sidebar status dot
    phase = "idle"
    if is_active:
        phase = controller.state.phase
    return Project(
        id=row["id"],
        name=row["name"],
        goal=row["goal"],
        project_path=row["project_path"],
        verify_command=row["verify_command"],
        work_branch=row["work_branch"],
        stop_after_cycle=bool(row["stop_after_cycle"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        is_active=is_active,
        orch_phase=phase,
    )


@api_router.get("")
async def list_projects() -> dict:
    """List all projects with active flag + orchestrator phase for sidebar dots."""
    from ..db import get_active_project

    active = get_active_project()
    rows = fetch_projects()
    projects = [_row_to_project(r, active).model_dump(mode="json") for r in rows]
    return {"count": len(projects), "projects": projects, "active_project_id": active}


@api_router.post("", status_code=201)
async def create_project(body: ProjectCreate) -> dict:
    """Create a new project."""
    pid = _slugify(body.name)
    now = datetime.utcnow().isoformat()
    p = {
        "id": pid,
        "name": body.name,
        "goal": body.goal,
        "project_path": body.project_path,
        "verify_command": body.verify_command,
        "work_branch": "solo-agent/auto",
        "stop_after_cycle": 0,
        "created_at": now,
        "updated_at": now,
    }
    insert_project(p)
    return {"status": "ok", "project": Project(**p, is_active=False).model_dump(mode="json")}  # type: ignore[arg-type]


@api_router.get("/{project_id}")
async def get_project(project_id: str) -> dict:
    row = fetch_project(project_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"project '{project_id}' not found")
    from ..db import get_active_project

    active = get_active_project()
    return {"project": _row_to_project(row, active).model_dump(mode="json")}


@api_router.put("/{project_id}")
async def edit_project(project_id: str, body: ProjectUpdate) -> dict:
    row = fetch_project(project_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"project '{project_id}' not found")
    fields = body.model_dump(exclude_none=True)
    if fields:
        fields["updated_at"] = datetime.utcnow().isoformat()
        update_project(project_id, **fields)
        # if this is the active project, reload settings
        from ..db import get_active_project

        if get_active_project() == project_id:
            controller._load_project_settings(project_id)
    row = fetch_project(project_id)
    from ..db import get_active_project

    return {"status": "ok", "project": _row_to_project(row, get_active_project()).model_dump(mode="json")}  # type: ignore[arg-type]


@api_router.delete("/{project_id}")
async def remove_project(project_id: str) -> dict:
    from ..db import get_active_project

    active = get_active_project()
    if active == project_id and controller.state.running:
        raise HTTPException(status_code=409, detail="cannot delete the active project while the loop is running")
    delete_project(project_id)
    return {"status": "ok"}


@api_router.post("/{project_id}/activate")
async def activate_project(project_id: str) -> dict:
    """Switch the active project. Stops the loop if running."""
    row = fetch_project(project_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"project '{project_id}' not found")
    msg = await controller.switch_project(project_id)
    return {"status": msg, "active_project_id": project_id}
