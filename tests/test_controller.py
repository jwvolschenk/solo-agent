"""Controller tests — state machine, kill switch, budget trip."""

import pytest

from src.config import settings


@pytest.fixture
def controller_with_tmp_repo(tmp_target_repo, monkeypatch, tmp_settings):
    """A fresh controller pointed at a tmp git repo + tmp state/db."""
    monkeypatch.setattr(settings, "target_repo", tmp_target_repo)
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
    budgetmod.budget = budgetmod.BudgetState()
    guardrails.kill_switch.clear()
    guardrails.loop_detector.reset()
    yield ctlmod.controller


@pytest.mark.asyncio
async def test_start_requires_repo(controller_with_tmp_repo, monkeypatch):
    # point at a non-repo path
    monkeypatch.setattr(settings, "target_repo", "/tmp")
    c = controller_with_tmp_repo
    c.state.cycle_number = 0
    msg = await c.start()
    assert "not a git repository" in msg
    assert c.state.phase == "error"


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
async def test_budget_breach_pauses(controller_with_tmp_repo, monkeypatch):
    from src.orchestrator import budget as budgetmod
    # set a tiny budget so any token usage trips it
    budgetmod.budget.cycle_limit = 1
    budgetmod.budget.day_limit = 1_000_000
    c = controller_with_tmp_repo
    # simulate token usage pushing over the cycle limit
    budgetmod.budget.add(100)  # already over cycle_limit of 1
    assert budgetmod.budget.ok is False
    assert budgetmod.budget.breached == "cycle"


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
