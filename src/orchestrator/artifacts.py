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

## Your mission

`GOAL.md` states the overarching goal for this project. Every cycle advances it.
Read it every session alongside this file. The goal may be to build a whole project
from scratch or to improve an existing one — adapt accordingly.

## The loop

The orchestrator (a separate process) drives this loop. It spawns a fresh session
for each task or reflection. The loop is **backlog-first**:

    EXECUTE pending tasks → ... → when backlog is clear:
      ARCHIVE done items → REFLECT to find new work → refill backlog → repeat

- **EXECUTE** (most cycles): the orchestrator picks the next `- [ ]` task from
  backlog.md and asks you to implement it. Churn through ALL pending tasks first.
- **REFLECT** (only when backlog is empty): survey the project vs. the goal and
  propose the next round of tasks to refill the backlog. The orchestrator archives
  completed items into `backlog-history/` before calling you.
- **VERIFY**: run by the ORCHESTRATOR *only if a verify command is configured*.
  Otherwise YOU own verification — run whatever build/test/lint/check this project
  uses before declaring a task complete.

This means you should NOT propose new enhancements while there's outstanding work.
Finish the backlog first; new work is only sought when the slate is clear.

## Your memory (on disk)

Since your session is wiped each time, your only memory is these files:

| File | What it is | You should |
|---|---|---|
| `GOAL.md` | the overarching goal for the project | read every session — it drives all work |
| `SOLO_AGENT.md` | this protocol | read first, every session |
| `directives.md` | human guidance queued for you | read every session — pending directives are PRIORITY work |
| `reflections.md` | what's been tried, what worked/failed | read second — avoid repeating failures |
| `backlog.md` | the task list | REFLECT adds to it; EXECUTE pulls the next `- [ ]` task |
| `skills/INDEX.md` | reusable snippets/tests the loop produced | consult before implementing |

## Directives (human steering)

`directives.md` contains guidance queued by a human mid-loop. Each has a status:
`pending` → `acknowledged` → `done`.

- **Read it every session.** Pending (`status: pending`) directives take priority
  over backlog tasks — address them first.
- When you start working on a directive, edit its `status:` line to `acknowledged`.
- When you complete it, edit the `status:` line to `done`.
- The human uses these to steer you: "use JWT not sessions", "focus on tests next",
  "this bug is critical", etc.

## Rules (non-negotiable)

1. **Fresh context.** Never assume state from a prior session — re-read the files.
2. **Backlog-first.** Don't propose new work while backlog items are pending.
   Churn through the existing backlog; reflection only happens when it's clear.
