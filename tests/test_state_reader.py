"""State reader tests — markdown parsing for tasks/journal."""

from src.state_reader import parse_journal, parse_reflections, parse_tasks
from src.orchestrator.artifacts import should_record_execute_reflection


def test_parse_tasks_recognizes_all_markers():
    content = """# Tasks

- [ ] pending task
- [x] done task
- [~] in progress task
- [!] blocked task
- not a task
"""
    items = parse_tasks(content)
    assert len(items) == 4
    statuses = [i.status for i in items]
    assert statuses == ["todo", "done", "in_progress", "blocked"]
    assert items[0].text == "pending task"


def test_parse_tasks_ignores_non_checkbox_lines():
    items = parse_tasks("just prose\n- not a checkbox\n- [ ] real task\n")
    assert len(items) == 1
    assert items[0].text == "real task"


def test_parse_journal_bullets():
    content = """# Journal

- Chose JWT over sessions
- DB schema designed
not a bullet
"""
    entries = parse_journal(content)
    assert len(entries) == 2
    assert entries[0].text == "Chose JWT over sessions"
    assert entries[1].text == "DB schema designed"


def test_parse_tasks_star_bullets_also_work():
    items = parse_tasks("* [x] star task\n")
    assert len(items) == 1
    assert items[0].status == "done"


def test_read_tasks_file(tmp_settings):
    from src.state_reader import read_tasks

    (tmp_settings.state_dir / "tasks.md").write_text("- [ ] do thing\n- [x] did thing\n")
    sf = read_tasks()
    assert sf.exists
    assert len(sf.tasks) == 2
    assert sf.tasks[1].status == "done"


def test_read_journal_file(tmp_settings):
    from src.state_reader import read_journal

    (tmp_settings.state_dir / "journal.md").write_text("- entry one\n- entry two\n")
    sf = read_journal()
    assert len(sf.entries) == 2


def test_parse_reflections_cycle_blocks():
    content = """# Reflections

## Cycle 4  2026-07-08T13:14:29Z  outcome:passed sha:3f88a0dbfb

Executed 3 backlog task(s); 3 completed.

## Cycle 6  2026-07-08T13:22:00Z  outcome:pending

Reflect phase: missing enemies and towers.
"""
    entries = parse_reflections(content)
    assert len(entries) == 2
    assert entries[0].cycle == 4
    assert entries[0].outcome == "passed"
    assert entries[0].sha == "3f88a0dbfb"
    assert "3 completed" in entries[0].text
    assert entries[1].cycle == 6
    assert entries[1].outcome == "pending"
    assert "Reflect phase" in entries[1].text


def test_read_reflections_file(tmp_settings):
    from src.state_reader import read_reflections

    (tmp_settings.project_path / "reflections.md").write_text(
        "## Cycle 1  2026-07-09T10:00:00Z  outcome:failed\n\nverify gate failed\n"
    )
    sf = read_reflections()
    assert sf.exists
    assert len(sf.reflections) == 1
    assert sf.reflections[0].outcome == "failed"


def test_should_record_execute_reflection_only_on_failure():
    assert should_record_execute_reflection(tasks_attempted=3, tasks_passed=3) is False
    assert should_record_execute_reflection(tasks_attempted=0, tasks_passed=0) is False
    assert should_record_execute_reflection(tasks_attempted=3, tasks_passed=2) is True
    assert should_record_execute_reflection(tasks_attempted=2, tasks_passed=0) is True
