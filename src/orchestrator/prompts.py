"""Prompt templates for the Ralph loop phases.

Each phase passes a structured prompt to a FRESH OpenCode session (no context
carry-over — that's the Ralph core principle). The agent reads backlog.md and
reflections.md itself; we point it at them and give clear stop conditions.
"""

from __future__ import annotations

from ..config import settings


def reflect_prompt(cycle: int) -> str:
    return f"""You are analyzing the codebase at {settings.project_path} to find high-value improvements.

Read these files first (they are your persistent memory across cycles):
- backlog.md   — tasks already identified (avoid duplicating)
- reflections.md — what's been tried and what failed (avoid repeating failures)

Then survey the codebase for concrete improvement opportunities. Focus on:
- Bugs, error-handling gaps, or correctness risks
- Missing or weak tests (coverage holes)
- Performance bottlenecks with measurable impact
- Code clarity / maintainability wins that reduce future risk

For each opportunity, append a new task to backlog.md as a markdown checkbox:
  - [ ] <short, verifiable description>

Constraints:
- Each task must be small enough to complete in one session (~10 minutes).
- Each task must be independently verifiable (the test suite or a build must pass).
- Prefer tasks with clear acceptance criteria over vague refactors.
- Do NOT mark items done; that's the orchestrator's job after verification.
- Do NOT delete or rewrite existing backlog items.

When done, respond with a one-line summary of how many tasks you added."""


def plan_prompt(cycle: int) -> str:
    return f"""You are structuring the backlog at {settings.project_path} for execution.

Read backlog.md. Re-order and refine the unchecked items so that:
- The highest-value, lowest-risk task is first.
- Each task has a clear, testable acceptance criterion (add one if missing).
- Dependencies between tasks are respected (do A before B if B needs A).

Do NOT check off items. Do NOT remove items unless they're truly obsolete
(move those to a '## Archived' section at the bottom instead).

When done, respond with a one-line summary of the task ordering."""


def execute_prompt(cycle: int, task_text: str) -> str:
    return f"""You are implementing ONE task in the codebase at {settings.project_path}.

TASK:
{task_text}

Read reflections.md first to avoid repeating past failures.

Rules:
- Make the minimal change needed to complete the task correctly.
- Do NOT mark the task done in backlog.md — the orchestrator verifies independently.
- Do NOT touch unrelated code. Stay in scope.
- Ensure your change is consistent with existing conventions in the codebase.
- If you discover the task is blocked or invalid, stop and say so clearly.

The orchestrator will run the full test suite when you finish. If it fails,
your changes will be reverted, so verify your work mentally before stopping.

When done, respond with: DONE: <one-line summary of what you changed>"""


def stop_phrase() -> str:
    """A marker the agent emits that we treat as clean completion."""
    return "DONE:"
