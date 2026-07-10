"""Runner tests — mock agent stub stands in for `opencode run`."""

import os

import pytest

from src.config import settings


@pytest.fixture(autouse=True)
def point_agent_command_at_mock(mock_agent_script, tmp_settings, monkeypatch):
    """Override settings.agent_command to call our mock agent script + isolate DB."""
    monkeypatch.setattr(
        settings, "agent_command",
        f'{mock_agent_script} {{prompt}}',
    )
    # tight timeout for the hang test
    monkeypatch.setattr(settings, "per_goal_timeout_sec", 3.0)


@pytest.mark.asyncio
async def test_runner_ok_mode_detects_success(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "ok")
    from src.orchestrator import runner
    # re-import budget to reset
    from src.orchestrator import budget as budgetmod
    budgetmod.budget.cycle_tokens = 0

    result = await runner.run_goal("do something")
    assert result.ok is True
    assert result.timed_out is False
    assert "DONE:" in result.final_message
    assert result.tokens_used == 150  # 100 prompt + 50 completion
    assert len(result.events) >= 2


@pytest.mark.asyncio
async def test_runner_error_mode_detected_from_event_not_exitcode(monkeypatch):
    """OpenCode bug: exits 0 even on session error. We must catch it via events."""
    monkeypatch.setenv("AGENT_MODE", "error")
    from src.orchestrator import runner

    result = await runner.run_goal("do something")
    assert result.ok is False
    assert result.error is not None
    assert "boom" in result.error.lower() or "session error" in result.error.lower()


@pytest.mark.asyncio
async def test_runner_hang_is_killed_by_timeout(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "hang")
    from src.orchestrator import runner

    result = await runner.run_goal("do something")
    assert result.timed_out is True
    assert result.ok is False


@pytest.mark.asyncio
async def test_runner_missing_binary_returns_clean_error(monkeypatch):
    monkeypatch.setattr(settings, "agent_command", "/nonexistent/binary {prompt}")
    from src.orchestrator import runner

    result = await runner.run_goal("do something")
    assert result.ok is False
    assert "not found" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_runner_does_not_double_count_tokens(monkeypatch):
    """Regression: tokens used to be counted twice -- once live per step_finish,
    once again from the post-hoc full-buffer parse."""
    monkeypatch.setenv("AGENT_MODE", "ok")
    from src.orchestrator import runner
    from src.orchestrator import budget as budgetmod
    budgetmod.budget.cycle_tokens = 0

    result = await runner.run_goal("do something")
    assert result.tokens_used == 150
    assert budgetmod.budget.cycle_tokens == 150  # not 300


@pytest.mark.asyncio
async def test_runner_does_not_double_emit_thin_activity(monkeypatch):
    """Regression: activity used to be emitted once live and once again from
    the post-hoc full-buffer parse."""
    monkeypatch.setenv("AGENT_MODE", "tool")
    from src.orchestrator import runner

    seen = []
    runner.set_activity_hook(lambda t, m, meta: seen.append(m))
    try:
        await runner.run_goal("do something")
    finally:
        runner.set_activity_hook(None)

    bash_msgs = [m for m in seen if m.startswith("$ npm test")]
    assert len(bash_msgs) == 1


@pytest.mark.asyncio
async def test_runner_emits_running_then_completed_transcript_for_correlated_tool(monkeypatch):
    """A tool_use event with a callID gets a 'running' entry, upgraded in
    place to 'completed' with output once the matching completed event
    arrives -- not two separate entries."""
    monkeypatch.setenv("AGENT_MODE", "tool")
    from src.orchestrator import runner
    from src import transcript

    transcript.clear()
    result = await runner.run_goal("do something")
    assert result.ok is True

    snap = transcript.snapshot()
    tool_entries = [e for e in snap if e.id == "call-1"]
    assert len(tool_entries) == 1  # updated in place, not appended twice
    assert tool_entries[0].status == "completed"
    assert tool_entries[0].output == "5 passed"


@pytest.mark.asyncio
async def test_runner_records_readonly_tool_without_correlation_id(monkeypatch):
    """A completed tool_use with no callID still gets recorded -- no running
    phase, straight to completed -- graceful degradation, not a dropped event."""
    monkeypatch.setenv("AGENT_MODE", "tool")
    from src.orchestrator import runner
    from src import transcript

    transcript.clear()
    await runner.run_goal("do something")

    snap = transcript.snapshot()
    read_entries = [e for e in snap if e.tool == "read"]
    assert len(read_entries) == 1
    assert read_entries[0].readonly is True
    assert read_entries[0].status == "completed"


@pytest.mark.asyncio
async def test_runner_records_full_text_event_uncapped_by_thin_limit(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "tool")
    from src.orchestrator import runner
    from src import transcript

    transcript.clear()
    await runner.run_goal("do something")
    text_entries = [e for e in transcript.snapshot() if e.kind == "text"]
    assert any(e.text == "DONE: ran tests" for e in text_entries)


@pytest.mark.asyncio
async def test_runner_tags_transcript_events_with_provided_session_id(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "tool")
    from src.orchestrator import runner
    from src import transcript

    transcript.clear()
    await runner.run_goal("do something", session_id="fixed-session-123")
    snap = transcript.snapshot()
    assert snap
    assert all(e.session_id == "fixed-session-123" for e in snap)


def test_new_session_id_is_public_and_prefixed():
    from src.orchestrator import runner
    assert runner.new_session_id().startswith("solo-")


@pytest.mark.asyncio
async def test_runner_handles_stdout_line_over_64k(monkeypatch):
    """Regression: NDJSON lines > 64 KiB must not crash (default limit is 4 MiB)."""
    monkeypatch.setenv("AGENT_MODE", "huge")
    from src.orchestrator import runner

    result = await runner.run_goal("stress oversized stdout line")
    assert result.ok is True
    assert "DONE: huge line ok" in result.final_message

