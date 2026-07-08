"""Prompt templates for the Ralph loop phases.

Each phase spawns a FRESH OpenCode session (no context carry-over — the Ralph
core principle). The loop's standing context lives in SOLO_AGENT.md + GOAL.md
(written by artifacts.ensure_artifacts), so these prompts stay SHORT: orient to
the phase, point at the protocol + goal + memory files, state the task.

Every prompt ends by reminding the agent of the DONE: stop signal.
"""

from __future__ import annotations

ORIENT = (
    "Read SOLO_AGENT.md (your operating protocol), GOAL.md (the project's "
    "overarching goal), directives.md (human guidance — pending ones are "
    "priority), and reflections.md (prior-cycle memory). You are a fresh "
    "session in an autonomous loop; those files are how you get oriented."
)


def reflect_prompt(cycle: int) -> str:
    return f"""{ORIENT}

PHASE: REFLECT (cycle {cycle}). The backlog has been cleared — all previous goals
are done and archived. It's time to find the next round of work.

Look at the current state of this project vs. GOAL.md. What's the highest-value
work to do next?

- If the project is empty (from-scratch), this means proposing the scaffolding
  and first concrete features.
- If the project exists, look for bugs, missing features toward the goal,
  weak tests, and clear improvements.

IMPORTANT: Check directives.md for any `status: pending` entries. If there are
any, add them to the top of backlog.md as the highest-priority tasks (the human
queued them mid-loop to steer you). Mark their status to `acknowledged` once
you've queued them.

Append each candidate as a new `- [ ]` line in backlog.md. Each task must be
small (~one session) and independently completable. Don't duplicate items
already in backlog.md.

End with: DONE: <how many tasks you added and the themes>"""


def plan_prompt(cycle: int) -> str:
    return f"""{ORIENT}

PHASE: PLAN (cycle {cycle}). Read backlog.md and order/refine the unchecked
(`- [ ]`) items so the next step toward GOAL.md is first. Ensure each has a
clear acceptance criterion; add one if missing. Don't check items off, and
don't delete items — move obsolete ones to a `## Archived` section at the bottom.

End with: DONE: <the task ordering, one line>"""


def execute_prompt(cycle: int, task_text: str) -> str:
    return f"""{ORIENT}

PHASE: EXECUTE (cycle {cycle}). Implement exactly ONE task:

    {task_text}

Read reflections.md to avoid repeating past failures, then make the change.
Stay in scope — don't refactor unrelated code.

**Marking the task done**: when you complete the task (or discover it was
already done), edit its line in backlog.md from `- [ ]` to `- [x]`. This is
REQUIRED — it's how the orchestrator tracks progress. If the task turns out
to be blocked or invalid, leave it as `- [ ]` and say so in your summary.

Since the orchestrator may not run a verify gate, run the project's own
build/test/lint yourself before stopping to confirm your work is correct.

End with: DONE: <one-line summary of what you changed or why you stopped>"""


def stop_phrase() -> str:
    """The marker the agent emits that we treat as clean completion."""
    return "DONE:"
