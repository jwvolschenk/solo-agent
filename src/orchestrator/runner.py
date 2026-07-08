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
from typing import Any, Callable, Optional

from ..config import settings
from . import budget

log = logging.getLogger("solo.runner")

# Callback the controller can register so tool-call events become activity log entries.
# Signature: (type, message, metadata) -> None. Implemented in controller via db.insert_activity.
ActivityCallback = Callable[[str, str, dict[str, Any]], None]

# Module-level activity hook. None = no activity logging.
_activity_hook: Optional[ActivityCallback] = None


def set_activity_hook(hook: Optional[ActivityCallback]) -> None:
    """Register a callback invoked for each notable agent action (tool calls)."""
    global _activity_hook
    _activity_hook = hook


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
    """Generate a stable session id. Used only if the command template
    references {session}; fresh goals (the Ralph default) don't pass --session."""
    return f"solo-{uuid.uuid4().hex[:12]}"


def build_command(prompt: str, session_id: str, title: str) -> list[str]:
    """Render settings.agent_command into an argv list.

    Substitutions: {repo} {session} {title} {model} {prompt}.
    Only placeholders actually present in the template are substituted, so the
    default template (no {session}) works without error and a custom template
    that includes {session} still gets it. The prompt is passed as one argv
    element (shlex reconstructs the quoting).
    """
    subs = {
        "repo": str(settings.project_path),
        "session": session_id,
        "title": title,
        "model": settings.agent_model,
        "prompt": prompt,
    }
    template = settings.agent_command
    # Only substitute known placeholders so braces in the prompt can't break it.
    for key, val in subs.items():
        template = template.replace("{" + key + "}", val)
    return shlex.split(template)


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
    result.events, result.final_message, result.tokens_used, stopped = _parse_event_stream(result.stdout)
    budget.budget.add(result.tokens_used)

    # Determine success. OpenCode signals clean completion via a step_finish
    # event with part.reason == "stop" (the agent chose to stop). We also accept
    # a captured final message as a weaker signal. An error event OR no stop
    # signal means failure. (Exit code is unreliable — issue #14551.)
    has_error = any(e.get("type") == "error" or e.get("type") == "session.error" for e in result.events)
    has_message = bool(result.final_message)
    result.ok = (stopped or has_message) and not has_error
    if has_error:
        err_events = [e for e in result.events if e.get("type") in ("error", "session.error")]
        # error message may be in part.error.message or part.message or a top-level string
        for e in err_events:
            part = e.get("part") or {}
            msg = (part.get("error") or {}).get("message") or part.get("message") or str(e.get("message", ""))
            if msg:
                result.error = str(msg)
                break
        if not result.error:
            result.error = "session error"
    elif not result.ok:
        result.error = "agent did not signal completion (no 'stop' step_finish, no final message)"
    log.info(
        "agent finished: ok=%s stopped=%s tokens=%d events=%d final=%r",
        result.ok, stopped, result.tokens_used, len(result.events), result.final_message[:80],
    )
    return result


def _parse_event_stream(stdout: str) -> tuple[list[dict], str, int, bool]:
    """Parse newline-delimited JSON events from agent stdout.

    Returns (events, final_message, total_tokens, stopped). Tolerates non-JSON
    lines (mixed logs) by skipping them.

    OpenCode's actual event schema (verified against v1.17.x):
      {"type": "text",        "part": {"type":"text", "text": "<msg>"}}    — assistant message
      {"type": "step_finish", "part": {"reason":"stop"|"tool-calls"|..., "tokens": {"total":N,"input":N,"output":N}}}
      {"type": "tool_use",    "part": {"tool":"<name>", "state":{"status":"completed",...}}}
      {"type": "error",       "part": {"error":{"message":"..."}}} or {"message":"..."}
    Clean completion = a step_finish with part.reason == "stop".
    """
    events: list[dict] = []
    final_message = ""
    total_tokens = 0
    stopped = False

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
        part = event.get("part") or {}

        # token accounting — step_finish carries cumulative-ish per-step tokens
        if etype == "step_finish":
            tokens = part.get("tokens") or {}
            total_tokens += budget.extract_tokens_from_event({"usage": tokens})
            if part.get("reason") == "stop":
                stopped = True

        # capture the latest assistant text message
        if etype == "text":
            text = part.get("text") or event.get("text")
            if isinstance(text, str) and text.strip():
                final_message = text.strip()

        # emit activity events for notable tool calls so the dashboard shows what the agent is doing
        if etype == "tool_use" and _activity_hook is not None:
            tool = part.get("tool", "unknown")
            state = part.get("state") or {}
            # OpenCode nests the actual tool input under state.input, not part.input
            inp = state.get("input") or part.get("input") or {}
            # also grab the title (OpenCode puts a human-readable summary there for bash calls)
            title = state.get("title") or ""
            if state.get("status") == "completed":
                _emit_tool_activity(tool, inp, title)

    # fallback: if no JSON events at all, treat stdout (minus noise) as the message
    if not events and stdout.strip():
        final_message = stdout.strip()[-2000:]
        stopped = True  # assume completion for non-JSON agents (e.g. a stub)

    return events, final_message, total_tokens, stopped


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


def _emit_tool_activity(tool: str, inp: dict, title: str = "") -> None:
    """Translate a completed OpenCode tool call into an activity log entry.

    Only fires for tools that represent real, visible actions (file edits, shell,
    writes). Read-only tools (read, glob, grep, tree) are noisy and skipped.
    """
    if _activity_hook is None:
        return
    # short path of a file, for display
    def _short(p: str) -> str:
        if not isinstance(p, str):
            return ""
        parts = p.split("/")
        return "/".join(parts[-3:]) if len(parts) > 3 else p

    atype = "tool"
    msg = ""
    meta: dict[str, Any] = {"tool": tool}

    if tool in ("edit", "write", "write_file", "create_file"):
        atype = "file"
        fp = inp.get("filePath") or inp.get("file_path") or inp.get("path") or ""
        verb = "edited" if tool == "edit" else "wrote"
        msg = f"{verb} {_short(fp)}" if fp else f"{tool} (unknown file)"
        meta["file"] = fp
    elif tool in ("bash", "shell", "execute"):
        atype = "tool"
        cmd = inp.get("command") or inp.get("cmd") or title or ""
        msg = f"$ {str(cmd).strip()[:100]}" if cmd else "shell command"
        meta["command"] = cmd
    elif tool in ("remove", "delete", "rm"):
        atype = "file"
        fp = inp.get("path") or inp.get("filePath") or ""
        msg = f"deleted {_short(fp)}" if fp else "deleted file"
        meta["file"] = fp
    else:
        # skip noisy read-only tools (read, glob, grep, tree, search, etc.)
        return

    if msg:
        try:
            _activity_hook(atype, msg, meta)
        except Exception as e:
            log.debug("activity hook failed: %s", e)
