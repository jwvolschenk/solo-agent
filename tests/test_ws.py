"""WebSocket endpoint tests — initial snapshot + transcript backfill on connect."""

import pytest

from src import transcript
from src.models import TranscriptEvent


@pytest.fixture(autouse=True)
def reset_transcript_buffer():
    transcript.clear()
    yield
    transcript.clear()


def test_ws_sends_snapshot_then_transcript_backfill(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        # first message is the DashboardSnapshot -- has no "kind" field
        assert "kind" not in first
        second = ws.receive_json()
        assert second["kind"] == "transcript_backfill"
        assert second["events"] == []


def test_ws_backfill_reflects_current_transcript_buffer(client):
    # direct buffer append (no broadcast needed) is enough to test backfill
    transcript._buffer.append(TranscriptEvent(id="e1", kind="tool", session_id="s1"))

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # snapshot
        backfill = ws.receive_json()
        assert backfill["kind"] == "transcript_backfill"
        assert len(backfill["events"]) == 1
        assert backfill["events"][0]["id"] == "e1"
