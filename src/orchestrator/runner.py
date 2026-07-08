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
from typing import Any, Callable, Literal, Optional

from ..config import settings
from ..models import TranscriptEvent
from .. import transcript
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


def new_session_id() -> str:
    """Generate a stable session id. Used to correlate {session} template
    substitution AND to group this goal's transcript events (see transcript.py)
    under one session card in the dashboard."""
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
    session_id: Optional[str] = None,
) -> AgentResult:
    """Run one agent goal to completion (or timeout). Returns an AgentResult.

    Spawns the agent as a subprocess in its own process group so we can kill
    the whole tree on timeout (opencode may spawn children).

    Reads stdout LINE BY LINE as it arrives — not buffered until exit — so
    activity events flow to the dashboard in real time while the agent works.

    session_id, if provided, tags every live transcript event this goal
    produces (see transcript.py) so the dashboard can group them under one
    session card. If omitted, a fresh one is generated (existing callers/tests
    are unaffected).
    """
    timeout = timeout or settings.per_goal_timeout_sec
    session_id = session_id or new_session_id()
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
    stdout_lines: list[str] = []
    stopped = False

    async def _read_stdout() -> None:
        """Read stdout line-by-line, processing each JSON event as it arrives."""
        nonlocal stopped
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break  # EOF — process closed stdout
            decoded = line.decode("utf-8", errors="replace").rstrip()
            stdout_lines.append(decoded)
            # process the event immediately for live activity
            ev_stopped = await _process_live_event(decoded, session_id)
            if ev_stopped:
                stopped = True

    async def _read_stderr() -> None:
        """Drain stderr (keep the pipe from blocking)."""
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break

    try:
        # run both readers concurrently with a hard timeout
        await asyncio.wait_for(
            asyncio.gather(_read_stdout(), _read_stderr()),
            timeout=timeout,
        )
        await proc.wait()
    except asyncio.TimeoutError:
        log.error("agent timed out after %.0fs — killing process group", timeout)
        await _kill_process_group(proc)
        result.timed_out = True
        result.error = f"timed out after {timeout}s"
        result.stdout = "\n".join(stdout_lines)
        return result

    result.stdout = "\n".join(stdout_lines)
    result.stderr = ""

    # parse the full event stream from collected stdout for the final result
    result.events, result.final_message, result.tokens_used, parse_stopped = _parse_event_stream(result.stdout)
    if parse_stopped:
        stopped = True
    # NOTE: tokens are already counted live, per step_finish event, inside
    # _process_live_event -- do NOT budget.budget.add(result.tokens_used) here.
    # That used to double-count every goal's usage (see test_runner_does_not_double_count_tokens).

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


async def _process_live_event(line: str, session_id: str) -> bool:
    """Process a single NDJSON line as it arrives from the agent's stdout.

    Drives BOTH activity tracks in real time as data arrives (not after the
    process exits):
      - Track A (thin, durable): _emit_tool_activity -> db.insert_activity via
        the activity hook, unchanged curated behavior (skips read-only tools,
        truncates).
      - Track B (rich, live-only): every tool call (any tool, any status) and
        every full-text reasoning message -> transcript.record().

    Returns True if this event signals completion (step_finish, reason=stop).

    This is the ONLY place activity/transcript events are emitted from the
    live stream. _parse_event_stream (run on the full buffer after the process
    exits) only computes the final AgentResult summary and must not re-emit —
    it used to, which double-logged every tool call/message and double-counted
    tokens (see _parse_event_stream's docstring).
    """
    line = line.strip()
    if not line or not line.startswith("{"):
        return False
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False

    etype = event.get("type", "")
    part = event.get("part") or {}

    if etype == "tool_use":
        await _handle_tool_use_event(part, session_id)

    # emit activity for assistant text messages (the agent's reasoning/output)
    if etype == "text":
        text = part.get("text") or ""
        if isinstance(text, str) and text.strip():
            if _activity_hook is not None:
                _activity_hook("system", text.strip()[:200], {})
            await transcript.record(
                TranscriptEvent(
                    id=str(uuid.uuid4()),
                    kind="text",
                    status="completed",
                    text=_cap(text.strip()),
                    session_id=session_id,
                )
            )

    # count tokens as they come in
    if etype == "step_finish":
        tokens = part.get("tokens") or {}
        budget.budget.add(budget.extract_tokens_from_event({"usage": tokens}))
        if part.get("reason") == "stop":
            return True

    return False


