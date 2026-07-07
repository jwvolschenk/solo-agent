"""GET /api/health — combined dashboard + llama-server health."""

from __future__ import annotations

from fastapi import APIRouter

from ..collector import collector

api_router = APIRouter(prefix="/api", tags=["health"])


@api_router.get("/health")
async def get_health() -> dict:
    """Return the dashboard's view of llama-server health.

    Always returns 200 (even when the server is offline) so the frontend's
    health-check poll never throws; the payload's ``status`` field carries the truth.
    """
    h = collector.health
    return {
        "status": h.status,
        "message": h.message,
        "http_status": h.http_status,
        "slots_idle": h.slots_idle,
        "slots_processing": h.slots_processing,
        "checked_at": h.checked_at.isoformat(),
    }
