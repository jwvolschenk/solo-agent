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
async def test_reflect_empty_injects_fallback_task_instead_of_pausing(
    controller_with_tmp_repo, tmp_target_repo, mock_agent_script, monkeypatch
):
    """When the backlog is empty and REFLECT finds no new work, the orchestrator
    must keep the 24/7 loop moving by seeding a coarse theme for PLAN to decompose,
    rather than pausing and waiting for a human."""
    from src import transcript
    from src.orchestrator import artifacts
    from src.state_reader import parse_tasks

    # the default "true {prompt}" stub emits no stdout, so run_goal treats it
    # as a failed (non-"ok") session and the REFLECT branch never even reaches
    # the "did reflect add new tasks?" check. Use the mock agent script in "ok"
    # mode instead: it emits a real DONE: message (a genuine success), but
    # writes nothing to backlog.md -- exactly "reflect succeeded, found nothing".
    monkeypatch.setattr(settings, "agent_command", f"{mock_agent_script} {{prompt}}")
    monkeypatch.setenv("AGENT_MODE", "ok")

    c = controller_with_tmp_repo
    # no pending tasks -> REFLECT path runs.
    (tmp_target_repo / "backlog.md").write_text("# Backlog\n")
    c.state.running = True  # simulate an active loop, as start() would set
    transcript.clear()

    await c._run_cycle()

    assert c.state.phase == "idle"
    assert c.state.running is True  # must NOT be force-stopped just because reflect found nothing
    candidates = parse_tasks(artifacts.read_candidates())
    pending_candidates = [t for t in candidates if t.status == "todo"]
    assert len(pending_candidates) == 1
    assert "orchestrator seed" in pending_candidates[0].text
    backlog_pending = [
        t for t in parse_tasks(artifacts.read_backlog()) if t.status == "todo"
    ]
    assert backlog_pending == []

    snap = transcript.snapshot()
    starts = [e for e in snap if e.kind == "session_start"]
    assert [e.task for e in starts] == ["reflect", "plan"]


