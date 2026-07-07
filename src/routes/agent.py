"""GET/POST /api/agent/activity — agent activity feed."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query

from ..models import ActivityEvent
from ..db import fetch_activity, insert_activity

api_router = APIRouter(prefix="/api/agent", tags=["agent"])


@api_router.get("/activity")
async def get_activity(limit: int = Query(50, ge=1, le=500)) -> dict:
    """Recent agent activity entries, newest first."""
    rows = fetch_activity(limit=limit)
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
            }
        )
    return {"count": len(events), "events": events}


@api_router.post("/activity", status_code=201)
async def post_activity(event: ActivityEvent) -> dict:
    """Agent (or anything) posts an activity event. Returns the stored id."""
    if not event.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    row_id = insert_activity(event)
    return {"status": "ok", "id": row_id}