3. **Mark tasks done.** When you complete a task (or find it's already done),
   edit its backlog.md line from `- [ ]` to `- [x]`. This is required — it's
   how progress is tracked.
4. **Directives are priority.** Address pending directives before other backlog work.
5. **Stay in scope.** EXECUTE does ONE task. Don't refactor unrelated code.
6. **Never touch `main` / run git unless told.** The orchestrator manages git.
7. **Leave it working — non-negotiable.** End every session with the project in
   a known-good state: it must build/compile and its existing tests must pass.
   Detect and run whatever build/test/lint tooling this project actually uses
   (there may be no orchestrator verify gate — then you own this check). If you
   can't get there, revert your own change rather than leave the tree broken,
   and say so in your DONE: summary. The next cycle assumes it's starting from
   a working system.
8. **Be honest.** If a task is blocked, invalid, or you can't complete it — say so
   clearly in your final message. Don't pretend success.
9. **Reverts can happen.** If an orchestrator gate fails, your work may be reverted.
   That's normal — read reflections.md to learn why.

## Stop signal

When you finish your assigned phase/task, end your final message with a line
starting with `DONE:` followed by a one-line summary, e.g.:

    DONE: Scaffolded the Godot project with base scene + main script.

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


def goal_path() -> Path:
    return _workspace() / "GOAL.md"


def backlog_path() -> Path:
    return _workspace() / "backlog.md"


def reflections_path() -> Path:
    return _workspace() / "reflections.md"


def skills_dir() -> Path:
    return _workspace() / "skills"


def ensure_artifacts() -> None:
    """Create the artifact files/dirs if missing, with explanatory headers.

    SOLO_AGENT.md is (re)written every call to stay in sync with the protocol —
    it's orchestrator-owned, not agent-editable. GOAL.md is (re)written from
    settings.goal each call too (the orchestrator owns the goal; the agent reads
    it). backlog.md/reflections.md/skills/ are created once and left for the
    loop to mutate.
    """
    # SOLO_AGENT.md + GOAL.md: orchestrator-owned, rewritten every call
    solo_agent_path().write_text(_SOLO_AGENT_MD, encoding="utf-8")
    write_goal(settings.goal)

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
    # directives.md: created with a header if missing. The directives module owns
    # the per-block format; we just ensure the file exists so the agent sees it.
    dir_path = _workspace() / "directives.md"
    if not dir_path.exists():
        dir_path.write_text(
            "# Directives\n\n"
            "# Human guidance queued for the agent. Each directive has a status:\n"
            "# pending -> acknowledged -> done. The agent reads this every session\n"
            "# and advances the status line as it works through them.\n\n",
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


# Rotating, project-agnostic categories for the orchestrator-injected fallback
# task (see append_fallback_task). Generic on purpose: the orchestrator has no
# idea what kind of project it's driving, so these must apply to anything.
_FALLBACK_CATEGORIES = [
    "performance and efficiency — profile or reason about hot paths and optimize one",
    "code quality and tech debt — refactor a messy/overgrown area for clarity",
    "test coverage — find an undertested area and add meaningful tests",
    "documentation — improve docs, comments, or onboarding material where it's weakest",
    "security and hardening — look for and fix a weak input-handling or trust boundary",
    "error handling and resilience — find a fragile path and make it fail gracefully",
    "dependency and tooling freshness — check for stale/vulnerable dependencies",
    "developer experience — improve tooling, scripts, or setup friction",
]


def append_fallback_task(cycle: int) -> str:
    """Append one orchestrator-authored, project-agnostic backlog task.

    Called when the REFLECT phase comes back with zero new tasks — rather than
    pausing the loop and waiting for a human, the orchestrator directs the
    agent itself so the 24/7 loop keeps moving. The category rotates
    deterministically by cycle number (no randomness) so repeated empty
    reflects don't just repeat the same nudge. Returns the appended task text.
    """
    category = _FALLBACK_CATEGORIES[cycle % len(_FALLBACK_CATEGORIES)]
    task = (
        f"- [ ] (orchestrator-injected, cycle {cycle}) Reflect found no new work — "
        f"survey the project for a {category} improvement, and implement one "
        f"concrete, high-value change."
    )
    content = read_backlog()
    sep = "\n" if content and not content.endswith("\n") else ""
    backlog_path().write_text(content + sep + task + "\n", encoding="utf-8")
    log.info("[cycle %d] injected fallback backlog task: %s", cycle, category)
    return task


def history_dir() -> Path:
    """Where dated backlog archives live."""
    d = _workspace() / "backlog-history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def archive_backlog() -> int:
    """Move completed (`- [x]`) backlog items into a dated history file.

    Reads backlog.md, extracts all `- [x]` lines, writes them to
    backlog-history/backlog-YYYY-MM-DD.md, and rewrites backlog.md with only
    the remaining (unchecked) items. Returns the number of items archived.

    Called by the controller when the backlog has no pending items — the cycle
    has cleared all goals, so we archive the evidence and start fresh.
    """
    content = read_backlog()
    if not content:
        return 0
    lines = content.splitlines()
    done_items: list[str] = []
    keep_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        # match: - [x] ... or * [x] ...
        if (stripped.startswith("- [x]") or stripped.startswith("* [x]")):
            done_items.append(stripped)
        else:
            keep_lines.append(line)

    if not done_items:
        return 0  # nothing done to archive

    # write the archive
    today = datetime.utcnow().strftime("%Y-%m-%d")
    archive_path = history_dir() / f"backlog-{today}.md"
    # append if the file already exists (same day)
    with archive_path.open("a", encoding="utf-8") as f:
        f.write(f"\n## Archived {today}\n\n")
        for item in done_items:
            f.write(item + "\n")

    # rewrite backlog.md without the done items
    backlog_path().write_text(
        "\n".join(keep_lines).rstrip() + "\n", encoding="utf-8"
    )
    log.info("archived %d completed backlog items to %s", len(done_items), archive_path.name)
    return len(done_items)


def write_goal(goal: str) -> None:
    """Write the overarching goal to GOAL.md. Orchestrator-owned.

    A missing/empty goal still writes a placeholder file so the agent sees the
    slot exists, but the controller refuses to start cycling until one is set.
    """
    body = goal.strip() or "(No goal set yet. The orchestrator will set one before cycling.)"
    goal_path().write_text(f"# Goal\n\n{body}\n", encoding="utf-8")


def read_goal() -> str:
    p = goal_path()
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
