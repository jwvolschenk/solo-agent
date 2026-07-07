"""Agent runner — spawns OpenCode (or any configured agent) headlessly.

Key design points (all from the OpenCode research):
  - ``--format json`` streams events on stdout; we parse for completion.
  - Exit code is UNRELIABLE (issue #14551, returns 0 on session errors) — we
    determine success from the event stream, not $?
  - Runs can hang indefinitely (issue #4255) — every call is wrapped in a hard
    asyncio.wait_for timeout + process-group kill.
  - Token usage is parsed from the event stream and fed to the budget governor.

The command is configurable (settings.agent_command) so a mock stub can stand
in for ``opencode run`` during tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
import uuid
from dataclasses import dataclass, field
from typing import Optional

from ..config import settings
from . import budget

log = logging.getLogger("solo.runner")


@dataclass
class AgentResult:
    """Outcome of one agent goal invocation."""

    ok: bool
    session_id: str
    stdout: str = ""
    stderr: str = ""
    final_message: str = ""
    tokens_used: int = 0
    timed_out: bool = False
    error: Optional[str] = None
    events: list[dict] = field(default_factory=list)


def _new_session_id() -> str:
    """Generate a stable session id for OpenCode's --session flag."""
    return f"solo-{uuid.uuid4().hex[:12]}"


def build_command(prompt: str, session_id: str, title: str) -> list[str]:
    """Render settings.agent_command into an argv list.

    Substitutions: {repo} {session} {title} {model} {prompt}.
    The prompt is substituted as a single quoted argv element.
    """
    rendered = settings.agent_command.format(
        repo=str(settings.target_repo),
        session=session_id,
        title=title,
        model=settings.agent_model,
        prompt=prompt,
    )
    return shlex.split(rendered)


async def run_goal(
    prompt: str,
    *,
    timeout: Optional[float] = None,
    title: str = "solo-agent goal",
) -> AgentResult:
    """Run one agent goal to completion (or timeout). Returns an AgentResult.

    Spawns the agent as a subprocess in its own process group so we can kill
    the whole tree on timeout (opencode may spawn children).
    """
    timeout = timeout or settings.per_goal_timeout_sec
    session_id = _new_session_id()
    argv = build_command(prompt, session_id, title)
    log.info("spawning agent: %s", _redact(argv))

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,  # new process group for clean kill
        )
    except FileNotFoundError as e:
        log.error("agent binary not found: %s", e)
        return AgentResult(ok=False, session_id=session_id, error=f"agent binary not found: {e}")

    result = AgentResult(ok=False, session_id=session_id)
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        log.error("agent timed out after %.0fs — killing process group", timeout)
        await _kill_process_group(proc)
        result.timed_out = True
        result.error = f"timed out after {timeout}s"
        # drain whatever output we can get (already in proc's pipes via communicate? no)
        return result

    result.stdout = stdout_b.decode("utf-8", errors="replace")
    result.stderr = stderr_b.decode("utf-8", errors="replace")

    # parse the JSON event stream from stdout
    result.events, result.final_message, result.tokens_used = _parse_event_stream(result.stdout)
    budget.budget.add(result.tokens_used)

    # determine success: a final message + no error event + no timeout
    has_error = any(e.get("type") == "session.error" for e in result.events)
    has_message = bool(result.final_message)
    result.ok = has_message and not has_error
    if has_error:
        result.error = next(
            (str(e.get("error", {}).get("message", "session error"))
             for e in result.events if e.get("type") == "session.error"),
            "session error",
        )
    log.info(
        "agent finished: ok=%s tokens=%d events=%d final=%r",
        result.ok, result.tokens_used, len(result.events), result.final_message[:80],
    )
    return result


def _parse_event_stream(stdout: str) -> tuple[list[dict], str, int]:
    """Parse newline-delimited JSON events from agent stdout.

    Returns (events, final_message, total_tokens). Tolerates non-JSON lines
    (mixed logs) by skipping them. If the agent didn't emit JSON, we fall back
    to treating the whole stdout as a message.
    """
    events: list[dict] = []
    final_message = ""
    total_tokens = 0

    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(event)

        etype = event.get("type", "")
        # token accounting
        total_tokens += budget.extract_tokens_from_event(event)

        # capture the final assistant message
        if etype == "session.message" or etype == "message":
            content = event.get("content") or event.get("text")
            if isinstance(content, list):
                # OpenCode often nests content as [{type, text}]
                content = " ".join(
                    str(c.get("text", "")) for c in content if isinstance(c, dict)
                )
            if isinstance(content, str) and content.strip():
                final_message = content.strip()
        elif etype == "session.end" and not final_message:
            # use the end summary if no message was captured
            final_message = str(event.get("summary", ""))

    # fallback: if no JSON events at all, treat stdout (minus noise) as the message
    if not events and stdout.strip():
        final_message = stdout.strip()[-2000:]

    return events, final_message, total_tokens


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the agent's whole process group (it may have spawned children)."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            os.killpg(pgid, signal.SIGKILL)
            await proc.wait()
    except ProcessLookupError:
        pass
    except Exception as e:
        log.warning("failed to kill process group: %s", e)
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _redact(argv: list[str]) -> str:
    """Render an argv list for logging, truncating very long prompt args."""
    out = []
    for a in argv:
        if len(a) > 120:
            out.append(a[:117] + "...")
        else:
            out.append(a)
    return " ".join(out)
