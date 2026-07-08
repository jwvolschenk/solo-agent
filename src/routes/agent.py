"""GET/POST /api/agent/activity — agent activity feed."""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..models import ActivityEvent
from ..db import fetch_activity, get_active_project, insert_activity

api_router = APIRouter(prefix="/api/agent", tags=["agent"])


@api_router.get("/activity")
async def get_activity(limit: int = Query(50, ge=1, le=500), project_id: Optional[str] = None) -> dict:
    """Recent agent activity entries, newest first. Defaults to the active
    project; pass project_id explicitly to look at a different one, or an
    empty active project (no project set yet) sees everything unfiltered."""
    pid = project_id if project_id is not None else (get_active_project() or "")
    rows = fetch_activity(limit=limit, project_id=pid)
    events = []
    for r in rows:
        try:
            meta = json.loads(r["metadata_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        events.append(
            {
                "id": r["id"],
                "type": r["type"],
                "message": r["message"],
                "timestamp": r["timestamp"],
                "metadata": meta,
                "project_id": r["project_id"],
            }
        )
    return {"count": len(events), "events": events}


@api_router.post("/activity", status_code=201)
async def post_activity(event: ActivityEvent) -> dict:
    """Agent (or anything) posts an activity event. Returns the stored id."""
    if not event.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    if event.project_id is None:
        event.project_id = get_active_project()
    row_id = insert_activity(event)
    return {"status": "ok", "id": row_id}
