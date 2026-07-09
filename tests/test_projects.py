"""Project CRUD, switching, directives-in-project-path, and soft-stop tests."""

import pytest

from src.config import settings


def test_create_and_list_project(client, tmp_path):
    target = tmp_path / "myproj"
    target.mkdir()
    r = client.post("/api/projects", json={
        "name": "Test Project",
        "goal": "Build a thing",
        "project_path": str(target),
    })
    assert r.status_code == 201
    pid = r.json()["project"]["id"]
    assert pid == "test-project"

    r2 = client.get("/api/projects")
    assert r2.status_code == 200
    data = r2.json()
    assert data["count"] >= 1
    assert any(p["id"] == "test-project" for p in data["projects"])


def test_activate_project(client, tmp_path):
    target = tmp_path / "proj1"
    target.mkdir()
    client.post("/api/projects", json={
        "name": "Proj One",
        "goal": "goal one",
        "project_path": str(target),
    })
    r = client.post("/api/projects/proj-one/activate")
    assert r.status_code == 200
    assert r.json()["status"] == "switched"
    assert r.json()["active_project_id"] == "proj-one"

    # settings should now reflect the project
    from src.config import settings as s
    assert str(s.project_path) == str(target)
    assert s.goal == "goal one"


def test_directives_written_to_project_path(client, tmp_settings):
    """Directives must land in project_path, not state_dir — the bug fix."""
    from src.directives import directives_path
    # post a directive
    r = client.post("/api/agent/directives", json={"priority": "high", "text": "do X"})
    assert r.status_code == 201
    # the file should be in project_path, not state_dir
    dp = directives_path()
    assert "project" in str(dp)  # tmp_settings sets project_path to .../project
    assert dp.exists()
    content = dp.read_text()
    assert "do X" in content


def test_get_project_not_found(client):
    r = client.get("/api/projects/nonexistent")
    assert r.status_code == 404


def test_delete_project(client, tmp_path):
    target = tmp_path / "todelete"
    target.mkdir()
    client.post("/api/projects", json={"name": "ToDelete", "goal": "", "project_path": str(target)})
    r = client.delete("/api/projects/todelete")
    assert r.status_code == 200
    # confirm gone
    assert client.get("/api/projects/todelete").status_code == 404


def test_edit_project_goal(client, tmp_path):
    target = tmp_path / "editable"
    target.mkdir()
    client.post("/api/projects", json={"name": "Editable", "goal": "old goal", "project_path": str(target)})
    r = client.put("/api/projects/editable", json={"goal": "new goal"})
    assert r.status_code == 200
    assert r.json()["project"]["goal"] == "new goal"


def test_soft_stop_flag_sets_state(client, tmp_settings):
    """The stop-after-cycle endpoint should set the flag on the controller state."""
    r = client.post("/api/orchestrator/stop-after-cycle")
    assert r.status_code == 200
    assert r.json()["stop_after_cycle"] is True

    # verify it's reflected in the state endpoint
    state = client.get("/api/orchestrator/state").json()
    assert state["stop_after_cycle"] is True

    # cancel it
    r2 = client.post("/api/orchestrator/cancel-stop-after-cycle")
    assert r2.json()["stop_after_cycle"] is False


def test_archive_backlog_moves_done_items(tmp_settings):
    """archive_backlog should move [x] items to a history file and leave unchecked ones."""
    from src.orchestrator import artifacts

    # write a backlog with done + pending items
    backlog = artifacts.backlog_path()
    backlog.parent.mkdir(parents=True, exist_ok=True)
    backlog.write_text(
        "# Backlog\n\n"
        "- [ ] pending task one\n"
        "- [x] done task one\n"
        "- [ ] pending task two\n"
        "- [x] done task two\n",
        encoding="utf-8",
    )

    archived = artifacts.archive_backlog()
    assert archived == 2

    # backlog should now have only the pending items
    remaining = backlog.read_text()
    assert "- [ ] pending task one" in remaining
    assert "- [ ] pending task two" in remaining
    assert "[x]" not in remaining

    # history file should exist with the done items
    histories = list(artifacts.history_dir().glob("backlog-*.md"))
    assert len(histories) == 1
    hcontent = histories[0].read_text()
    assert "done task one" in hcontent
    assert "done task two" in hcontent


def test_archive_backlog_nothing_done_returns_zero(tmp_settings):
    from src.orchestrator import artifacts

    backlog = artifacts.backlog_path()
    backlog.parent.mkdir(parents=True, exist_ok=True)
    backlog.write_text("# Backlog\n\n- [ ] only pending\n", encoding="utf-8")
    assert artifacts.archive_backlog() == 0


def test_relocate_stale_seeds_moves_planner_lines(tmp_settings):
    from src.orchestrator import artifacts
    from src.state_reader import parse_tasks

    backlog = artifacts.backlog_path()
    candidates = artifacts.candidates_path()
    backlog.parent.mkdir(parents=True, exist_ok=True)
    backlog.write_text(
        "# Backlog\n\n"
        "- [ ] (orchestrator seed, cycle 2) theme line\n"
        "- [ ] ship feature A\n",
        encoding="utf-8",
    )
    candidates.write_text("# Backlog Candidates\n\n", encoding="utf-8")

    moved = artifacts.relocate_stale_seeds()
    assert moved == 1
    assert "ship feature A" in backlog.read_text()
    assert "theme line" not in backlog.read_text()
    seeds = [t.text for t in parse_tasks(artifacts.read_candidates()) if t.status == "todo"]
    assert len(seeds) == 1
    assert "orchestrator seed" in seeds[0]


def test_compact_reflections_archives_old_entries(tmp_settings, monkeypatch):
    from src.orchestrator import artifacts

    monkeypatch.setattr(artifacts.settings, "reflections_max_entries", 2)
    reflections = artifacts.reflections_path()
    reflections.parent.mkdir(parents=True, exist_ok=True)
    reflections.write_text(
        artifacts._REFLECTIONS_PREAMBLE
        + "## Cycle 1  2026-07-09T10:00:00Z  outcome:failed\n\nfirst failure\n"
        + "\n## Cycle 2  2026-07-09T11:00:00Z  outcome:pending\n\nreflect insight\n"
        + "\n## Cycle 3  2026-07-09T12:00:00Z  outcome:passed\n\nthird entry\n",
        encoding="utf-8",
    )

    archived = artifacts.compact_reflections()
    assert archived == 1

    remaining = reflections.read_text()
    assert "first failure" not in remaining
    assert "reflect insight" in remaining
    assert "third entry" in remaining
    assert remaining.count("## Cycle ") == 2

    archives = list(artifacts.reflections_archive_dir().glob("reflections-*.md"))
    assert len(archives) == 1
    assert "first failure" in archives[0].read_text()


def test_append_reflection_trims_when_over_max(tmp_settings, monkeypatch):
    from src.orchestrator import artifacts

    monkeypatch.setattr(artifacts.settings, "reflections_max_entries", 2)
    reflections = artifacts.reflections_path()
    reflections.parent.mkdir(parents=True, exist_ok=True)
    reflections.write_text(artifacts._REFLECTIONS_PREAMBLE, encoding="utf-8")

    artifacts.append_reflection("failure alpha", cycle=1, outcome="failed")
    artifacts.append_reflection("reflect beta", cycle=2, outcome="pending")
    artifacts.append_reflection("failure gamma", cycle=3, outcome="passed")

    content = reflections.read_text()
    assert "failure alpha" not in content
    assert "reflect beta" in content
    assert "failure gamma" in content
    assert content.count("## Cycle ") == 2
