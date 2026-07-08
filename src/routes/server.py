"""GET /api/slots, GET /api/props — proxied llama-server introspection."""

from __future__ import annotations

import httpx
from fastapi import APIRouter

from ..collector import _auth_headers, collector
from ..config import settings
from ..parsers import parse_props, parse_slots

api_router = APIRouter(prefix="/api", tags=["server"])


@api_router.get("/slots")
async def get_slots() -> dict:
    """Current slot status. Returns the last polled snapshot (no extra fetch)."""
    return {
        "status": collector.health.status,
        "slots": [s.model_dump(mode="json") for s in collector.slots],
    }


@api_router.get("/props")
async def get_props() -> dict:
    """Fetch model info + generation params live from /props.

    Props change rarely but we fetch on demand rather than caching, so the
    dashboard always reflects the running model. On failure returns offline.
    """
    base = settings.llama_server_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout, headers=_auth_headers()) as c:
            r = await c.get(f"{base}/props")
            r.raise_for_status()
            props = parse_props(r.json())
            return {"status": "ok", "props": props.model_dump(mode="json")}
    except (httpx.HTTPError, OSError, ValueError) as e:
        return {"status": "offline", "error": str(e), "props": None}