@pytest.mark.asyncio
async def test_reflect_path_runs_separate_plan_session(
    controller_with_tmp_repo, tmp_target_repo, monkeypatch
):
    """REFLECT and PLAN must be distinct sessions; PLAN should decompose coarse
    reflect output before the next EXECUTE cycle."""
    import textwrap
    from src import transcript
    from src.orchestrator import artifacts
    from src.state_reader import parse_tasks

    phase_agent = tmp_target_repo / "phase_agent.py"
    repo = str(tmp_target_repo)
    phase_agent.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, sys
        from pathlib import Path
        prompt = sys.argv[-1] if len(sys.argv) > 1 else ""
        root = Path({repo!r})
        candidates = root / "backlog-candidates.md"
        backlog = root / "backlog.md"
        if "PHASE: REFLECT" in prompt:
            candidates.write_text(
                "# Backlog Candidates\\n\\n- [ ] Build entire auth system end-to-end\\n",
                encoding="utf-8",
            )
            msg = "DONE: added 1 coarse candidate"
        elif "PHASE: PLAN" in prompt:
            backlog.write_text(
                "# Backlog\\n\\n"
                "- [ ] Add User model and migration\\n"
                "- [ ] Add login endpoint with tests\\n"
                "- [ ] Add logout endpoint with tests\\n",
                encoding="utf-8",
            )
            candidates.write_text("# Backlog Candidates\\n\\n", encoding="utf-8")
            msg = "DONE: 3 ready tasks, first is User model"
        else:
            msg = "DONE: noop"
        print(json.dumps({{"type": "text", "part": {{"type": "text", "text": msg}}}}))
        print(json.dumps({{"type": "step_finish", "part": {{"reason": "stop", "tokens": {{"total": 10}}}}}}))
    """))
    phase_agent.chmod(0o755)
    monkeypatch.setattr(settings, "agent_command", f"{phase_agent} {{prompt}}")

    c = controller_with_tmp_repo
    (tmp_target_repo / "backlog.md").write_text("# Backlog\n")
    transcript.clear()

    await c._run_cycle()

    snap = transcript.snapshot()
    starts = [e for e in snap if e.kind == "session_start"]
    assert [e.task for e in starts] == ["reflect", "plan"]

    pending = [t for t in parse_tasks(artifacts.read_backlog()) if t.status == "todo"]
    assert len(pending) == 3
    assert "User model" in pending[0].text
    assert parse_tasks(artifacts.read_candidates()) == []
    assert c.state.phase == "idle"


@pytest.mark.asyncio
async def test_execute_only_one_backlog_task_per_cycle(
    controller_with_tmp_repo, tmp_target_repo, mock_agent_script, monkeypatch,
):
    """With multiple pending items, each cycle executes exactly one."""
    from src import transcript

    monkeypatch.setattr(settings, "agent_command", f"{mock_agent_script} {{prompt}}")
    monkeypatch.setenv("AGENT_MODE", "ok")

    c = controller_with_tmp_repo
    (tmp_target_repo / "backlog.md").write_text(
        "- [ ] first task\n- [ ] second task\n- [ ] third task\n"
    )
    transcript.clear()

    await c._run_cycle()

    starts = [e for e in transcript.snapshot() if e.kind == "session_start"]
    assert len(starts) == 1
    assert starts[0].task == "first task"


@pytest.mark.asyncio
async def test_relocate_stale_seed_skipped_by_executor(
    controller_with_tmp_repo, tmp_target_repo, mock_agent_script, monkeypatch,
):
    """Legacy orchestrator seeds in backlog.md must not reach EXECUTE."""
    from src import transcript
    from src.orchestrator import artifacts

    monkeypatch.setattr(settings, "agent_command", f"{mock_agent_script} {{prompt}}")
    monkeypatch.setenv("AGENT_MODE", "ok")

    c = controller_with_tmp_repo
    (tmp_target_repo / "backlog.md").write_text(
        "# Backlog\n\n"
        "- [ ] (orchestrator seed, cycle 1) old seed line\n"
        "- [ ] real executable task\n"
    )
    (tmp_target_repo / "backlog-candidates.md").write_text("# Backlog Candidates\n\n")
    transcript.clear()

    await c._run_cycle()

    starts = [e for e in transcript.snapshot() if e.kind == "session_start"]
    assert len(starts) == 1
    assert starts[0].task == "real executable task"
    from src.state_reader import parse_tasks
    seeds = [
        t.text for t in parse_tasks(artifacts.read_candidates()) if t.status == "todo"
    ]
    assert len(seeds) == 1
    assert "orchestrator seed" in seeds[0]


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


@pytest.mark.asyncio
async def test_switch_project_notifies_already_connected_clients(controller_with_tmp_repo, tmp_path):
    """A project switch must not just clear the server-side buffer -- it must
    also tell already-connected dashboard clients to drop their own stale
    transcript state, or a tab open across the switch keeps showing the
    previous project's session cards until it happens to reconnect."""
    from datetime import datetime

    from src import transcript
    from src.db import insert_project

    c = controller_with_tmp_repo
    saved_broadcast = transcript._broadcast
    received = []

    async def fake_broadcast(payload):
        received.append(payload)

    transcript.set_broadcast(fake_broadcast)
    try:
        other = tmp_path / "other-project-2"
        other.mkdir()
        now = datetime.utcnow().isoformat()
        insert_project({
            "id": "other2", "name": "Other2", "goal": "build y", "project_path": str(other),
            "verify_command": "", "work_branch": "solo-agent/auto", "stop_after_cycle": 0,
            "created_at": now, "updated_at": now,
        })

        await c.switch_project("other2")

        assert {"kind": "transcript_backfill", "events": []} in received
    finally:
        transcript.set_broadcast(saved_broadcast)
