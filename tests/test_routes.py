"""Route tests — all endpoints via TestClient with mocked llama-server."""

import pytest

from src.models import HealthState, MetricsSnapshot, SlotInfo


@pytest.fixture
def mocked_collector():
    """Patch the collector's in-memory state with known values."""
    from src import collector as collector_mod
    collector_mod.collector.health = HealthState(status="ok", message="ok", http_status=200)
    collector_mod.collector.metrics = MetricsSnapshot(
        prompt_tokens_seconds=2450.0,
        predicted_tokens_seconds=94.0,
        prompt_tokens_total=1000,
        tokens_predicted_total=500,
    )
    collector_mod.collector.slots = [SlotInfo(id=0, n_ctx=262144, is_processing=False)]
    yield collector_mod.collector


def test_health_endpoint(client, mocked_collector):
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["http_status"] == 200


def test_metrics_endpoint(client, mocked_collector):
    r = client.get("/api/metrics")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["metrics"]["prompt_tokens_seconds"] == 2450.0
    assert data["metrics"]["predicted_tokens_seconds"] == 94.0


def test_metrics_history_endpoint(client, mocked_collector):
    # insert one metrics point so history isn't empty
    from src.db import insert_metrics
    from src.models import MetricsSnapshot
    insert_metrics(MetricsSnapshot(prompt_tokens_seconds=10.0, predicted_tokens_seconds=5.0))

    r = client.get("/api/metrics/history?range=1h")
    assert r.status_code == 200
    data = r.json()
    assert data["range"] == "1h"
    assert data["count"] >= 1


def test_slots_endpoint(client, mocked_collector):
    r = client.get("/api/slots")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert len(data["slots"]) == 1
    assert data["slots"][0]["n_ctx"] == 262144


def test_activity_post_and_get(client, tmp_settings):
    r = client.post("/api/agent/activity", json={"type": "file", "message": "wrote auth.py"})
    assert r.status_code == 201
    assert r.json()["id"] >= 1

    r2 = client.get("/api/agent/activity")
    assert r2.status_code == 200
    events = r2.json()["events"]
    assert any(e["message"] == "wrote auth.py" for e in events)


def test_activity_rejects_empty_message(client):
    r = client.post("/api/agent/activity", json={"message": "   "})
    assert r.status_code == 400


def test_directive_full_lifecycle(client, tmp_settings):
    # create
    r = client.post("/api/agent/directives", json={"priority": "high", "text": "do the thing"})
    assert r.status_code == 201
    did = r.json()["directive"]["id"]
    assert did == "d1"

    # list
    r2 = client.get("/api/agent/directives")
    assert r2.json()["count"] == 1

    # patch status
    r3 = client.patch(f"/api/agent/directives/{did}", json={"status": "acknowledged"})
    assert r3.status_code == 200
    assert r3.json()["directive"]["status"] == "acknowledged"

    # 404 for unknown
    r4 = client.patch("/api/agent/directives/d99", json={"status": "done"})
    assert r4.status_code == 404


def test_state_tasks_endpoint(client, tmp_settings):
    (tmp_settings.state_dir / "tasks.md").write_text("- [ ] task one\n- [x] task two\n")
    r = client.get("/api/state/tasks")
    assert r.status_code == 200
    data = r.json()
    assert data["exists"] is True
    assert len(data["tasks"]) == 2


def test_orchestrator_state_endpoint(client, tmp_settings):
    r = client.get("/api/orchestrator/state")
    assert r.status_code == 200
    data = r.json()
    assert "phase" in data
    assert "cycle_number" in data


def test_dashboard_html_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "SOLO AGENT MONITOR" in r.text


def test_metrics_history_validates_range(client):
    r = client.get("/api/metrics/history?range=2h")
    assert r.status_code == 422  # only 1h/6h/24h allowed


def test_config_get_and_set(client, tmp_path):
    # GET
    r = client.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert "project_path" in data
    assert "goal" in data
    assert "verify_command" in data

    # make a real dir to point at
    target = tmp_path / "myproject"
    target.mkdir()

    # PUT project_path
    r2 = client.put("/api/config", json={"project_path": str(target)})
    assert r2.status_code == 200
    assert r2.json()["project_path"] == str(target)

    # GET reflects the change
    r3 = client.get("/api/config")
    assert r3.json()["project_path"] == str(target)


def test_config_set_goal(client):
    r = client.put("/api/config", json={"goal": "Build a tower defense game"})
    assert r.status_code == 200
    assert r.json()["goal"] == "Build a tower defense game"
    # GET reflects it
    assert client.get("/api/config").json()["goal"] == "Build a tower defense game"


def test_config_set_verify_command(client):
    # empty string disables the gate
    r = client.put("/api/config", json={"verify_command": ""})
    assert r.status_code == 200
    assert r.json()["verify_command"] == ""
    # set it to something
    r2 = client.put("/api/config", json={"verify_command": "pytest -q"})
    assert r2.json()["verify_command"] == "pytest -q"


def test_config_rejects_nonexistent_path(client):
    r = client.put("/api/config", json={"project_path": "/nonexistent/xyz"})
    assert r.status_code == 400
