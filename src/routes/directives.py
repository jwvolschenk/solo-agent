"""GET/POST /api/agent/directives, GET/PATCH /api/agent/directives/{id}.

The directive queue is the human -> agent feedback channel. It lives in
directives.md (parsed by directives.py) and is mirrored into SQLite for history.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..db import fetch_directives
from ..directives import append_directive, load_directives, update_directive_status
from ..models import DirectiveCreate, DirectiveUpdate

api_router = APIRouter(prefix="/api/agent", tags=["directives"])


@api_router.get("/directives")
async def get_directives() -> dict:
    """List all directives (source of truth: directives.md, mirrored to DB)."""
    directives = load_directives()
    return {
        "count": len(directives),
        "directives": [d.model_dump(mode="json") for d in directives],
    }


@api_router.post("/directives", status_code=201)
async def post_directive(body: DirectiveCreate) -> dict:
    """Queue a new directive for the agent."""
    d = append_directive(body.priority, body.text)
    return {"status": "ok", "directive": d.model_dump(mode="json")}


@api_router.get("/directives/{did}")
async def get_directive(did: str) -> dict:
    directives = load_directives()
    d = next((x for x in directives if x.id == did), None)
    if d is None:
        raise HTTPException(status_code=404, detail=f"directive '{did}' not found")
    return {"directive": d.model_dump(mode="json")}


@api_router.patch("/directives/{did}")
async def patch_directive(did: str, body: DirectiveUpdate) -> dict:
    """Advance a directive's status (pending -> acknowledged -> done).

    Rewrites the status: line in directives.md so the file channel stays in sync
    with the DB. Returns 404 if the directive doesn't exist.
    """
    d = update_directive_status(did, body.status)
    if d is None:
        raise HTTPException(status_code=404, detail=f"directive '{did}' not found")
    return {"status": "ok", "directive": d.model_dump(mode="json")}


@api_router.get("/directives/history/all")
async def get_directive_history() -> dict:
    """Full status-transition audit across all directives (from SQLite)."""
    rows = fetch_directives()
    return {
        "directives": [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "priority": r["priority"],
                "text": r["text"],
                "status": r["current_status"],
                "first_seen_at": r["first_seen_at"],
            }
            for r in rows
        ]
    }
