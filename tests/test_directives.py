"""Directive tests — create, parse, lifecycle, dual channel."""

import pytest

from src.directives import (
    append_directive,
    directives_path,
    load_directives,
    parse_directives,
    serialize_directive,
    update_directive_status,
)
from src.models import Directive


def _sample_directive(id="d1", status="pending", priority="normal", text="do X"):
    from datetime import datetime
    return Directive(id=id, created_at=datetime(2026, 7, 7, 14, 32, 0),
                     priority=priority, text=text, status=status)  # type: ignore[arg-type]


def test_parse_directive_block():
    content = """# Directives

## DIRECTIVE d1  2026-07-07T14:32:00Z  priority:high
status: pending

Refactor the auth module.

## DIRECTIVE d2  2026-07-07T15:00:00Z  priority:low
status: done

Small tweak.
"""
    dirs = parse_directives(content)
    assert len(dirs) == 2
    assert dirs[0].id == "d1"
    assert dirs[0].priority == "high"
    assert dirs[0].status == "pending"
    assert "Refactor the auth" in dirs[0].text
    assert dirs[1].status == "done"


def test_parse_directive_missing_status_defaults_pending():
    content = "## DIRECTIVE d1  2026-07-07T14:32:00Z  priority:normal\n\nbody only\n"
    dirs = parse_directives(content)
    assert len(dirs) == 1
    assert dirs[0].status == "pending"


def test_parse_directive_ignores_non_directive_content():
    content = "# Directives\n\nsome preamble\n\n## DIRECTIVE d1  2026-07-07T14:32:00Z  priority:normal\nstatus: pending\n\nbody\n"
    dirs = parse_directives(content)
    assert len(dirs) == 1
    assert dirs[0].id == "d1"


def test_serialize_roundtrip():
    d = _sample_directive(priority="high", text="do thing")
    out = serialize_directive(d)
    assert "## DIRECTIVE d1" in out
    assert "priority:high" in out
    assert "status: pending" in out
    assert "do thing" in out
    # parse it back
    reparsed = parse_directives(out)
    assert len(reparsed) == 1
    assert reparsed[0].priority == "high"


def test_append_directive_creates_file_and_mints_id(tmp_settings):
    d1 = append_directive("high", "first directive")
    assert d1.id == "d1"
    assert d1.status == "pending"
    assert directives_path().exists()

    d2 = append_directive("normal", "second directive")
    assert d2.id == "d2"

    # both persisted in file
    loaded = load_directives()
    assert len(loaded) == 2
    assert loaded[1].id == "d2"


def test_update_directive_status_rewrites_file(tmp_settings):
    append_directive("normal", "task one")
    append_directive("normal", "task two")

    updated = update_directive_status("d1", "acknowledged")
    assert updated is not None
    assert updated.status == "acknowledged"

    # reload from disk to confirm file was rewritten
    loaded = load_directives()
    assert loaded[0].status == "acknowledged"
    assert loaded[1].status == "pending"  # untouched


def test_update_status_unknown_directive_returns_none(tmp_settings):
    append_directive("normal", "task")
    assert update_directive_status("d99", "done") is None


def test_update_status_when_file_missing_returns_none(tmp_settings):
    assert update_directive_status("d1", "done") is None


def test_full_lifecycle(tmp_settings):
    d = append_directive("high", "implement feature X")
    assert d.status == "pending"

    update_directive_status(d.id, "acknowledged")
    assert load_directives()[0].status == "acknowledged"

    update_directive_status(d.id, "done")
    assert load_directives()[0].status == "done"
