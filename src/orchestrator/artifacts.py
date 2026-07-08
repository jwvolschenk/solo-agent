"""Artifact management — the durable things that persist across cycles.

Per the Ralph + Reflexion design, these cross cycle boundaries (and ONLY these):
  - SOLO_AGENT.md     the loop constitution: role, cycle, artifact map, rules.
                      Written once by the orchestrator; read by every fresh
                      session so the agent knows how to execute the loop.
  - backlog.md        the PRD / task list (mutated by the plan phase)
  - reflections.md    append-only episodic memory of what worked / failed
  - skills/           index of reusable tests/snippets the agent produced

Everything else (the OpenCode session context) is wiped each cycle by design.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import settings

log = logging.getLogger("solo.artifacts")


# The loop constitution. Written verbatim into SOLO_AGENT.md in the target repo.
# Every fresh session reads this first to orient itself in the whole loop.
# Keep it tight — this is read every session, so every line is paid-for.
_SOLO_AGENT_MD = """\
# SOLO_AGENT.md — Loop Operating Protocol

You are the **worker** in an autonomous, 24/7 self-improvement loop (the "Ralph loop").
Each of your sessions is **fresh** — nothing carries over except what's on disk.
Read this file first, every time, to remember how to operate.

## The loop

The orchestrator (a separate process) drives this cycle, spawning a fresh session
for each phase. You never run the whole loop; you execute ONE phase and stop.

    REFLECT → PLAN → EXECUTE (per task) → VERIFY → RECORD → REFLECT ...

- **REFLECT**: survey the codebase, propose concrete improvement tasks.
- **PLAN**: structure the backlog — small, verifiable, ordered tasks.
- **EXECUTE**: implement ONE task per session, minimally and correctly.
- **VERIFY**: run by the ORCHESTRATOR (not you) — never self-attest completion.
- **RECORD**: orchestrator appends the outcome to reflections.md.

## Your memory (on disk)

Since your session is wiped each time, your only memory is these files:

| File | What it is | You should |
|---|---|---|
| `SOLO_AGENT.md` | this protocol | read first, every session |
| `reflections.md` | what's been tried, what worked/failed | read second — avoid repeating failures |
| `backlog.md` | the task list | REFLECT adds to it; EXECUTE pulls the next `- [ ]` task |
| `skills/INDEX.md` | reusable snippets/tests the loop produced | consult before implementing |

## Rules (non-negotiable)

1. **Fresh context.** Never assume state from a prior session — re-read the files.
2. **Stay in scope.** EXECUTE does ONE task. Don't refactor unrelated code.
3. **Never mark a task done.** The orchestrator verifies independently and decides.
   You may not check off backlog items or rewrite them to look complete.
4. **Never touch `main`.** All your work lands on the auto branch; the orchestrator
   manages git. Don't run git commands unless explicitly told to.
5. **Verifiability.** Each task you implement must be checkable (a test, a build,
   a type check). If you can't verify it, it's the wrong task.
6. **Be honest.** If a task is blocked, invalid, or you can't complete it — say so
   clearly in your final message. Don't pretend success.
7. **Reverts happen.** If verification fails, the orchestrator reverts your work.
   That's normal, not a failure — read reflections.md to learn why.

## Stop signal

When you finish your assigned phase/task, end your final message with a line
starting with `DONE:` followed by a one-line summary, e.g.:

    DONE: Fixed who_owes to accumulate prices for duplicate names.

Then stop. The orchestrator detects completion from this. Do not ask questions
or wait for further input — this runs unattended.
"""


def _workspace() -> Path:
    """Where artifacts live: inside project_path so the agent sees them in its
    working dir (its sandbox). This is the standard Ralph convention — backlog.md,
    reflections.md, skills/, and SOLO_AGENT.md sit alongside the code."""
    return Path(settings.project_path)


def solo_agent_path() -> Path:
    return _workspace() / "SOLO_AGENT.md"


def backlog_path() -> Path:
    return _workspace() / "backlog.md"


def reflections_path() -> Path:
    return _workspace() / "reflections.md"


def skills_dir() -> Path:
    return _workspace() / "skills"


def ensure_artifacts() -> None:
    """Create the artifact files/dirs if missing, with explanatory headers.

    SOLO_AGENT.md is (re)written every call to stay in sync with the protocol —
    it's orchestrator-owned, not agent-editable. The other artifacts are created
    once with headers and then left for the loop to mutate.
    """
    # SOLO_AGENT.md: always rewrite — it's the orchestrator's contract with the agent
    solo_agent_path().write_text(_SOLO_AGENT_MD, encoding="utf-8")

    if not backlog_path().exists():
        backlog_path().write_text(
            "# Backlog\n\n"
            "# Tasks the solo-agent loop works through. Each cycle: the reflect\n"
            "# phase regenerates candidates, the plan phase structures them here,\n"
            "# the execute phase picks the next unchecked item as a goal.\n\n"
            "- [ ] (sample) Add a README quickstart section\n",
            encoding="utf-8",
        )
    if not reflections_path().exists():
        reflections_path().write_text(
            "# Reflections\n\n"
            "# Append-only episodic memory. After each cycle, the orchestrator\n"
            "# records what was attempted and the verify outcome. Read at the\n"
            "# start of each fresh session so the agent doesn't repeat failures.\n\n",
            encoding="utf-8",
        )
    skills_dir().mkdir(parents=True, exist_ok=True)
    if not (skills_dir() / "INDEX.md").exists():
        (skills_dir() / "INDEX.md").write_text(
            "# Skill Index\n\nReusable artifacts produced across cycles.\n\n",
            encoding="utf-8",
        )


def read_backlog() -> str:
    p = backlog_path()
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def read_reflections() -> str:
    p = reflections_path()
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def append_reflection(entry: str, *, cycle: int, outcome: str, sha: Optional[str] = None) -> None:
    """Append a timestamped reflection entry. Never edits prior entries."""
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    sha_part = f" sha:{sha[:10]}" if sha else ""
    block = f"\n## Cycle {cycle}  {ts}  outcome:{outcome}{sha_part}\n\n{entry.strip()}\n"
    with reflections_path().open("a", encoding="utf-8") as f:
        f.write(block)
    log.info("appended reflection for cycle %d (%s)", cycle, outcome)


def read_skill_index() -> str:
    p = skills_dir() / "INDEX.md"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
