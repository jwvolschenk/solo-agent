"""Directives — human -> agent feedback queue with full lifecycle.

The directive queue lives in ``<STATE_DIR>/directives.md`` as a sibling to the
other shared state files. Format (machine + human readable):

    ## DIRECTIVE d1  2026-07-07T14:32:00Z  priority:high
    status: pending

    Refactor the auth module to use the new JWT token format
    before touching the API routes.

Two convergent channels update status:
  1. File edit (primary): the agent edits the ``status:`` line. The state
     watcher re-parses -> upserts DB -> records transition -> broadcasts.
  2. HTTP PATCH (convenience): routes/directives.py updates the DB and rewrites
     the matching ``status:`` line in directives.md so both stay in sync.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import settings
from .db import next_directive_id, upsert_directive
from .models import Directive

log = logging.getLogger("solo.directives")

DIRECTIVE_FILE = "directives.md"

# Header:  ## DIRECTIVE <id>  <iso ts>  priority:<p>
_HEADER_RE = re.compile(
    r"^##\s+DIRECTIVE\s+(?P<id>d\d+)\s+(?P<ts>\S+)\s+priority:(?P<prio>high|normal|low)\s*$",
    re.IGNORECASE,
)
_STATUS_RE = re.compile(r"^status:\s*(?P<s>pending|acknowledged|done)\s*$", re.IGNORECASE)


def directives_path() -> Path:
    """Where directives.md lives: inside project_path so the agent sees it in
    its sandbox. (Previously this used state_dir, which was a different folder
    than where the agent works — the agent never saw the directives.)"""
    return Path(settings.project_path) / DIRECTIVE_FILE


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def parse_directives(content: str) -> list[Directive]:
    """Parse directives.md content into Directive objects.

    Tolerates missing status (defaults to pending) and empty bodies. Non-directive
    content (preamble, stray text) is ignored.
    """
    directives: list[Directive] = []
    current: Optional[Directive] = None
    body_lines: list[str] = []

    def _flush() -> None:
        nonlocal current, body_lines
        if current is not None:
            current.text = "\n".join(body_lines).strip()
            directives.append(current)
        current = None
        body_lines = []

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        m = _HEADER_RE.match(line)
        if m:
            _flush()
            try:
                created = datetime.fromisoformat(m["ts"].replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                created = datetime.utcnow()
            current = Directive(
                id=m["id"],
                created_at=created,
                priority=m["prio"].lower(),  # type: ignore[arg-type]
                text="",
                status="pending",
                raw="",
            )
            continue
        if current is not None:
            sm = _STATUS_RE.match(line.strip())
            if sm:
                current.status = sm["s"].lower()  # type: ignore[assignment]
            else:
                body_lines.append(raw_line)

    _flush()

    # attach raw block to each (best-effort: re-serialize)
    for d in directives:
        d.raw = serialize_directive(d)
    return directives


def load_directives() -> list[Directive]:
    """Read directives.md from disk and parse it. Returns [] if absent."""
    p = directives_path()
    if not p.exists():
        return []
    content = p.read_text(encoding="utf-8", errors="replace")
    return parse_directives(content)


# ---------------------------------------------------------------------------
# Write / mutate
# ---------------------------------------------------------------------------


def serialize_directive(d: Directive) -> str:
    """Render a directive back to its file block form."""
    ts = d.created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    body = d.text.strip()
    return (
        f"## DIRECTIVE {d.id}  {ts}  priority:{d.priority}\n"
        f"status: {d.status}\n\n"
        f"{body}\n"
    )


def _ensure_file() -> None:
    p = directives_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "# Directives\n\n"
            "# Human guidance queued for the agent. The agent reads this file\n"
            "# each loop and advances each directive pending -> acknowledged -> done.\n"
            "# Format:\n"
            "#   ## DIRECTIVE <id>  <ts>  priority:<high|normal|low>\n"
            "#   status: pending|acknowledged|done\n"
            "#   <freeform body>\n\n",
            encoding="utf-8",
        )


def append_directive(priority: str, text: str) -> Directive:
    """Create a new directive: mint id, append block to file, upsert DB."""
    _ensure_file()
    did = next_directive_id()
    d = Directive(
        id=did,
        created_at=datetime.utcnow(),
        priority=priority,  # type: ignore[arg-type]
        text=text.strip(),
        status="pending",
    )
    block = serialize_directive(d)
    with directives_path().open("a", encoding="utf-8") as f:
        f.write("\n" + block)
    upsert_directive(d)
    log.info("created directive %s (priority=%s)", did, priority)
    return d


def update_directive_status(did: str, status: str) -> Optional[Directive]:
    """Rewrite the status: line of a directive in directives.md + update DB.

    Returns the updated directive, or None if not found / file missing.
    """
    p = directives_path()
    if not p.exists():
        return None
    directives = load_directives()
    target = next((d for d in directives if d.id == did), None)
    if target is None:
        return None
    target.status = status  # type: ignore[assignment]
    target.raw = serialize_directive(target)
    # rewrite the whole file (small, low write rate)
    _rewrite_file(directives)
    upsert_directive(target)
    log.info("directive %s -> %s", did, status)
    return target


def _rewrite_file(directives: list[Directive]) -> None:
    """Overwrite directives.md with the given directive set, preserving the header."""
    p = directives_path()
    header_lines: list[str] = []
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if _HEADER_RE.match(line):
                break
            header_lines.append(line)
    if not header_lines:
        header_lines = ["# Directives\n"]
    body = "\n".join(serialize_directive(d) for d in directives)
    out = "\n".join(header_lines).rstrip() + "\n\n" + body
    p.write_text(out, encoding="utf-8")


def sync_to_db() -> list[Directive]:
    """Re-read directives.md and upsert every directive into the DB.

    Called by the file watcher when directives.md changes (agent edited a status line).
    """
    directives = load_directives()
    for d in directives:
        upsert_directive(d)
    return directives
