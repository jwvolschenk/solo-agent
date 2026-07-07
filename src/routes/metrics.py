"""GET /api/metrics, GET /api/metrics/history — current + historical metrics."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..collector import collector
from ..db import fetch_metrics_history

api_router = APIRouter(prefix="/api", tags=["metrics"])


@api_router.get("/metrics")
async def get_metrics() -> dict:
    """Current in-memory metrics snapshot (last successful poll)."""
    m = collector.metrics
    if m is None:
        return {"status": "no_data", "metrics": None}
    return {
        "status": "ok",
        "health": collector.health.status,
        "metrics": m.model_dump(mode="json"),
    }


@api_router.get("/metrics/history")
async def get_metrics_history(
    range: str = Query("1h", pattern="^(1h|6h|24h)$"),
) -> dict:
    """Historical metrics from the SQLite ring buffer.

    ``range`` selects the window; we map it to minutes.
    """
    minutes = {"1h": 60, "6h": 360, "24h": 1440}[range]
    rows = fetch_metrics_history(minutes=minutes)
    return {
        "range": range,
        "count": len(rows),
        "points": [
            {
                "captured_at": r["captured_at"],
                "prefill_tps": r["prompt_tokens_seconds"],
                "decode_tps": r["predicted_tokens_seconds"],
                "prompt_tokens_total": r["prompt_tokens_total"],
                "tokens_predicted_total": r["tokens_predicted_total"],
                "requests_processing": r["requests_processing"],
                "requests_deferred": r["requests_deferred"],
            }
            for r in rows
        ],
    }
