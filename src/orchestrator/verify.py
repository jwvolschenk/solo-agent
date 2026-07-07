"""Verification gate — orchestrator-owned, never agent-attested.

Runs VERIFY_COMMAND in the target repo and reports structured pass/fail.
This is the single most important guardrail for 24/7 operation: the agent
cannot mark itself done; only this gate can.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass

from ..config import settings

log = logging.getLogger("solo.verify")


@dataclass
class VerifyResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False

    def summary(self) -> str:
        tag = "PASS" if self.ok else "FAIL"
        return f"{tag} (rc={self.returncode})"


async def run_verify(timeout: float = 600.0) -> VerifyResult:
    """Run the verification command in settings.target_repo.

    The command is split with shlex so ``pytest -q`` etc. work. Output is
    captured and truncated to a sane size for storage.
    """
    cmd = shlex.split(settings.verify_command)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(settings.target_repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        log.error("verify command not found: %s", e)
        return VerifyResult(ok=False, returncode=-1, stdout="", stderr=f"command not found: {e}")

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        log.error("verify timed out after %.0fs", timeout)
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return VerifyResult(ok=False, returncode=-1, stdout="", stderr=f"verify timed out after {timeout}s")

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    truncated = False
    MAX = 20_000
    if len(stdout) > MAX:
        stdout = stdout[-MAX:]
        truncated = True
    rc = proc.returncode if proc.returncode is not None else -1
    ok = rc == 0
    log.info("verify %s", "PASS" if ok else "FAIL")
    return VerifyResult(ok=ok, returncode=rc, stdout=stdout, stderr=stderr, truncated=truncated)
