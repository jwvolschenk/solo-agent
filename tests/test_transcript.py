"""Transcript ring buffer tests — Track B (rich, in-memory-only) activity."""

import pytest

from src import transcript
from src.models import TranscriptEvent


@pytest.fixture(autouse=True)
def reset_transcript():
    """Isolate each test's buffer + broadcast hook, restoring whatever
    broadcast callback was registered before (e.g. by importing src.main
    in another test module) so this file can't leak state into others."""
    saved_broadcast = transcript._broadcast
    transcript.clear()
    transcript.set_broadcast(None)
    yield
    transcript.clear()
    transcript.set_broadcast(saved_broadcast)


def _event(id="e1", **kw) -> TranscriptEvent:
    defaults = dict(id=id, kind="tool", status="completed", session_id="s1")
    defaults.update(kw)
    return TranscriptEvent(**defaults)


@pytest.mark.asyncio
async def test_record_appends_and_snapshot_returns_in_order():
    await transcript.record(_event(id="e1"))
    await transcript.record(_event(id="e2"))
    ids = [e.id for e in transcript.snapshot()]
    assert ids == ["e1", "e2"]


@pytest.mark.asyncio
async def test_update_replaces_existing_entry_by_id():
    await transcript.record(_event(id="e1", status="running"), op="append")
    await transcript.record(_event(id="e1", status="completed", output="ok"), op="update")
    snap = transcript.snapshot()
    assert len(snap) == 1
    assert snap[0].status == "completed"
    assert snap[0].output == "ok"


@pytest.mark.asyncio
async def test_update_with_unknown_id_falls_back_to_append():
    await transcript.record(_event(id="never-seen", status="completed"), op="update")
    snap = transcript.snapshot()
    assert len(snap) == 1
    assert snap[0].id == "never-seen"


@pytest.mark.asyncio
async def test_buffer_is_bounded():
    for i in range(transcript.MAX_EVENTS + 50):
        await transcript.record(_event(id=f"e{i}"))
    assert len(transcript.snapshot()) == transcript.MAX_EVENTS
    # oldest events evicted -- the first surviving one is e50
    assert transcript.snapshot()[0].id == "e50"


def test_clear_empties_buffer():
    transcript._buffer.append(_event(id="e1"))
    transcript.clear()
    assert transcript.snapshot() == []


@pytest.mark.asyncio
async def test_record_broadcasts_via_registered_callback():
    received = []

    async def fake_broadcast(payload):
        received.append(payload)

    transcript.set_broadcast(fake_broadcast)
    await transcript.record(_event(id="e1"))
    assert len(received) == 1
    assert received[0]["kind"] == "transcript_event"
    assert received[0]["op"] == "append"
    assert received[0]["event"]["id"] == "e1"


@pytest.mark.asyncio
async def test_broadcast_failure_is_swallowed():
    async def bad_broadcast(payload):
        raise RuntimeError("ws gone")

    transcript.set_broadcast(bad_broadcast)
    # must not raise
    await transcript.record(_event(id="e1"))
    assert len(transcript.snapshot()) == 1
