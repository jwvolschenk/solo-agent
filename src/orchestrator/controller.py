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
from ..models import ActivityEvent, CycleRecord, OrchestratorState, TranscriptEvent
from ..state_reader import parse_tasks
from .. import transcript
from . import artifacts, budget, guardrails, prompts
from .memory import build_memory_brief
from .runner import new_session_id, set_activity_hook
from . import trace
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
        transcript.clear()  # a new project's loop starts with a clean rich transcript
        await transcript.notify_cleared()  # tell already-connected clients to drop stale transcript
        # resume the new project's persisted state
        self._resume()
        self.state.project_id = project_id
        self._persist()
        trace.bind(
            project_id=project_id,
            cycle=self.state.cycle_number,
            phase=self.state.phase,
        )
        trace.lifecycle(
            "switch_project",
            project_id=project_id,
            path=str(settings.project_path),
        )
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
        trace.bind(
            project_id=pid or None,
            cycle=self.state.cycle_number,
            phase=self.state.phase,
        )
        trace.lifecycle(
            "resume_state",
            cycle=self.state.cycle_number,
            phase=self.state.phase,
            running=self.state.running,
        )

    def _set_phase(self, phase: str, *, reason: str | None = None) -> None:
        """Update phase, log the transition, and persist."""
        prev = self.state.phase
        if prev != phase:
            trace.bind(phase=phase, cycle=self.state.cycle_number, project_id=self.active_project_id)
            trace.phase_transition(prev, phase, reason=reason)
        self.state.phase = phase
        self._persist()

    async def start(self) -> str:
        """Begin cycling. Requires an active project with a goal."""
        # require a goal
        if not settings.goal.strip():
            msg = "no goal set — set one on the active project before starting"
            log.error(msg)
            trace.error("start_blocked", reason=msg)
            self._set_phase("error", reason=msg)
            self.state.last_error = msg
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
        self._reset_stall_detectors()
        # wire the runner's tool calls into the activity feed
        set_activity_hook(
            lambda t, m, meta: insert_activity(
                ActivityEvent(type=t, message=m, metadata=meta, project_id=self.active_project_id)
            )
        )  # type: ignore[arg-type]
        self.state.running = True
        self.state.last_error = None
        trace.bind(
            project_id=self.active_project_id,
            cycle=self.state.cycle_number,
        )
        self._set_phase("idle", reason="start")
        self._task = asyncio.create_task(self._loop(), name="orchestrator")
        trace.lifecycle(
            "start",
            project_id=self.active_project_id,
            path=str(settings.project_path),
            goal_chars=len(settings.goal),
        )
        log.info("orchestrator started (project=%s, goal=%d chars)", settings.project_path, len(settings.goal))
        return "started"

    async def pause(self) -> str:
        """Pause between cycles (current cycle finishes its phase)."""
        self.state.running = False
        self._set_phase("paused", reason="pause")
        trace.lifecycle("pause")
        log.info("orchestrator paused")
        return "paused"

    async def resume(self) -> str:
        """Resume from pause."""
        if self._task is not None and not self._task.done():
            return "already running"
        self._reset_stall_detectors()
        self.state.running = True
        self._set_phase("idle", reason="resume")
        self._task = asyncio.create_task(self._loop(), name="orchestrator")
        trace.lifecycle("resume")
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
        self._set_phase("stopped", reason="stop")
        guardrails.kill_switch.clear()
        set_activity_hook(None)  # stop logging tool calls
        trace.lifecycle("stop")
        log.info("orchestrator stopped")
        return "stopped"

    # ---- the loop ------------------------------------------------------------

    async def _loop(self) -> None:
        """Outer loop: one full cycle per iteration, forever, until paused/stopped."""
        while self.state.running:
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                trace.lifecycle("cancelled")
                log.info("orchestrator task cancelled")
                raise
            except Exception as e:
                log.exception("cycle crashed: %s", e)
                trace.error("cycle_crashed", error=str(e))
                self._set_phase("error", reason=str(e))
                self.state.last_error = str(e)
                self.state.running = False
                return

            if not self.state.running:
                break
            # soft-stop-after-cycle: the cycle finished cleanly; now stop before
            # starting a new one so the human can evaluate the workspace.
            if self.state.stop_after_cycle:
                trace.lifecycle(
                    "stop_after_cycle",
                    cycle=self.state.cycle_number,
                )
                log.info("stop_after_cycle flag tripped — stopping after cycle %d", self.state.cycle_number)
                self.state.running = False
                self._set_phase("stopped", reason="stop_after_cycle")
                self.state.stop_after_cycle = False  # clear so a later Start runs normally
                # also clear the flag on the project record
                if self.active_project_id:
                    update_project(self.active_project_id, stop_after_cycle=0)
                set_activity_hook(None)
                break
            # brief breather between cycles
            try:
                await asyncio.sleep(settings.inter_cycle_delay_sec)
            except asyncio.CancelledError:
                raise

    async def _run_cycle(self) -> None:
        """One cycle. The cycle shape adapts to the backlog state:

        - If there are PENDING (unchecked) backlog tasks → EXECUTE the first one.
          One task per cycle; remaining backlog items wait for later cycles.
        - If there are NO pending tasks → ARCHIVE completed items to a dated
          history file, then REFLECT (find candidates) and PLAN (decompose &
          order) to refill the backlog.

        This prevents the "continuously moving target" problem: the agent
        finishes existing goals before looking for new ones.
        """
        if guardrails.kill_switch.engaged:
            trace.guardrail("kill_switch", guardrails.kill_switch.reason or "engaged")
            self.state.running = False
            self._set_phase("stopped", reason="kill_switch")
            return
        budget.budget.rollover_day_if_needed()

        self.state.cycle_number += 1
        cycle = self.state.cycle_number
        trace.bind(cycle=cycle, project_id=self.active_project_id, task=None, session_id=None)
        budget.reset_cycle()
        guardrails.loop_detector.reset()
        self.state.cycle_tokens_used = 0
        self.state.current_task = None
        self.state.agent_session_id = None
        artifacts.relocate_stale_seeds()

        rec = CycleRecord(cycle_number=cycle, phase="execute", project_id=self.active_project_id)
        self._current_cycle_id = insert_cycle(rec)

        snap = await snapshot()
        self.state.last_snapshot_sha = snap
        tasks_attempted = 0
        tasks_passed = 0
        gate = verify_enabled()
        outcome = "paused"
        head: Optional[str] = None
        lines = 0

        # --- Check the backlog: are there pending executor tasks? ---
        backlog_tasks = parse_tasks(artifacts.read_backlog())
        pending = [
            t for t in backlog_tasks
            if t.status == "todo" and not artifacts.is_planner_only_task(t.text)
        ]
        drift_msg = artifacts.backlog_format_drift()
        trace.info(
            "cycle_start",
            snapshot_sha=(snap[:10] if snap else None),
            pending_tasks=len(pending),
            verify_gate=gate,
            path="execute" if pending else "reflect",
            backlog_format_drift=bool(drift_msg),
        )

        if not pending and drift_msg:
            log.warning("[cycle %d] %s — pausing", cycle, drift_msg)
            trace.guardrail(
                "backlog_format_drift",
                drift_msg,
                headings=artifacts.count_task_headings(artifacts.read_backlog()),
            )
            self.state.running = False
            self._set_phase("paused", reason="backlog_format_drift")
            self.state.last_error = drift_msg
            await self._record_cycle(
                cycle, outcome="paused", error=drift_msg, sha=snap,
            )
            trace.info("cycle_end", outcome="paused", error=drift_msg, lines=0)
            return

        if pending:
            if guardrails.kill_switch.engaged:
                self._set_phase("idle", reason="kill_switch_mid_cycle")
                return
            # ---- EXECUTE PATH: one backlog task per cycle ----
            task = pending[0]
            trace.bind(task=task.text)
            trace.info(
                "execute_task",
                pending_count=len(pending),
                task_preview=task.text[:120],
            )
            log.info(
                "[cycle %d] executing 1 of %d pending backlog task(s): %s",
                cycle, len(pending), task.text[:80],
            )
            self._set_phase("execute", reason="backlog_task")
            tasks_attempted = 1
            self.state.current_task = task.text
            self._persist()

            sid = new_session_id()
            trace.bind(session_id=sid)
            await transcript.record(TranscriptEvent(
                id=sid, kind="session_start", status="running",
                session_id=sid, cycle=cycle, task=task.text,
            ))
            task_snap = await snapshot()
            memory_brief = build_memory_brief()
            res = await run_goal(
                prompts.execute_prompt(cycle, task.text, memory_brief=memory_brief),
                title=f"solo cycle {cycle} task",
                session_id=sid,
            )
            await transcript.record(TranscriptEvent(
                id=f"{sid}-end", kind="session_end",
                status="completed" if res.ok else "error",
                session_id=sid, cycle=cycle, task=task.text,
            ))
            self.state.cycle_tokens_used = budget.budget.cycle_tokens
            self.state.agent_session_id = res.session_id
            self._persist()

            if not res.ok:
                log.warning("[cycle %d] task agent failed: %s", cycle, res.error)
                trace.warning(
                    "execute_failed",
                    error=res.error,
                    timed_out=res.timed_out,
                    session_id=res.session_id,
                )
                if gate and task_snap:
                    trace.git_action("revert", reason="agent_failed", sha=task_snap[:10])
                    await revert_to(task_snap)
            elif gate:
                self._set_phase("verify", reason="agent_ok")
                verify = await run_verify()
                if not verify.ok:
                    log.warning("[cycle %d] VERIFY FAIL, reverting: %s", cycle, verify.summary())
                    trace.warning("verify_revert", summary=verify.summary())
                    if task_snap:
                        trace.git_action("revert", reason="verify_failed", sha=task_snap[:10])
                        await revert_to(task_snap)
                else:
                    tasks_passed = 1
                    await stage_all()
                    await commit_all(f"solo-agent cycle {cycle}: {task.text[:72]}")
                    trace.git_action("commit", reason="verify_passed", tasks_passed=1)
                    log.info("[cycle %d] task committed", cycle)
            else:
                tasks_passed = 1
                await stage_all()
                await commit_all(f"solo-agent cycle {cycle}: {task.text[:72]}")
                trace.git_action("commit", reason="no_verify_gate", tasks_passed=1)
                log.info("[cycle %d] task committed", cycle)

            # record the execute cycle
            self._set_phase("record", reason="execute_complete")
            head = await snapshot()
            lines = await diff_stat(snap) if snap else 0
            outcome = "passed" if tasks_passed > 0 else ("failed" if tasks_attempted else "paused")
            reflection_text = (
                f"Executed {tasks_attempted} backlog task(s); {tasks_passed} completed"
                + (" (gate passed)" if gate else " (no gate; agent self-verified)")
                + f". {lines} lines changed."
            )
            if artifacts.should_record_execute_reflection(
                tasks_attempted=tasks_attempted, tasks_passed=tasks_passed
            ):
                artifacts.append_reflection(
                    reflection_text, cycle=cycle, outcome=outcome, sha=head
                )
            await self._record_cycle(
                cycle, outcome=outcome, sha=snap, head_sha=head, lines=lines,
                tokens=budget.budget.cycle_tokens, attempted=tasks_attempted,
                passed=tasks_passed, summary=reflection_text[:500],
            )
            self._update_stall_counters(lines, tasks_passed, tasks_attempted)
            await self._maybe_pause_on_stall(cycle)

        else:
            # ---- REFLECT PATH: backlog empty/all-done → archive, then plan ----
            trace.info("reflect_path", reason="backlog_clear")
            log.info("[cycle %d] backlog clear — archiving + reflecting", cycle)

            # Step 1: archive completed items into a dated history file
            archived = artifacts.archive_backlog()
            if archived:
                await stage_all()
                await commit_all(f"solo-agent cycle {cycle}: archive {archived} completed backlog items")
                trace.info("backlog_archived", count=archived)
                log.info("[cycle %d] archived %d completed items", cycle, archived)

            # Step 2: reflect to find candidate work
            self._set_phase("reflect", reason="backlog_clear")
            sid = new_session_id()
            trace.bind(session_id=sid, task="reflect")
            memory_brief = build_memory_brief()
            await transcript.record(TranscriptEvent(
                id=sid, kind="session_start", status="running",
                session_id=sid, cycle=cycle, task="reflect",
            ))
            reflect = await run_goal(
                prompts.reflect_prompt(cycle, memory_brief=memory_brief),
                title=f"solo cycle {cycle} reflect",
                session_id=sid,
            )
            await transcript.record(TranscriptEvent(
                id=f"{sid}-end", kind="session_end",
                status="completed" if reflect.ok else "error",
                session_id=sid, cycle=cycle, task="reflect",
            ))
            self.state.cycle_tokens_used = budget.budget.cycle_tokens
            self.state.agent_session_id = reflect.session_id
            self._persist()

            if not reflect.ok:
                trace.warning("reflect_failed", error=reflect.error)
                await self._record_cycle(
                    cycle, outcome="errored", error=reflect.error or "reflect failed",
                    sha=snap, tokens=reflect.tokens_used,
                )
                self.state.consecutive_fail_cycles += 1
                await self._maybe_pause_on_stall(cycle)
                trace.info(
                    "cycle_end",
                    outcome="errored",
                    path="reflect",
                    error=reflect.error,
                )
                return

            # Step 3: ensure planner inbox has candidates (orchestrator seed if
            # reflect found nothing — PLAN decomposes into backlog.md next).
            candidate_tasks = parse_tasks(artifacts.read_candidates())
            pending_candidates = [t for t in candidate_tasks if t.status == "todo"]
            fallback_task: Optional[str] = None
            if not pending_candidates:
                fallback_task = artifacts.append_fallback_candidate(cycle)
                trace.info("reflect_fallback_seed", category=fallback_task)
                log.info("[cycle %d] reflect produced no candidates; orchestrator seeded fallback", cycle)
                candidate_tasks = parse_tasks(artifacts.read_candidates())
                pending_candidates = [t for t in candidate_tasks if t.status == "todo"]

            # Step 4: plan — decompose candidate themes into backlog.md tasks.
            plan_summary = ""
            if pending_candidates:
                self._set_phase("plan", reason="candidates_ready")
                plan_sid = new_session_id()
                trace.bind(session_id=plan_sid, task="plan")
                await transcript.record(TranscriptEvent(
                    id=plan_sid, kind="session_start", status="running",
                    session_id=plan_sid, cycle=cycle, task="plan",
                ))
                plan = await run_goal(
                    prompts.plan_prompt(cycle, memory_brief=memory_brief),
                    title=f"solo cycle {cycle} plan",
                    session_id=plan_sid,
                )
                await transcript.record(TranscriptEvent(
                    id=f"{plan_sid}-end", kind="session_end",
                    status="completed" if plan.ok else "error",
                    session_id=plan_sid, cycle=cycle, task="plan",
                ))
                self.state.cycle_tokens_used = budget.budget.cycle_tokens
                self.state.agent_session_id = plan.session_id
                self._persist()
                if not plan.ok:
                    log.warning("[cycle %d] PLAN failed: %s", cycle, plan.error)
                    trace.warning("plan_failed", error=plan.error)
                    plan_summary = f" Plan failed ({plan.error or 'unknown'}); backlog unchanged."
                else:
                    trace.info("plan_complete", message_preview=plan.final_message[:200])
                    plan_summary = f" Plan: {plan.final_message[:200]}"

            backlog_tasks = parse_tasks(artifacts.read_backlog())
            new_pending = [
                t for t in backlog_tasks
                if t.status == "todo" and not artifacts.is_planner_only_task(t.text)
            ]
            post_plan_drift = artifacts.backlog_format_drift()
            if post_plan_drift and not new_pending:
                log.warning("[cycle %d] %s — pausing", cycle, post_plan_drift)
                trace.guardrail(
                    "backlog_format_drift",
                    post_plan_drift,
                    headings=artifacts.count_task_headings(artifacts.read_backlog()),
                    phase="post_plan",
                )
                self.state.running = False
                self._set_phase("paused", reason="backlog_format_drift")
                self.state.last_error = post_plan_drift
                await self._record_cycle(
                    cycle, outcome="paused", error=post_plan_drift, sha=snap,
                    tokens=budget.budget.cycle_tokens,
                    summary=(reflect.final_message[:250] + plan_summary)[:500],
                )
                trace.info("cycle_end", outcome="paused", error=post_plan_drift, lines=0)
                return

            head = await snapshot()
            lines = await diff_stat(snap) if snap else 0
            outcome = "passed"
            if fallback_task:
                reflection_text = (
                    f"Archived {archived} completed items. Reflect found no candidates, so "
                    f"the orchestrator seeded backlog-candidates.md for PLAN to "
                    f"decompose: {fallback_task}.{plan_summary}"
                )
            else:
                reflection_text = (
                    f"Archived {archived} completed items, then reflected: "
                    f"{len(new_pending)} ready task(s) after plan."
                    f" Reflect: {reflect.final_message[:120]}.{plan_summary}"
                )
            artifacts.append_reflection(reflection_text, cycle=cycle, outcome=outcome, sha=head)
            await self._record_cycle(
                cycle, outcome=outcome, sha=snap, head_sha=head, lines=lines,
                tokens=budget.budget.cycle_tokens, attempted=0, passed=0,
                summary=(reflect.final_message[:250] + plan_summary)[:500],
            )
            self._update_stall_counters(lines, 0, 0)
            await self._maybe_pause_on_stall(cycle)

        if self.state.phase in ("paused", "stopped", "error"):
            trace.info(
                "cycle_end",
                outcome=outcome,
                lines=lines,
                tokens=budget.budget.cycle_tokens,
                tasks_attempted=tasks_attempted,
                tasks_passed=tasks_passed,
                head_sha=(head[:10] if head else None),
                interrupted_phase=self.state.phase,
            )
            return

        self._set_phase("idle", reason="cycle_complete")
        self.state.current_task = None
        self.state.last_outcome = outcome
        trace.info(
            "cycle_end",
            outcome=outcome,
            lines=lines,
            tokens=budget.budget.cycle_tokens,
            tasks_attempted=tasks_attempted,
            tasks_passed=tasks_passed,
            head_sha=(head[:10] if head else None),
        )

    def _reset_stall_detectors(self) -> None:
        """Clear stall history when the human explicitly restarts the loop."""
        self.state.consecutive_low_change_cycles = 0
        self.state.consecutive_fail_cycles = 0
        guardrails.no_progress.reset()

    def _update_stall_counters(self, lines: int, tasks_passed: int, tasks_attempted: int) -> None:
        """Update the diminishing-returns + consecutive-failure counters.

        Low-line cycles only count toward stall when no task completed — a
        successful execute cycle that only touches backlog.md (a few lines)
        is real progress and must not trip the guardrail.
        """
        if lines < settings.stall_min_lines_changed and tasks_passed == 0:
            self.state.consecutive_low_change_cycles += 1
        else:
            self.state.consecutive_low_change_cycles = 0
        self.state.consecutive_fail_cycles = (
            self.state.consecutive_fail_cycles + 1
            if tasks_passed == 0 and tasks_attempted > 0
            else 0
        )

    async def _maybe_pause_on_stall(self, cycle: int) -> None:
        """Auto-pause if the loop is stalled or consistently failing."""
        if (
            self.state.consecutive_low_change_cycles >= settings.stall_detection_cycles
            or self.state.consecutive_fail_cycles >= settings.stall_detection_cycles
        ):
            reason = (
                f"stalled: {self.state.consecutive_low_change_cycles} low-change cycles, "
                f"{self.state.consecutive_fail_cycles} fail cycles"
            )
            trace.guardrail(
                "stall",
                reason,
                low_change_cycles=self.state.consecutive_low_change_cycles,
                fail_cycles=self.state.consecutive_fail_cycles,
            )
            log.warning(
                "[cycle %d] stall/consistent-failure detected — pausing for human", cycle
            )
            self.state.running = False
            self._set_phase("paused", reason=reason)
            self.state.last_error = reason

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
