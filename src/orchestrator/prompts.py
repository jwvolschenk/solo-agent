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
    "overarching goal), CODEDB.md (codedb navigation for this repo — also "
    "auto-loaded by OpenCode), directives.md (human guidance — pending ones "
    "are priority), and reflections.md (recent-cycle memory — failures and "
    "reflect insights only, not full history). You are a fresh session in an "
    "autonomous loop; those files are how you get oriented."
)


def reflect_prompt(cycle: int, memory_brief: str = "") -> str:
    memory = f"\n\n{memory_brief}\n" if memory_brief else ""
    return f"""{ORIENT}{memory}

PHASE: REFLECT (cycle {cycle}). The backlog has been cleared — all previous goals
are done and archived. It's time to find the next round of work.

Look at the current state of this project vs. GOAL.md. What's the highest-value
work to do next?

- If the project is empty (from-scratch), this means proposing the scaffolding
  and first concrete features.
- If the project exists, look for bugs, missing features toward the goal,
  weak tests, and clear improvements.

IMPORTANT: Check directives.md for any `status: pending` entries. If there are
any, add them to the top of backlog.md as ready one-session tasks (the human
queued them mid-loop to steer you). Mark their status to `acknowledged` once
you've queued them.

Append each coarse opportunity as a new `- [ ]` line in backlog-candidates.md
(not backlog.md — that file is the executor queue). Prefer several themes when
you see them. Don't duplicate items already in backlog-candidates.md.

End with: DONE: <how many candidates you added and the themes>"""


def plan_prompt(cycle: int, memory_brief: str = "") -> str:
    memory = f"\n\n{memory_brief}\n" if memory_brief else ""
    return f"""{ORIENT}{memory}

PHASE: PLAN (cycle {cycle}). Read backlog-candidates.md and decompose every
unchecked (`- [ ]`) theme into ready one-session tasks in backlog.md.

1. **Decompose**: for each candidate theme, add multiple smaller `- [ ]` tasks to
   backlog.md — one checkbox line per task (NOT `### Task:` headings). Each must
   be completable in one session with a testable outcome.
2. **Order**: put the highest-value next step toward GOAL.md first in backlog.md.
3. **Refine**: each backlog.md task needs a clear acceptance criterion (on the
   line or as a sub-bullet).
4. **Clear candidates**: when done, remove all processed lines from
   backlog-candidates.md (move to `## Archived` there if you want a trace). Leave
   backlog-candidates.md empty when finished — the executor never reads it.

Aim for **3–8** new tasks in backlog.md when the input allows. Don't check
backlog.md items off. Don't delete without archiving.

End with: DONE: <count of backlog.md tasks added and which one is first>"""


def execute_prompt(cycle: int, task_text: str, memory_brief: str = "") -> str:
    memory = f"\n\n{memory_brief}\n" if memory_brief else ""
    return f"""{ORIENT}{memory}

PHASE: EXECUTE (cycle {cycle}). Implement exactly ONE task:

    {task_text}

Read reflections.md for recent failures and reflect insights, then make the change.
Stay in scope — don't refactor unrelated code.

**Marking the task done**: when you complete the task (or discover it was
already done), edit its line in backlog.md from `- [ ]` to `- [x]`. This is
REQUIRED — it's how the orchestrator tracks progress. If the task turns out
to be blocked or invalid, leave it as `- [ ]` and say so in your summary.

**Leave the project working — non-negotiable.** Before you stop, the project
must build/compile and its existing tests must pass. Detect and run whatever
build/test/lint tooling this project actually uses (there may be no
orchestrator verify gate — then you own this check). If you can't get there,
revert your change rather than leave the tree broken. The next cycle starts
from whatever state you leave behind.

End with: DONE: <one-line summary of what you changed or why you stopped>"""


def stop_phrase() -> str:
    """The marker the agent emits that we treat as clean completion."""
    return "DONE:"
