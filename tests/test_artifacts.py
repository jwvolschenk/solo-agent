"""Artifact bootstrap tests — CODEDB.md and opencode.json instructions."""

import json

import pytest

from src.orchestrator import artifacts


@pytest.fixture
def codedb_cmd(monkeypatch):
    """Deterministic codedb MCP argv for opencode.json seeding tests."""
    monkeypatch.setattr(artifacts.settings, "codedb_mcp_command", "/usr/bin/codedb mcp")


def test_ensure_codedb_guide_creates_stub(tmp_settings):
    artifacts.ensure_codedb_guide()
    path = artifacts.codedb_path()
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "codedb MCP" in content
    assert "Entry points" in content


def test_ensure_codedb_guide_does_not_overwrite_existing(tmp_settings):
    path = artifacts.codedb_path()
    path.write_text("# Custom guide\n\nMy notes.\n", encoding="utf-8")
    artifacts.ensure_codedb_guide()
    assert path.read_text(encoding="utf-8") == "# Custom guide\n\nMy notes.\n"


def test_ensure_opencode_instructions_creates_config(tmp_settings, codedb_cmd):
    artifacts.ensure_opencode_instructions()
    config_path = artifacts.opencode_config_path()
    assert config_path.exists()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["instructions"] == ["CODEDB.md"]
    assert data["mcp"]["codedb"] == {
        "type": "local",
        "command": ["/usr/bin/codedb", "mcp"],
    }


def test_ensure_opencode_instructions_merges_existing(tmp_settings, codedb_cmd):
    config_path = artifacts.opencode_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "mcp": {"codedb": {"type": "local", "command": ["codedb", "mcp"]}},
                "instructions": ["CONTRIBUTING.md"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    artifacts.ensure_opencode_instructions()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["instructions"] == ["CODEDB.md", "CONTRIBUTING.md"]
    assert data["mcp"]["codedb"]["command"] == ["codedb", "mcp"]


def test_ensure_opencode_instructions_seeds_mcp_when_only_instructions_present(
    tmp_settings, codedb_cmd,
):
    config_path = artifacts.opencode_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "instructions": ["CODEDB.md"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    artifacts.ensure_opencode_instructions()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["mcp"]["codedb"]["command"] == ["/usr/bin/codedb", "mcp"]


def test_ensure_opencode_instructions_skips_mcp_without_binary(tmp_settings, monkeypatch):
    monkeypatch.setattr(artifacts.settings, "codedb_mcp_command", "")
    monkeypatch.setattr(artifacts.shutil, "which", lambda _name: None)
    artifacts.ensure_opencode_instructions()
    data = json.loads(artifacts.opencode_config_path().read_text(encoding="utf-8"))
    assert data["instructions"] == ["CODEDB.md"]
    assert "mcp" not in data


def test_ensure_artifacts_wires_codedb(tmp_settings, codedb_cmd):
    artifacts.ensure_artifacts()
    assert artifacts.codedb_path().exists()
    data = json.loads(artifacts.opencode_config_path().read_text(encoding="utf-8"))
    assert "CODEDB.md" in data["instructions"]
    assert data["mcp"]["codedb"]["type"] == "local"
    solo = artifacts.solo_agent_path().read_text(encoding="utf-8")
    assert "CODEDB.md" in solo
