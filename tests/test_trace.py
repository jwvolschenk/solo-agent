"""Tests for orchestrator trace logging."""

from src.orchestrator import trace


def test_trace_kv_includes_context(monkeypatch, caplog):
  import logging

  caplog.set_level(logging.INFO, logger="solo.orch")
  trace.bind(cycle=3, phase="execute", project_id="proj-1", task="fix the thing")
  trace.info("test_event", ok=True, error=None)

  assert len(caplog.records) == 1
  msg = caplog.records[0].message
  assert "event=test_event" in msg
  assert "cycle=3" in msg
  assert "phase=execute" in msg
  assert "project=proj-1" in msg
  assert "task=" in msg
  assert "ok=true" in msg


def test_phase_transition_updates_context(caplog):
  import logging

  caplog.set_level(logging.INFO, logger="solo.orch")
  trace.bind(cycle=1, phase="idle")
  trace.phase_transition("idle", "execute", reason="backlog_task")

  msg = caplog.records[-1].message
  assert "event=phase_transition" in msg
  assert "from_phase=idle" in msg
  assert "to_phase=execute" in msg
  assert "reason=backlog_task" in msg
  assert "phase=execute" in msg


def test_output_snippet_emits_debug(caplog):
  import logging

  caplog.set_level(logging.DEBUG, logger="solo.orch")
  trace.output_snippet("agent_stderr", "line one\nline two")

  msg = caplog.records[-1].message
  assert "event=agent_stderr" in msg
  assert "line one" in msg
