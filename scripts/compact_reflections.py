#!/usr/bin/env python3
"""One-shot compaction for reflections.md in a target project repo.

Moves older entries to reflections-archive/ and keeps the rolling window
(configured via REFLECTIONS_MAX_ENTRIES, default 15).

Usage:
    python scripts/compact_reflections.py /path/to/project
    python scripts/compact_reflections.py /path/to/project --max 10
    PROJECT_PATH=/path/to/project python scripts/compact_reflections.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.config import settings  # noqa: E402
from src.orchestrator import artifacts  # noqa: E402
from src.state_reader import parse_reflections  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact reflections.md to a rolling window.")
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=settings.project_path,
        help="Project repo path (default: PROJECT_PATH env / settings)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help=f"Entries to keep (default: {settings.reflections_max_entries})",
    )
    args = parser.parse_args()

    project = args.path.expanduser().resolve()
    reflections = project / "reflections.md"
    if not reflections.exists():
        print(f"no reflections.md at {reflections}", file=sys.stderr)
        return 1

    before = len(parse_reflections(reflections.read_text(encoding="utf-8")))
    before_bytes = reflections.stat().st_size
    archived = artifacts.compact_reflections(max_entries=args.max, workspace=project)
    after = len(parse_reflections(reflections.read_text(encoding="utf-8")))
    after_bytes = reflections.stat().st_size
    limit = args.max if args.max is not None else settings.reflections_max_entries

    print(f"project:  {project}")
    print(f"limit:    {limit}")
    print(f"entries:  {before} -> {after} (archived {archived})")
    print(f"size:     {before_bytes} -> {after_bytes} bytes")
    if archived:
        archive_dir = project / "reflections-archive"
        latest = sorted(archive_dir.glob("reflections-*.md"))[-1] if archive_dir.is_dir() else None
        if latest:
            print(f"archive:  {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
