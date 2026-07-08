"""Controller tests — state machine, kill switch, budget trip."""

import pytest

from src.config import settings


@pytest.fixture
def controller_with_tmp_repo(tmp_target_repo, monkeypatch, tmp_settings):
    """A fresh controller pointed at a tmp git repo + tmp state/db."""
    monkeypatch.setattr(settings, "project_path", tmp_target_repo)
    monkeypatch.setattr(settings, "base_branch", "main")
    monkeypatch.setattr(settings, "work_branch", "solo-agent/test")
    monkeypatch.setattr(settings, "verify_command", "true")  # always passes
    # point the agent at a stub that succeeds
    monkeypatch.setattr(settings, "agent_command", "true {prompt}")  # no-op succeeds

    # reset the singleton controller so it picks up the new settings
    from src.orchestrator import controller as ctlmod
    from src.orchestrator import budget as budgetmod
    from src.orchestrator import guardrails
    ctlmod.controller = ctlmod.OrchestratorController()
    budgetmod.budget = budgetmod.TokenCounter()
    guardrails.kill_switch.clear()
    guardrails.loop_detector.reset()
    yield ctlmod.controller


@pytest.mark.asyncio
async def test_start_requires_goal(controller_with_tmp_repo, monkeypatch):
    """Without a goal the loop refuses to start — there's nothing to drive toward."""
    monkeypatch.setattr(settings, "goal", "")
    c = controller_with_tmp_repo
    c.state.cycle_number = 0
    msg = await c.start()
    assert "no goal" in msg
    assert c.state.phase == "error"


@pytest.mark.asyncio
async def test_start_auto_inits_non_git_dir(controller_with_tmp_repo, monkeypatch, tmp_path):
    """An empty/non-git project_path gets git-init'd automatically (from-scratch case)."""
    empty = tmp_path / "greenfield"
    empty.mkdir()
    monkeypatch.setattr(settings, "project_path", empty)
    monkeypatch.setattr(settings, "goal", "build something")
    monkeypatch.setattr(settings, "agent_command", "true {prompt}")  # no-op agent
    monkeypatch.setattr(settings, "verify_command", "")  # no gate
    from src.orchestrator import controller as ctlmod
    c = ctlmod.controller
    msg = await c.start()
    # it should start (not error on missing repo) and git should now be initialized
    assert msg == "started"
    assert (empty / ".git").exists()
    # stop immediately so we don't run a real cycle
    await c.stop()


@pytest.mark.asyncio
async def test_kill_switch_stops_loop(controller_with_tmp_repo):
    from src.orchestrator import guardrails
    c = controller_with_tmp_repo
    # engage kill switch before starting the loop task
    guardrails.kill_switch.request_stop("test")
    # run one cycle manually
    await c._run_cycle()
    assert c.state.phase == "stopped"
    guardrails.kill_switch.clear()


@pytest.mark.asyncio
async def test_token_usage_never_blocks_loop(controller_with_tmp_repo):
    """No budgets on a local model — tokens are counted for display but never pause."""
    from src.orchestrator import budget as budgetmod
    c = controller_with_tmp_repo
    # simulate heavy token usage
    budgetmod.budget.add(10_000_000)
    assert budgetmod.budget.cycle_tokens == 10_000_000
    # but it never blocks — the loop always runs 24/7
    assert budgetmod.budget.ok is True
    assert budgetmod.budget.breached == ""


def test_controller_persists_and_resumes(controller_with_tmp_repo):
    """State written via _persist is readable via get_orch_state."""
    from src.db import get_orch_state

    c = controller_with_tmp_repo
    c.state.cycle_number = 42
    c.state.phase = "paused"
    c._persist()

    persisted = get_orch_state()
    assert persisted["cycle_number"] == 42
    assert persisted["phase"] == "paused"


@pytest.mark.asyncio
async def test_run_cycle_brackets_task_execution_with_transcript_session_markers(controller_with_tmp_repo, tmp_target_repo):
    """Each backlog task execution should show up in the transcript as a
    session_start/session_end pair, so the dashboard can group tool calls."""
    from src import transcript

    c = controller_with_tmp_repo
    (tmp_target_repo / "backlog.md").write_text("- [ ] do the thing\n")
    transcript.clear()

    await c._run_cycle()

    assert c.state.phase == "idle"
    snap = transcript.snapshot()
    starts = [e for e in snap if e.kind == "session_start"]
    ends = [e for e in snap if e.kind == "session_end"]
    assert len(starts) == 1
    assert len(ends) == 1
    assert starts[0].task == "do the thing"
    assert starts[0].session_id == ends[0].session_id
    assert starts[0].status == "running"
    assert ends[0].status in ("completed", "error")


@pytest.mark.asyncio
async def test_switch_project_clears_transcript(controller_with_tmp_repo, tmp_path):
    from datetime import datetime

    from src import transcript
    from src.db import insert_project
    from src.models import TranscriptEvent

    c = controller_with_tmp_repo
    transcript._buffer.append(TranscriptEvent(id="e1", kind="tool", session_id="s1"))

    other = tmp_path / "other-project"
    other.mkdir()
    now = datetime.utcnow().isoformat()
    insert_project({
        "id": "other", "name": "Other", "goal": "build x", "project_path": str(other),
        "verify_command": "", "work_branch": "solo-agent/auto", "stop_after_cycle": 0,
        "created_at": now, "updated_at": now,
    })

    await c.switch_project("other")
    assert transcript.snapshot() == []
