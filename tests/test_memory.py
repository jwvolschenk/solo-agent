"""Memory brief tests — bounded, high-signal prompt context."""

from src.orchestrator.memory import build_memory_brief


def test_build_memory_brief_prefers_failures_and_reflect(tmp_settings, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "memory_brief_max_items", 2)
    monkeypatch.setattr(settings, "memory_brief_item_max_chars", 120)
    monkeypatch.setattr(settings, "memory_brief_max_chars", 500)

    (tmp_settings.project_path / "reflections.md").write_text(
        "# Reflections\n\n"
        "## Cycle 1  2026-07-09T10:00:00Z  outcome:passed\n\n"
        "Executed 3 backlog task(s); 3 completed.\n\n"
        "## Cycle 2  2026-07-09T11:00:00Z  outcome:failed\n\n"
        "VERIFY FAIL, reverting: tests broken.\n\n"
        "## Cycle 3  2026-07-09T12:00:00Z  outcome:pending\n\n"
        "Reflect phase: missing gameplay loop and coverage.\n",
        encoding="utf-8",
    )

    brief = build_memory_brief()
    assert brief.startswith("RECENT MEMORY (curated by orchestrator):")
    assert "cycle 2 [failed]" in brief
    assert "cycle 3 [pending]" in brief
    assert "cycle 1 [passed]" not in brief


def test_build_memory_brief_honors_disabled_limits(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "memory_brief_max_chars", 0)
    assert build_memory_brief() == ""
