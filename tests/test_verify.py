"""Verify gate tests — orchestrator-owned test runner."""

import pytest

from src.config import settings


@pytest.mark.asyncio
async def test_verify_pass_on_exit_zero(tmp_path, monkeypatch):
    # write a tiny passing test script
    script = tmp_path / "verify_pass.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setattr(settings, "verify_command", str(script))
    monkeypatch.setattr(settings, "project_path", tmp_path)

    from src.orchestrator import verify
    result = await verify.run_verify(timeout=10)
    assert result.ok is True
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_verify_fail_on_exit_nonzero(tmp_path, monkeypatch):
    script = tmp_path / "verify_fail.sh"
    script.write_text("#!/bin/sh\necho 'test failure' >&2\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setattr(settings, "verify_command", str(script))
    monkeypatch.setattr(settings, "project_path", tmp_path)

    from src.orchestrator import verify
    result = await verify.run_verify(timeout=10)
    assert result.ok is False
    assert result.returncode == 1
    assert "test failure" in result.stderr


@pytest.mark.asyncio
async def test_verify_missing_command_fails_clean(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "verify_command", "/nonexistent/pytest")
    monkeypatch.setattr(settings, "project_path", tmp_path)
    from src.orchestrator import verify
    result = await verify.run_verify(timeout=10)
    assert result.ok is False
    assert "not found" in result.stderr.lower()


@pytest.mark.asyncio
async def test_verify_timeout_kills_process(tmp_path, monkeypatch):
    script = tmp_path / "verify_slow.sh"
    script.write_text("#!/bin/sh\nsleep 60\n")
    script.chmod(0o755)
    monkeypatch.setattr(settings, "verify_command", str(script))
    monkeypatch.setattr(settings, "project_path", tmp_path)
    from src.orchestrator import verify
    result = await verify.run_verify(timeout=2)
    assert result.ok is False
    assert "timed out" in result.stderr.lower()
