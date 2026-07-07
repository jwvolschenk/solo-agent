"""WS /ws — live dashboard updates.

On connect, the client receives the current snapshot immediately, then gets
pushed updates as the collector / state watcher / orchestrator emit them.
Falls back gracefully: if the WS is closed, the frontend can poll the REST API.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..collector import collector
from ..models import DashboardSnapshot
from ..ws import manager

log = logging.getLogger("solo.ws.route")

api_router = APIRouter(tags=["ws"])


@api_router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    # send an immediate snapshot so the client isn't blank until the next poll
    try:
        initial = collector.snapshot()
        await ws.send_text(initial.model_dump_json())
    except Exception as e:
        log.debug("initial ws send failed: %s", e)

    try:
        # We don't expect inbound messages, but we must read to detect disconnects.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("ws loop ended: %s", e)
    finally:
        await manager.disconnect(ws)
