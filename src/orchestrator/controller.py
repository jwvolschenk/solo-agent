"""Ralph loop controller — the autonomous self-improvement state machine.

Phases per cycle:
    IDLE -> REFLECT -> PLAN -> EXECUTE (per task) -> VERIFY -> RECORD -> back to REFLECT

There are no token budgets (local model, runs 24/7). Guardrails consulted between
phases: kill switch, no-progress detector, diminishing-returns detector. Any trip
pauses the loop. Token usage is counted for display only (see budget.py).

State is persisted to SQLite (orch_state) so an interrupted loop resumes cleanly.
The controller runs as an asyncio background task started by the FastAPI lifespan.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from ..config import settings
from ..db import (
    get_active_project, get_orch_state, insert_activity, insert_cycle,
    set_orch_state, update_cycle, fetch_project, set_active_project, update_project,
)
from ..models import ActivityEvent, CycleRecord, OrchestratorState
from ..state_reader import parse_tasks
from . import artifacts, budget, guardrails, prompts
from .runner import set_activity_hook
from .git_ops import (
    commit_all,
    diff_stat,
    ensure_work_branch,
    is_repo,
    ensure_repo,
    revert_to,
    snapshot,
    stage_all,
)
from .runner import run_goal
from .verify import is_enabled as verify_enabled, run_verify

log = logging.getLogger("solo.controller")


class OrchestratorController:
    """Drives the Ralph loop. Single instance, started in app lifespan.

    Project-aware: holds an active_project_id and reads project-specific config
    (goal, project_path, verify_command) from the projects table. State is
    persisted per-project so each project has its own cycle counter, phase, and
    stall history.
    """

    def __init__(self) -> None:
        self.state = OrchestratorState()
        self._task: Optional[asyncio.Task] = None
        self._current_cycle_id: Optional[int] = None
        self.active_project_id: Optional[str] = None
        # restore the active project + its state on startup
        self.active_project_id = get_active_project()
        if self.active_project_id:
            self._load_project_settings(self.active_project_id)
        self._resume()

    # ---- project management ---------------------------------------------------

    def _load_project_settings(self, project_id: str) -> bool:
        """Load a project's config into the live settings object so all the
        downstream consumers (git_ops, runner, verify, artifacts) pick it up.
        Returns False if the project doesn't exist."""
        row = fetch_project(project_id)
        if row is None:
            return False
        settings.project_path = type(settings.project_path)(row["project_path"])
        settings.goal = row["goal"]
        settings.verify_command = row["verify_command"]
        settings.work_branch = row["work_branch"]
        return True

    async def switch_project(self, project_id: str) -> str:
        """Switch the active project. Stops the loop if running, loads the new
        project's config + state into memory."""
        # stop current loop if running
        if self.state.running:
            await self.stop()
        # persist current state to the old project's key before switching
        if self.active_project_id and self.active_project_id != project_id:
            self._persist()
        # load new project
        if not self._load_project_settings(project_id):
            return f"project {project_id} not found"
        self.active_project_id = project_id
        set_active_project(project_id)
        # reset transient state
        budget.reset_cycle()
        guardrails.loop_detector.reset()
        guardrails.no_progress.reset()
        # resume the new project's persisted state
        self._resume()
        self.state.project_id = project_id
        self._persist()
        log.info("switched to project %s (path=%s)", project_id, settings.project_path)
        return "switched"

    def _resume(self) -> None:
        """Restore state for the active project from SQLite."""
        pid = self.active_project_id or ""
        persisted = get_orch_state(pid)
        if persisted:
            try:
                self.state = OrchestratorState(**persisted)
                log.info(
                    "resumed state for project %s: cycle=%d phase=%s",
                    pid or "(none)", self.state.cycle_number, self.state.phase,
                )
            except Exception as e:
                log.warning("could not resume state (%s); starting fresh", e)
                self.state = OrchestratorState()
        else:
            self.state = OrchestratorState()
        # never auto-resume a mid-run; require explicit start
        if self.state.phase not in ("idle", "stopped", "paused", "error"):
            self.state.phase = "paused"
            self.state.running = False
        self.state.project_id = pid or None

    # ---- lifecycle -----------------------------------------------------------

    async def start(self) -> str:
        """Begin cycling. Requires an active project with a goal."""
        # require a goal
        if not settings.goal.strip():
            msg = "no goal set — set one on the active project before starting"
            log.error(msg)
            self.state.phase = "error"
            self.state.last_error = msg
            self._persist()
            return msg

        # load the project's stop_after_cycle flag into state
        if self.active_project_id:
            row = fetch_project(self.active_project_id)
            if row:
                self.state.stop_after_cycle = bool(row["stop_after_cycle"])

        # auto-init git if needed (from-scratch case)
        await ensure_repo()
        await ensure_work_branch()
        artifacts.ensure_artifacts()

        if self._task is not None and not self._task.done():
            return "already running"
        guardrails.kill_switch.clear()
        # wire the runner's tool calls into the activity feed
        set_activity_hook(lambda t, m, meta: insert_activity(ActivityEvent(type=t, message=m, metadata=meta)))  # type: ignore[arg-type]
        self.state.running = True
        self.state.phase = "idle"
        self.state.last_error = None
        self._persist()
        self._task = asyncio.create_task(self._loop(), name="orchestrator")
        log.info("orchestrator started (project=%s, goal=%d chars)", settings.project_path, len(settings.goal))
        return "started"

    async def pause(self) -> str:
        """Pause between cycles (current cycle finishes its phase)."""
        self.state.running = False
        self.state.phase = "paused"
        self._persist()
        log.info("orchestrator paused")
        return "paused"

    async def resume(self) -> str:
        """Resume from pause."""
        if self._task is not None and not self._task.done():
            return "already running"
        self.state.running = True
        self.state.phase = "idle"
        self._persist()
        self._task = asyncio.create_task(self._loop(), name="orchestrator")
        return "resumed"

    async def stop(self) -> str:
        """Hard stop — engages kill switch (honored mid-cycle) and cancels the task."""
        guardrails.kill_switch.request_stop("manual stop")
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.state.running = False
        self.state.phase = "stopped"
        guardrails.kill_switch.clear()
        set_activity_hook(None)  # stop logging tool calls
        self._persist()
        log.info("orchestrator stopped")
        return "stopped"

    # ---- the loop ------------------------------------------------------------

    async def _loop(self) -> None:
        """Outer loop: one full cycle per iteration, forever, until paused/stopped."""
        while self.state.running:
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                log.info("orchestrator task cancelled")
                raise
            except Exception as e:
                log.exception("cycle crashed: %s", e)
                self.state.phase = "error"
                self.state.last_error = str(e)
                self.state.running = False
                self._persist()
                return

            if not self.state.running:
                break
            # soft-stop-after-cycle: the cycle finished cleanly; now stop before
            # starting a new one so the human can evaluate the workspace.
            if self.state.stop_after_cycle:
                log.info("stop_after_cycle flag tripped — stopping after cycle %d", self.state.cycle_number)
                self.state.running = False
                self.state.phase = "stopped"
                self.state.stop_after_cycle = False  # clear so a later Start runs normally
                # also clear the flag on the project record
                if self.active_project_id:
                    update_project(self.active_project_id, stop_after_cycle=0)
                set_activity_hook(None)
                self._persist()
                break
            # brief breather between cycles
            try:
                await asyncio.sleep(settings.inter_cycle_delay_sec)
            except asyncio.CancelledError:
                raise

    async def _run_cycle(self) -> None:
        """One REFLECT -> PLAN -> EXECUTE -> VERIFY -> RECORD cycle."""
        # Only the kill switch gates the loop up front. No token budgets —
        # this runs against a local model and churns 24/7. Token usage is
        # counted for display only (see budget.py).
        if guardrails.kill_switch.engaged:
            self.state.running = False
            self.state.phase = "stopped"
            self._persist()
            return
        budget.budget.rollover_day_if_needed()

        self.state.cycle_number += 1
        cycle = self.state.cycle_number
        budget.reset_cycle()
        guardrails.loop_detector.reset()
        self.state.cycle_tokens_used = 0
        self.state.current_task = None
        self.state.agent_session_id = None

        rec = CycleRecord(cycle_number=cycle, phase="reflect", project_id=self.active_project_id)
        self._current_cycle_id = insert_cycle(rec)
        self.state.phase = "reflect"
        self._persist()

        snap = await snapshot()  # git sha before any changes
        self.state.last_snapshot_sha = snap
        tasks_attempted = 0
        tasks_passed = 0

        # --- REFLECT + PLAN (one fresh session; the prompt does both) ---
        log.info("[cycle %d] REFLECT+PLAN", cycle)
        reflect = await run_goal(
            prompts.reflect_prompt(cycle), title=f"solo cycle {cycle} reflect"
        )
        self.state.cycle_tokens_used = budget.budget.cycle_tokens
        self.state.agent_session_id = reflect.session_id
        self._persist()
        if not reflect.ok:
            await self._record_cycle(
                cycle, outcome="errored", error=reflect.error or "reflect failed",
                sha=snap, tokens=reflect.tokens_used,
            )
            self.state.consecutive_fail_cycles += 1
            await self._maybe_pause_on_stall(cycle)
            return

        # re-parse backlog to find unchecked tasks
        backlog_tasks = parse_tasks(artifacts.read_backlog())
        pending = [t for t in backlog_tasks if t.status == "todo"]
        if not pending:
            log.info("[cycle %d] no pending backlog tasks; pausing for human", cycle)
            await self._record_cycle(
                cycle, outcome="paused", error="backlog empty",
                summary="no pending tasks; needs human to seed backlog", sha=snap,
            )
            self.state.running = False
            self.state.phase = "paused"
            self._persist()
            return

        # --- EXECUTE (one fresh session per task, up to a small cap) ---
        self.state.phase = "execute"
        self._persist()
        max_tasks = min(len(pending), 3)  # cap work per cycle to keep cycles bounded
        gate = verify_enabled()  # if False, no orchestrator gate; agent self-verifies
        for task in pending[:max_tasks]:
            if guardrails.kill_switch.engaged:
                break
            tasks_attempted += 1
            self.state.current_task = task.text
            self._persist()
            log.info("[cycle %d] EXECUTE: %s", cycle, task.text[:80])

            # snapshot before each task so we can revert just it on gate failure
            task_snap = await snapshot()
            res = await run_goal(
                prompts.execute_prompt(cycle, task.text),
                title=f"solo cycle {cycle} task",
            )
            self.state.cycle_tokens_used = budget.budget.cycle_tokens
            self._persist()

            if not res.ok:
                log.warning("[cycle %d] task agent failed: %s", cycle, res.error)
                # only revert if there's a gate to enforce; without one, trust the agent
                if gate and task_snap:
                    await revert_to(task_snap)
                continue

            # --- VERIFY (only if a gate is configured) ---
            if gate:
                self.state.phase = "verify"
                self._persist()
                verify = await run_verify()
                if verify.ok:
                    tasks_passed += 1
                    await stage_all()
                    await commit_all(f"solo-agent cycle {cycle}: {task.text[:72]}")
                    log.info("[cycle %d] VERIFY PASS, committed", cycle)
                else:
                    log.warning("[cycle %d] VERIFY FAIL, reverting: %s", cycle, verify.summary())
                    if task_snap:
                        await revert_to(task_snap)
            else:
                # No orchestrator gate — the agent self-verifies (prompt told it to).
                # Commit the work and count it as done.
                tasks_passed += 1
                await stage_all()
                await commit_all(f"solo-agent cycle {cycle}: {task.text[:72]}")
                log.info("[cycle %d] no gate — committed agent's self-verified work", cycle)

        # --- RECORD ---
        self.state.phase = "record"
        head = await snapshot()
        lines = await diff_stat(snap) if snap else 0
        outcome = "passed" if tasks_passed > 0 else ("failed" if tasks_attempted else "paused")
        reflection_text = (
            f"Attempted {tasks_attempted} task(s); {tasks_passed} completed"
            + (" (gate passed)" if gate else " (no gate; agent self-verified)")
            + f". {lines} lines changed. Agent summary: {reflect.final_message[:200]}"
        )
        artifacts.append_reflection(
            reflection_text, cycle=cycle, outcome=outcome, sha=head,
        )
        await self._record_cycle(
            cycle, outcome=outcome, sha=snap, head_sha=head, lines=lines,
            tokens=budget.budget.cycle_tokens, attempted=tasks_attempted,
            passed=tasks_passed, summary=reflect.final_message[:500],
        )

        # update stall detectors
        self.state.consecutive_low_change_cycles = (
            self.state.consecutive_low_change_cycles + 1 if lines < settings.stall_min_lines_changed else 0
        )
        self.state.consecutive_fail_cycles = (
            self.state.consecutive_fail_cycles + 1 if tasks_passed == 0 and tasks_attempted > 0 else 0
        )
        await self._maybe_pause_on_stall(cycle)

        self.state.phase = "idle"
        self.state.current_task = None
        self.state.last_outcome = outcome
        self._persist()

    async def _maybe_pause_on_stall(self, cycle: int) -> None:
        """Auto-pause if the loop is stalled or consistently failing."""
        if guardrails.no_progress.observe(
            # feed the diff_stat via the last recorded lines_changed
            self._last_lines_changed()
        ) or self.state.consecutive_fail_cycles >= settings.stall_detection_cycles:
            log.warning(
                "[cycle %d] stall/consistent-failure detected — pausing for human", cycle
            )
            self.state.running = False
            self.state.phase = "paused"
            self.state.last_error = (
                f"stalled: {self.state.consecutive_low_change_cycles} low-change cycles, "
                f"{self.state.consecutive_fail_cycles} fail cycles"
            )
            self._persist()

    def _last_lines_changed(self) -> int:
        """Peek the most recent cycle's lines_changed from DB for the no-progress detector."""
        from ..db import fetch_cycles

        rows = fetch_cycles(limit=1, project_id=self.active_project_id or "")
        if rows:
            try:
                return int(rows[0]["lines_changed"])
            except (KeyError, ValueError, TypeError):
                return 0
        return 0

    async def _record_cycle(
        self,
        cycle: int,
        *,
        outcome: str,
        sha: Optional[str] = None,
        head_sha: Optional[str] = None,
        lines: int = 0,
        tokens: int = 0,
        attempted: int = 0,
        passed: int = 0,
        error: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> None:
        """Finalize the current cycle row in the DB."""
        if self._current_cycle_id is None:
            return
        update_cycle(
            self._current_cycle_id,
            phase=self.state.phase,
            ended_at=datetime.utcnow(),
            outcome=outcome,
            snapshot_sha=sha,
            head_sha=head_sha,
            lines_changed=lines,
            tokens_used=tokens,
            tasks_attempted=attempted,
            tasks_passed=passed,
            error=error,
            summary=summary,
            agent_session_id=self.state.agent_session_id,
        )

    # ---- persistence ---------------------------------------------------------

    def _persist(self) -> None:
        self.state.updated_at = datetime.utcnow()
        self.state.project_id = self.active_project_id
        set_orch_state(self.state.model_dump(mode="json"), self.active_project_id or "")

    def snapshot(self) -> OrchestratorState:
        return self.state


# Singleton, wired into the FastAPI app at startup.
controller = OrchestratorController()
