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
from ..db import get_orch_state, insert_cycle, set_orch_state, update_cycle
from ..models import CycleRecord, OrchestratorState
from ..state_reader import parse_tasks
from . import artifacts, budget, guardrails, prompts
from .git_ops import (
    commit_all,
    diff_stat,
    ensure_work_branch,
    is_repo,
    revert_to,
    snapshot,
    stage_all,
)
from .runner import run_goal
from .verify import run_verify

log = logging.getLogger("solo.controller")


class OrchestratorController:
    """Drives the Ralph loop. Single instance, started in app lifespan."""

    def __init__(self) -> None:
        self.state = OrchestratorState()
        self._task: Optional[asyncio.Task] = None
        self._current_cycle_id: Optional[int] = None
        self._resume()

    # ---- lifecycle -----------------------------------------------------------

    def _resume(self) -> None:
        """Restore state from SQLite so we resume after a restart."""
        persisted = get_orch_state()
        if persisted:
            try:
                self.state = OrchestratorState(**persisted)
                log.info(
                    "resumed orchestrator state: cycle=%d phase=%s",
                    self.state.cycle_number, self.state.phase,
                )
            except Exception as e:
                log.warning("could not resume state (%s); starting fresh", e)
        # if we were mid-run when killed, don't auto-resume running; require explicit start
        if self.state.phase not in ("idle", "stopped", "paused", "error"):
            self.state.phase = "paused"
            self.state.running = False

    async def start(self) -> str:
        """Begin cycling. Returns a status message."""
        if not await is_repo():
            msg = f"project_path {settings.project_path} is not a git repository"
            log.error(msg)
            self.state.phase = "error"
            self.state.last_error = msg
            self._persist()
            return msg
        await ensure_work_branch()
        artifacts.ensure_artifacts()

        if self._task is not None and not self._task.done():
            return "already running"
        guardrails.kill_switch.clear()
        self.state.running = True
        self.state.phase = "idle"
        self.state.last_error = None
        self._persist()
        self._task = asyncio.create_task(self._loop(), name="orchestrator")
        log.info("orchestrator started (project=%s)", settings.project_path)
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

        rec = CycleRecord(cycle_number=cycle, phase="reflect")
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
        for task in pending[:max_tasks]:
            if guardrails.kill_switch.engaged:
                break
            tasks_attempted += 1
            self.state.current_task = task.text
            self._persist()
            log.info("[cycle %d] EXECUTE: %s", cycle, task.text[:80])

            # snapshot before each task so we can revert just it on failure
            task_snap = await snapshot()
            res = await run_goal(
                prompts.execute_prompt(cycle, task.text),
                title=f"solo cycle {cycle} task",
            )
            self.state.cycle_tokens_used = budget.budget.cycle_tokens
            self._persist()

            if not res.ok:
                log.warning("[cycle %d] task agent failed: %s", cycle, res.error)
                await revert_to(task_snap) if task_snap else None
                continue

            # --- VERIFY (orchestrator-owned gate) ---
            self.state.phase = "verify"
            self._persist()
            verify = await run_verify()
            if verify.ok:
                tasks_passed += 1
                # commit the passing change
                await stage_all()
                await commit_all(f"solo-agent cycle {cycle}: {task.text[:72]}")
                log.info("[cycle %d] VERIFY PASS, committed", cycle)
            else:
                log.warning("[cycle %d] VERIFY FAIL, reverting: %s", cycle, verify.summary())
                await revert_to(task_snap) if task_snap else None

        # --- RECORD ---
        self.state.phase = "record"
        head = await snapshot()
        lines = await diff_stat(snap) if snap else 0
        outcome = "passed" if tasks_passed > 0 else ("failed" if tasks_attempted else "paused")
        reflection_text = (
            f"Attempted {tasks_attempted} task(s); {tasks_passed} passed verify. "
            f"{lines} lines changed. Agent summary: {reflect.final_message[:200]}"
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

        rows = fetch_cycles(limit=1)
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
        set_orch_state(self.state.model_dump(mode="json"))

    def snapshot(self) -> OrchestratorState:
        return self.state


# Singleton, wired into the FastAPI app at startup.
controller = OrchestratorController()
