"""Artifact management — the durable things that persist across cycles.

Per the Ralph + Reflexion design, these cross cycle boundaries (and ONLY these):
  - SOLO_AGENT.md     the loop constitution: role, cycle, artifact map, rules.
                      Written once by the orchestrator; read by every fresh
                      session so the agent knows how to execute the loop.
  - backlog.md              executor queue — one-session tasks only (PLAN writes, EXECUTE reads)
  - backlog-candidates.md   planner inbox — coarse themes from REFLECT + orchestrator seeds
  - reflections.md    rolling recent memory; older entries in reflections-archive/
  - skills/           index of reusable tests/snippets the agent produced
  - CODEDB.md         project-specific codedb navigation guide (agent-maintained;
                      auto-loaded via .opencode/opencode.json instructions)

Everything else (the OpenCode session context) is wiped each cycle by design.
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import settings

log = logging.getLogger("solo.artifacts")

_REFLECTIONS_PREAMBLE = (
    "# Reflections\n\n"
    "# Recent-cycle memory. The orchestrator keeps the last entries here;\n"
    "# older ones move to reflections-archive/. Read this file each session\n"
    "# to avoid repeating failures — it is intentionally short.\n\n"
)


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
      ARCHIVE done items → REFLECT (find candidates) → PLAN (decompose & order)
      → refill backlog → repeat

- **EXECUTE** (most cycles): the orchestrator picks the **next** `- [ ]` task from
  backlog.md and asks you to implement it — **one task per cycle**. Finish the
  backlog one item at a time; reflection only happens when it's clear.
- **REFLECT** (only when backlog is empty): survey the project vs. the goal and
  append coarse candidates to `backlog-candidates.md` (not backlog.md). Pending
  human directives go straight to `backlog.md` as ready work. The orchestrator
  archives completed items into `backlog-history/` before calling you.
- **PLAN** (immediately after REFLECT): read `backlog-candidates.md`, decompose
  each theme into small one-session tasks in `backlog.md`, then clear the
  candidates file. The executor never reads backlog-candidates.md.
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
| `reflections.md` | recent failures + reflect insights (bounded) | read each session — avoid repeating mistakes |
| `backlog-candidates.md` | planner inbox (coarse themes) | REFLECT + orchestrator seeds append here; PLAN consumes and clears |
| `backlog.md` | executor queue (one-session tasks) | PLAN writes ready tasks; EXECUTE pulls the next `- [ ]` |
| `skills/INDEX.md` | reusable snippets/tests the loop produced | consult before implementing |
| `CODEDB.md` | codedb MCP navigation for *this* repo | auto-loaded every session — prefer codedb over grep; update as you learn |

## Directives (human steering)

`directives.md` contains guidance queued by a human mid-loop. Each has a status:
`pending` → `acknowledged` → `done`.

- **Read it every session.** Pending (`status: pending`) directives take priority
  over backlog tasks — address them first.
- When you start working on a directive, edit its `status:` line to `acknowledged`.
- When you complete it, edit the `status:` line to `done`.
- The human uses these to steer you: "use JWT not sessions", "focus on tests next",
  "this bug is critical", etc.
- A common directive: **review and improve `CODEDB.md`** with navigation patterns,
  entry points, and lessons learned while working in this codebase.

## Rules (non-negotiable)

1. **Fresh context.** Never assume state from a prior session — re-read the files.
2. **Backlog-first.** Don't propose new work while backlog items are pending.
   The executor takes one backlog item per cycle; reflection only happens when
   the executor queue is clear.
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
   That's normal — read reflections.md (recent memory only) to learn why.

## Stop signal

When you finish your assigned phase/task, end your final message with a line
starting with `DONE:` followed by a one-line summary, e.g.:

    DONE: Scaffolded the Godot project with base scene + main script.

Then stop. The orchestrator detects completion from this. Do not ask questions
or wait for further input — this runs unattended.
"""

# Agent-maintained navigation guide. Seeded once; OpenCode auto-loads it via
# .opencode/opencode.json instructions. Agents (or a human directive) flesh out
# the project-specific sections as they learn the codebase.
_CODEDB_MD_STUB = """\
# CODEDB.md — Code Navigation for This Repo

OpenCode auto-loads this file every session (via `.opencode/opencode.json`).
Use the **codedb MCP tools** to explore this codebase — they are indexed,
symbol-aware, and faster than blind grep/glob.

## Tool cheat sheet (generic)

| Tool | When to use |
|---|---|
| `codedb_outline` | Before reading a large file — see functions/structs/imports |
| `codedb_symbol` | Jump to where a type or function is **defined** |
| `codedb_word` | Every occurrence of an exact identifier |
| `codedb_callers` | Who calls / references a symbol |
| `codedb_deps` | Import blast radius for a file |
| `codedb_search` | Substring or regex search across the index |
| `codedb_query` | Chain find → outline → read in one call |
| `codedb_index` | (Re)build the index after big structural changes |

Prefer codedb over grep/find when you have an identifier or know the file shape.

## Index setup

- Index command: `codedb index <project-root>` (or MCP `codedb_index`)
- Ignore rules: `.codedbignore` (if present)
- Re-index when: parsers change, `.codedbignore` changes, or search feels stale

*(Fill in project-root path and any index quirks below.)*

## Entry points

*(List the main modules, routes, or subsystems a new session should know about.
Example: `src/main.py` → FastAPI app, `src/orchestrator/controller.py` → Ralph loop.)*

## Navigation patterns

*(Project-specific recipes, e.g. "for API routes use codedb_routes", "start
changes with codedb_deps on the file you are editing", "hot files from
codedb_hot".)*

## Gotchas

*(Build layout, generated code paths to skip, monorepo package boundaries, etc.)*

---

**Maintainers:** update this file as you learn the codebase. Humans can queue a
directive in `directives.md` to review and improve it. Keep it concise — every
line is paid for in every session.
"""

