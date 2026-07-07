"""State reader tests — markdown parsing for tasks/journal."""

from src.state_reader import parse_journal, parse_tasks


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
