"""Shared pytest fixtures.

Provides:
  - isolated tmp STATE_DIR + DB per test (via monkeypatching settings)
  - sample llama-server response fixtures (current + classic schema)
  - a mock agent stub script (stands in for `opencode run` in runner tests)
  - a FastAPI TestClient
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

# Make src importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ----------------------------------------------------------------------------
# llama-server response fixtures (both schemas)
# ----------------------------------------------------------------------------

PROMETHEUS_CURRENT = """
# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 1234
# HELP llamacpp:prompt_seconds_total Prompt process time
# TYPE llamacpp:prompt_seconds_total counter
llamacpp:prompt_seconds_total 5.6
# HELP llamacpp:tokens_predicted_total Number of generation tokens processed.
# TYPE llamacpp:tokens_predicted_total counter
llamacpp:tokens_predicted_total 789
# HELP llamacpp:tokens_predicted_seconds_total Predict process time
# TYPE llamacpp:tokens_predicted_seconds_total counter
llamacpp:tokens_predicted_seconds_total 8.4
# HELP llamacpp:n_decode_total Total number of llama_decode() calls
# TYPE llamacpp:n_decode_total counter
llamacpp:n_decode_total 100
# HELP llamacpp:n_tokens_max Largest observed n_tokens.
# TYPE llamacpp:n_tokens_max counter
llamacpp:n_tokens_max 5000
# HELP llamacpp:prompt_tokens_seconds Average prompt throughput in tokens/s.
# TYPE llamacpp:prompt_tokens_seconds gauge
llamacpp:prompt_tokens_seconds 2450.0
# HELP llamacpp:predicted_tokens_seconds Average generation throughput in tokens/s.
# TYPE llamacpp:predicted_tokens_seconds gauge
llamacpp:predicted_tokens_seconds 94.0
# HELP llamacpp:requests_processing Number of requests processing.
# TYPE llamacpp:requests_processing gauge
llamacpp:requests_processing 1
# HELP llamacpp:requests_deferred Number of requests deferred.
# TYPE llamacpp:requests_deferred gauge
llamacpp:requests_deferred 0
# HELP llamacpp:n_busy_slots_per_decode Average number of busy slots per llama_decode() call
# TYPE llamacpp:n_busy_slots_per_decode gauge
llamacpp:n_busy_slots_per_decode 1.0
"""

# Classic schema: numeric 'state', flat params, slots_idle in health, model in slot
SLOTS_CLASSIC = [
    {"id": 0, "state": 0, "n_ctx": 4096, "model": "qwen-test",
     "next_token": {"n_decoded": 42, "n_remain": -1}},
    {"id": 1, "state": 1, "n_ctx": 4096, "model": "qwen-test",
     "next_token": {"n_decoded": 100, "n_remain": 50}},
]

SLOTS_CURRENT = [
    {"id": 0, "n_ctx": 262144, "speculative": False, "is_processing": False},
    {"id": 1, "n_ctx": 262144, "speculative": False, "is_processing": True,
     "n_prompt_tokens": 500, "generated": "hello world",
     "next_token": {"n_decoded": 25, "has_next_token": True}},
]

HEALTH_CURRENT_OK = {"status": "ok"}
HEALTH_CLASSIC_OK = {"status": "ok", "slots_idle": 1, "slots_processing": 1}
HEALTH_ERROR_BODY = {"error": {"message": "Loading model", "type": "unavailable_error", "code": 503}}

PROPS_CURRENT = {
    "default_generation_settings": {"params": {"temperature": 0.8, "top_k": 20, "n_predict": -1}, "n_ctx": 262144},
    "total_slots": 1,
    "model_alias": "qwen-test",
    "chat_template": "{{prompt}}",
    "bos_token": "<s>",
    "eos_token": "</s>",
}

PROPS_CLASSIC = {
    "default_generation_settings": {"temperature": 0.7, "top_k": 40, "n_predict": 256, "n_ctx": 4096},
    "total_slots": 2,
    "model_alias": "qwen-classic",
}


@pytest.fixture
def prometheus_text():
    return PROMETHEUS_CURRENT


@pytest.fixture
def slots_current():
    return SLOTS_CURRENT


@pytest.fixture
def slots_classic():
    return SLOTS_CLASSIC


@pytest.fixture
def props_current():
    return PROPS_CURRENT


@pytest.fixture
def props_classic():
    return PROPS_CLASSIC


# ----------------------------------------------------------------------------
# Isolated settings (tmp STATE_DIR + DB) per test
# ----------------------------------------------------------------------------


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    """Point settings at a tmp state_dir + db_path so tests don't touch real data."""
    from src import config, db

    state_dir = tmp_path / "workspace"
    state_dir.mkdir()
    db_path = tmp_path / "data" / "test.db"
    db_path.parent.mkdir()

    monkeypatch.setattr(config.settings, "state_dir", state_dir)
    monkeypatch.setattr(config.settings, "db_path", db_path)
    # also patch the directives module's path lookup which captured settings at import
    from src import directives as dirmod
    monkeypatch.setattr(dirmod, "settings", config.settings)

    # init the schema against the fresh path
    db.init_db(db_path)
    yield config.settings


@pytest.fixture
def tmp_target_repo(tmp_path):
    """A throwaway git repo to act as the orchestrator's TARGET_REPO."""
    import subprocess

    repo = tmp_path / "target"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@test"], check=True)
    (repo / "README.md").write_text("# target\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True, env=env)
    return repo


# ----------------------------------------------------------------------------
# Mock agent stub (stands in for `opencode run` in runner tests)
# ----------------------------------------------------------------------------


@pytest.fixture
def mock_agent_script(tmp_path):
    """Write a fake 'agent' script that emits OpenCode-style JSON events + exits.

    Mirrors the real OpenCode event schema (verified v1.17.x):
      {"type": "text",        "part": {"type":"text", "text": "..."}}
      {"type": "step_finish", "part": {"reason":"stop", "tokens": {"total":N,...}}}
      {"type": "error",       "part": {"error":{"message":"..."}}}

    The script reads AGENT_MODE to decide behavior:
      ok       -> emits a text message + a stop step_finish, then exits 0
      error    -> emits an error event then exits 0 (opencode bug: rc 0 on error)
      hang     -> sleeps forever (to test timeout/kill)
    """
    script = tmp_path / "mock_agent.py"
    script.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import os, sys, time, json
        mode = os.environ.get("AGENT_MODE", "ok")
        if mode == "hang":
            time.sleep(10000)
            sys.exit(0)
        if mode == "error":
            print(json.dumps({"type": "error", "part": {"error": {"message": "boom"}}}))
            sys.exit(0)
        # ok: emit assistant text + a clean stop step_finish with token counts
        print(json.dumps({"type": "text", "part": {"type": "text", "text": "DONE: added a test"}}))
        print(json.dumps({"type": "step_finish", "part": {"reason": "stop", "tokens": {"total": 150, "input": 100, "output": 50}}}))
        sys.exit(0)
        """))
    script.chmod(0o755)
    return script


# ----------------------------------------------------------------------------
# FastAPI TestClient
# ----------------------------------------------------------------------------


@pytest.fixture
def client(tmp_settings):
    """FastAPI TestClient with isolated settings. Background tasks are NOT started
    (we avoid the lifespan to keep tests deterministic)."""
    from fastapi.testclient import TestClient
    from src.main import app

    # we don't want the real collector/watcher running during tests
    with TestClient(app) as c:
        yield c