# Instruction files the orchestrator ensures are listed in opencode.json.
_OPENCODE_INSTRUCTION_FILES = ("CODEDB.md",)


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


def candidates_path() -> Path:
    return _workspace() / "backlog-candidates.md"


# Lines in backlog.md matching this are planner-only and must not reach EXECUTE.
_SEED_MARKERS = ("(orchestrator seed", "(orchestrator-injected")


def is_planner_only_task(text: str) -> bool:
    """True for orchestrator seed lines that belong in backlog-candidates.md."""
    lowered = text.lower()
    return any(marker in lowered for marker in _SEED_MARKERS)


def reflections_path() -> Path:
    return _workspace() / "reflections.md"


def skills_dir() -> Path:
    return _workspace() / "skills"


def codedb_path() -> Path:
    return _workspace() / "CODEDB.md"


def opencode_config_path() -> Path:
    return _workspace() / ".opencode" / "opencode.json"


def ensure_codedb_guide() -> None:
    """Create CODEDB.md if missing. Agent-maintained after first seed."""
    path = codedb_path()
    if not path.exists():
        path.write_text(_CODEDB_MD_STUB, encoding="utf-8")
        log.info("seeded %s", path)


def ensure_opencode_instructions() -> None:
    """Ensure .opencode/opencode.json wires CODEDB.md + codedb MCP.

    Merges into an existing config (preserves provider, permissions, etc.).
    Seeds ``mcp.codedb`` only when missing — never overwrites a custom entry.
    Idempotent.
    """
    config_path = opencode_config_path()
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("could not parse %s; skipping opencode merge", config_path)
            return
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"$schema": "https://opencode.ai/config.json"}

    changed = False

    raw = data.get("instructions")
    if raw is None:
        instructions: list[str] = []
    elif isinstance(raw, str):
        instructions = [raw]
    elif isinstance(raw, list):
        instructions = [str(x) for x in raw]
    else:
        log.warning("opencode.json instructions is not a list; skipping instructions merge")
        instructions = []

    for name in _OPENCODE_INSTRUCTION_FILES:
        if name not in instructions:
            instructions.insert(0, name)
            changed = True

    if changed or "instructions" not in data:
        data["instructions"] = instructions

    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        mcp = None

    if mcp is None or "codedb" not in mcp:
        cmd = _resolve_codedb_mcp_command()
        if cmd is None:
            log.warning(
                "codedb binary not found (set CODEDB_MCP_COMMAND); "
                "skipping mcp.codedb seed in %s",
                config_path,
            )
        else:
            if mcp is None:
                mcp = {}
            mcp["codedb"] = {"type": "local", "command": cmd}
            data["mcp"] = mcp
            changed = True

    if not changed:
        return

    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    log.info(
        "updated %s (instructions=%s, mcp.codedb=%s)",
        config_path,
        instructions,
        isinstance(data.get("mcp"), dict) and "codedb" in data["mcp"],
    )


def _resolve_codedb_mcp_command() -> Optional[list[str]]:
    """Argv for the codedb MCP server, or None if not discoverable."""
    override = settings.codedb_mcp_command.strip()
    if override:
        return shlex.split(override)
    binary = shutil.which("codedb")
    if binary:
        return [binary, "mcp"]
    return None


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
            "# Executor queue: one-session tasks only. PLAN writes here after\n"
            "# decomposing themes from backlog-candidates.md.\n\n",
            encoding="utf-8",
        )
    if not candidates_path().exists():
        candidates_path().write_text(
            "# Backlog Candidates\n\n"
            "# Planner inbox: coarse themes from REFLECT and orchestrator seeds.\n"
            "# PLAN decomposes these into backlog.md, then clears this file.\n"
            "# The executor never reads this file.\n\n",
            encoding="utf-8",
        )
    if not reflections_path().exists():
        reflections_path().write_text(_REFLECTIONS_PREAMBLE, encoding="utf-8")
    else:
        compact_reflections()
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
    ensure_codedb_guide()
    ensure_opencode_instructions()
    relocate_stale_seeds()


