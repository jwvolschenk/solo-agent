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
