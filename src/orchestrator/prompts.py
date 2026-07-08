"""Prompt templates for the Ralph loop phases.

Each phase spawns a FRESH OpenCode session (no context carry-over — the Ralph
core principle). The loop's standing context — role, the cycle, the artifact
map, the rules — lives in SOLO_AGENT.md (written by artifacts.ensure_artifacts),
so these prompts stay SHORT: they orient the session to its phase and point it
at the protocol file + memory, then state the task. Read once as a file beats
paid-per-session prompt tokens.

Every prompt ends by reminding the agent of the DONE: stop signal.
"""

from __future__ import annotations

ORIENT = (
    "Read SOLO_AGENT.md first (your operating protocol) and reflections.md "
    "(prior-cycle memory). You are a fresh session in an autonomous improvement "
    "loop; those two files are how you get oriented."
)


def reflect_prompt(cycle: int) -> str:
    return f"""{ORIENT}

PHASE: REFLECT (cycle {cycle}). Survey this codebase and propose concrete
improvement tasks. Focus on bugs, missing or weak tests, error-handling gaps,
and clear maintainability wins — each independently verifiable.

Append each candidate as a new `- [ ]` line in backlog.md. Each task must be
small (~10 min) and checkable by a test, build, or lint. Don't duplicate items
already in backlog.md, and don't remove or check off existing items.

End with: DONE: <how many tasks you added and the themes>"""


def plan_prompt(cycle: int) -> str:
    return f"""{ORIENT}

PHASE: PLAN (cycle {cycle}). Read backlog.md and order/refine the unchecked
(`- [ ]`) items so the highest-value, lowest-risk task is first. Ensure each
has a clear, testable acceptance criterion; add one if missing. Don't check
items off, and don't delete items — move obsolete ones to a `## Archived`
section at the bottom.

End with: DONE: <the task ordering, one line>"""


def execute_prompt(cycle: int, task_text: str) -> str:
    return f"""{ORIENT}

PHASE: EXECUTE (cycle {cycle}). Implement exactly ONE task:

    {task_text}

Read reflections.md to avoid repeating past failures, then make the minimal
correct change. Stay in scope — don't refactor unrelated code. Don't mark the
task done in backlog.md (the orchestrator verifies independently). If you find
the task blocked or invalid, stop and say so honestly.

The orchestrator will run the verification gate when you finish; if it fails,
your work is reverted, so reason about correctness before stopping.

End with: DONE: <one-line summary of what you changed>"""


def stop_phrase() -> str:
    """The marker the agent emits that we treat as clean completion."""
    return "DONE:"