def read_backlog() -> str:
    p = backlog_path()
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def read_candidates() -> str:
    p = candidates_path()
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def _append_line(path: Path, line: str) -> None:
    content = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    sep = "\n" if content and not content.endswith("\n") else ""
    path.write_text(content + sep + line + "\n", encoding="utf-8")


def relocate_stale_seeds() -> int:
    """Move orchestrator seed lines from backlog.md into backlog-candidates.md.

    Handles legacy state where seeds were written directly to the executor queue.
    Returns the number of lines relocated.
    """
    content = read_backlog()
    if not content:
        return 0
    moved: list[str] = []
    keep: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- [", "* [")) and is_planner_only_task(stripped):
            moved.append(stripped)
        else:
            keep.append(line)
    if not moved:
        return 0
    for item in moved:
        _append_line(candidates_path(), item)
    backlog_path().write_text("\n".join(keep).rstrip() + ("\n" if keep else ""), encoding="utf-8")
    log.info("relocated %d planner seed(s) from backlog.md to backlog-candidates.md", len(moved))
    return len(moved)


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


def append_fallback_candidate(cycle: int) -> str:
    """Append one coarse orchestrator seed to backlog-candidates.md for PLAN.

    Called when REFLECT produces no candidates. PLAN decomposes this into
    one-session tasks in backlog.md. The executor never sees this file.
    """
    category = _FALLBACK_CATEGORIES[cycle % len(_FALLBACK_CATEGORIES)]
    task = (
        f"- [ ] (orchestrator seed, cycle {cycle}) Next improvement theme: "
        f"{category} — survey the project vs. GOAL.md and queue concrete work"
    )
    _append_line(candidates_path(), task)
    log.info("[cycle %d] seeded planner candidate: %s", cycle, category)
    return task


def append_fallback_task(cycle: int) -> str:
    """Deprecated alias — seeds go to backlog-candidates.md, not backlog.md."""
    return append_fallback_candidate(cycle)


def history_dir() -> Path:
    """Where dated backlog archives live."""
    d = _workspace() / "backlog-history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def reflections_archive_dir() -> Path:
    """Where rolled-off reflection entries are stored."""
    d = _workspace() / "reflections-archive"
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


def should_record_execute_reflection(*, tasks_attempted: int, tasks_passed: int) -> bool:
    """Whether an execute cycle warrants an append to reflections.md.

    Successful all-pass cycles are recorded in the cycles DB only — repeating
    "Executed N tasks; N completed" in reflections.md bloats agent context
    without helping future sessions avoid failures.
    """
    if tasks_attempted == 0:
        return False
    return tasks_passed < tasks_attempted


def append_reflection(entry: str, *, cycle: int, outcome: str, sha: Optional[str] = None) -> None:
    """Append a timestamped reflection entry, then trim to the rolling window."""
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    sha_part = f" sha:{sha[:10]}" if sha else ""
    block = f"\n## Cycle {cycle}  {ts}  outcome:{outcome}{sha_part}\n\n{entry.strip()}\n"
    with reflections_path().open("a", encoding="utf-8") as f:
        f.write(block)
    log.info("appended reflection for cycle %d (%s)", cycle, outcome)
    compact_reflections()


def compact_reflections(
    max_entries: Optional[int] = None,
    *,
    workspace: Optional[Path] = None,
) -> int:
    """Keep only the most recent reflection entries; archive the rest.

    Returns the number of entries moved to reflections-archive/.
    """
    limit = settings.reflections_max_entries if max_entries is None else max_entries
    if limit <= 0:
        return 0

    ws = Path(workspace) if workspace is not None else _workspace()
    path = ws / "reflections.md"
    if not path.exists():
        return 0

    from ..state_reader import parse_reflections

    content = path.read_text(encoding="utf-8", errors="replace")
    entries = parse_reflections(content)
    if len(entries) <= limit:
        return 0

    archive_entries = entries[: len(entries) - limit]
    keep_entries = entries[len(entries) - limit :]

    first_raw = entries[0].raw
    idx = content.find(first_raw)
    preamble = content[:idx] if idx > 0 else _REFLECTIONS_PREAMBLE

    today = datetime.utcnow().strftime("%Y-%m-%d")
    archive_dir = ws / "reflections-archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"reflections-{today}.md"
    with archive_path.open("a", encoding="utf-8") as f:
        f.write(f"\n## Archived {today} ({len(archive_entries)} entries)\n\n")
        for entry in archive_entries:
            f.write(entry.raw + "\n")

    new_content = preamble.rstrip() + "\n"
    for entry in keep_entries:
        new_content += "\n" + entry.raw + "\n"
    path.write_text(new_content.rstrip() + "\n", encoding="utf-8")
    log.info(
        "compacted reflections at %s: archived %d, kept %d (limit %d)",
        ws,
        len(archive_entries),
        len(keep_entries),
        limit,
    )
    return len(archive_entries)


def read_skill_index() -> str:
    p = skills_dir() / "INDEX.md"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
