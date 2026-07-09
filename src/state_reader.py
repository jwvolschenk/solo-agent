"""State file reader — parses the shared workspace markdown.

Reads tasks.md, journal.md, plan.md, summaries/*.md, and directives.md from the
mounted STATE_DIR. Uses an mtime cache so unchanged files aren't re-parsed on
every poll.

Markdown conventions parsed here:
  tasks.md      checkbox lines:  - [ ] todo / - [x] done / - [~] in_progress / - [!] blocked
  journal.md    bullet lines:    - entry text
  plan.md / *.md  raw content (no structured parsing)
  directives.md  handled by directives.py (this module just exposes the raw file)
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path

from .config import settings
from .models import JournalEntry, ReflectionEntry, StateFile, TaskItem

log = logging.getLogger("solo.state")

_cache: dict[Path, float] = {}  # path -> last parsed mtime


def _read_file(path: Path) -> StateFile:
    """Read a file into a StateFile. Updates the mtime cache on success."""
    sf = StateFile(name=path.name, path=str(path))
    if not path.exists():
        _cache.pop(path, None)
        return sf
    try:
        st = path.stat()
    except OSError as e:
        log.warning("stat %s failed: %s", path, e)
        return sf
    sf.exists = True
    sf.size = st.st_size
    sf.mtime = datetime.utcfromtimestamp(st.st_mtime)
    # read text
    try:
        sf.content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("read %s failed: %s", path, e)
        return sf
    _cache[path] = st.st_mtime
    return sf


def _changed(path: Path) -> bool:
    """True if path is new or its mtime advanced since the last parse."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _cache.pop(path, None)
        return True  # treat missing as "changed" so callers see exists=False
    return _cache.get(path, -1.0) != mtime


def parse_tasks(content: str) -> list[TaskItem]:
    """Parse checkbox markdown into TaskItem list.

    Recognised markers (in order): [x] done, [~] in_progress, [!] blocked, [ ] todo.
    Lines without a checkbox marker are ignored.
    """
    items: list[TaskItem] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line.startswith("- [") and not line.startswith("* ["):
            continue
        # marker is the char inside [ ]
        try:
            marker = line[3]
        except IndexError:
            continue
        text = line[5:].strip() if len(line) > 5 else ""
        status = {
            "x": "done",
            "~": "in_progress",
            "!": "blocked",
            " ": "todo",
        }.get(marker)
        if status is None:
            continue
        items.append(TaskItem(status=status, text=text, raw=raw_line))  # type: ignore[arg-type]
    return items


def parse_journal(content: str) -> list[JournalEntry]:
    """Parse bullet markdown into JournalEntry list. One entry per bullet line."""
    out: list[JournalEntry] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("- ") or line.startswith("* "):
            out.append(JournalEntry(text=line[2:].strip(), raw=raw_line))
    return out


_REFLECTION_HEADER = re.compile(
    r"^## Cycle (\d+)\s+(\S+)\s+outcome:(\w+)(?:\s+sha:(\S+))?\s*$",
    re.MULTILINE,
)


def parse_reflections(content: str) -> list[ReflectionEntry]:
    """Parse reflections.md blocks headed by ``## Cycle N ... outcome:...``."""
    out: list[ReflectionEntry] = []
    matches = list(_REFLECTION_HEADER.finditer(content))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()
        out.append(
            ReflectionEntry(
                cycle=int(match.group(1)),
                timestamp=match.group(2),
                outcome=match.group(3),
                sha=match.group(4),
                text=body,
                raw=content[match.start() : end].strip(),
            )
        )
    return out


def read_tasks() -> StateFile:
    p = settings.state_dir / "tasks.md"
    return _read_with(p, parse_tasks, "tasks")


def read_journal() -> StateFile:
    p = settings.state_dir / "journal.md"
    return _read_with(p, parse_journal, "entries")


def read_plan() -> StateFile:
    p = settings.state_dir / "plan.md"
    return _read_file(p)


def read_backlog() -> StateFile:
    """Read backlog.md from project_path (where the orchestrator's agent writes it)."""
    p = Path(settings.project_path) / "backlog.md"
    return _read_with(p, parse_tasks, "tasks")


def read_reflections() -> StateFile:
    """Read reflections.md from project_path (the agent's episodic memory)."""
    p = Path(settings.project_path) / "reflections.md"
    sf = _read_file(p)
    if sf.content:
        try:
            sf.reflections = parse_reflections(sf.content)
        except Exception as e:
            log.warning("parse %s failed: %s", p, e)
    return sf


def read_file(name: str) -> StateFile:
    """Read an arbitrary state file by name (no structured parsing)."""
    # disallow escaping state_dir
    safe = Path(name).name
    p = settings.state_dir / safe
    return _read_file(p)


def read_file_changed(name: str) -> tuple[StateFile, bool]:
    """Read a state file + report whether it changed since last read."""
    safe = Path(name).name
    p = settings.state_dir / safe
    changed = _changed(p)
    sf = _read_file(p)
    return sf, changed


def list_summaries() -> list[StateFile]:
    """List all *.md under state_dir/summaries/, newest first."""
    sdir = settings.state_dir / "summaries"
    if not sdir.is_dir():
        return []
    out: list[StateFile] = []
    for entry in sorted(sdir.iterdir(), key=os.path.getmtime, reverse=True):
        if entry.is_file() and entry.suffix == ".md":
            out.append(_read_file(entry))
    return out


def read_summary(name: str) -> StateFile:
    safe = Path(name).name
    p = settings.state_dir / "summaries" / safe
    return _read_file(p)


def all_state_files() -> dict[str, StateFile]:
    """Convenience: read the standard set of state files at once."""
    return {
        "tasks": read_tasks(),
        "journal": read_journal(),
        "plan": read_plan(),
    }


# ---------------------------------------------------------------------------


def _read_with(p: Path, parser, field: str) -> StateFile:
    sf = _read_file(p)
    if sf.content:
        try:
            setattr(sf, field, parser(sf.content))
        except Exception as e:
            log.warning("parse %s failed: %s", p, e)
    return sf