_READONLY_TOOLS = {"read", "grep", "glob", "list", "tree", "search"}


async def _handle_tool_use_event(part: dict, session_id: str) -> None:
    """Fires on every tool_use event, any tool, any status.

    Track A (thin) only fires for completed, allowlisted tools — unchanged
    _emit_tool_activity behavior. Track B (rich) records every tool call: a
    "running" entry when a correlation id is available and the tool isn't
    already completed, upgraded in place to "completed"/"error" once it is.
    If no correlation id can be found, the running phase is skipped entirely
    and only the completed entry is recorded — same trigger point as before,
    just with fuller detail.
    """
    tool = part.get("tool", "unknown")
    state = part.get("state") or {}
    status_raw = state.get("status") or ""
    inp = state.get("input") or part.get("input") or {}
    title = state.get("title") or ""

    if status_raw == "completed" and _activity_hook is not None:
        _emit_tool_activity(tool, inp, title)

    if status_raw == "completed":
        status: Literal["running", "completed", "error"] = (
            "error" if state.get("error") else "completed"
        )
    elif status_raw:
        status = "running"
    else:
        return  # no status at all -- nothing meaningful to record yet

    call_id = _extract_call_id(part, state)
    if status == "running" and call_id is None:
        return  # can't correlate a later completion back to this entry

    output = None if status == "running" else _extract_tool_output(state, part)
    await transcript.record(
        TranscriptEvent(
            id=call_id or str(uuid.uuid4()),
            kind="tool",
            status=status,
            tool=tool,
            readonly=tool in _READONLY_TOOLS,
            input=_cap(_stringify(inp)),
            output=_cap(output) if output else None,
            title=title or None,
            session_id=session_id,
        ),
        op="append" if status == "running" else "update",
    )


def _extract_call_id(part: dict, state: dict) -> Optional[str]:
    """Best-effort correlation id so a later completed event can update the
    same transcript entry a running event created. Field name isn't confirmed
    against a real OpenCode trace -- checks several plausible candidates and
    returns None (safe degradation) if none match."""
    for src, key in ((part, "callID"), (part, "id"), (state, "callID"), (state, "id")):
        val = src.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _extract_tool_output(state: dict, part: dict) -> Optional[str]:
    """Best-effort tool output/diff extraction. Field name isn't confirmed
    against a real OpenCode trace -- tries several plausible candidates in
    order and logs at DEBUG (cheap, no-op when disabled) if none match, so
    real traces can be inspected and this list tuned later."""
    metadata = state.get("metadata") or {}
    candidates = [
        state.get("output"), metadata.get("diff"), metadata.get("patch"),
        metadata.get("stdout"), part.get("output"),
    ]
    for c in candidates:
        if c:
            return _stringify(c)
    log.debug("no output field found for tool_use, state keys=%s", sorted(state.keys()))
    return None


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


def _cap(text: Optional[str], limit: int = 8192) -> Optional[str]:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + "… [truncated]"


def _parse_event_stream(stdout: str) -> tuple[list[dict], str, int, bool]:
    """Parse newline-delimited JSON events from the FULL accumulated stdout,
    once the process has exited, to compute the final AgentResult summary
    (events/final_message/tokens_used/stopped).

    This does NOT emit activity or transcript events — that already happened
    live, per-line, in _process_live_event as the process ran. Re-emitting
    here used to double-log every tool call/message and double-count tokens
    (this function's total_tokens used to also get added to the budget a
    second time in run_goal, on top of the live per-step_finish adds) — fixed
    by making this purely a read-only summary pass.

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
