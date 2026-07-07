"""Artifact management — the three durable things that persist across cycles.

Per the Ralph + Reflexion design, ONLY these cross cycle boundaries:
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


def _workspace() -> Path:
    """Where artifacts live. For self-improving mode that's target_repo; for
    external targets we keep a sibling workspace dir to avoid polluting the target.

    Simplest correct behavior: put artifacts in target_repo (so the agent sees
    them in its working dir). This is the standard Ralph convention.
    """
    return Path(settings.target_repo)


def backlog_path() -> Path:
    return _workspace() / "backlog.md"


def reflections_path() -> Path:
    return _workspace() / "reflections.md"


def skills_dir() -> Path:
    return _workspace() / "skills"


def ensure_artifacts() -> None:
    """Create the artifact files/dirs if missing, with explanatory headers."""
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
