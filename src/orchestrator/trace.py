"""Orchestrator flow trace — structured logs for debugging and agent handoff.

State transitions and significant orchestrator events are logged with consistent
key=value fields so an investigating agent can grep ``solo.orch`` or the trace
file under ``data/orch-trace.log``.

Set ``LOG_LEVEL=DEBUG`` for verbose subprocess output snippets on failures.
"""

from __future__ import annotations

import logging
import logging.handlers
from contextvars import ContextVar
from typing import Any, Optional

from ..config import settings

log = logging.getLogger("solo.orch")

_cycle: ContextVar[Optional[int]] = ContextVar("orch_cycle", default=None)
_phase: ContextVar[str] = ContextVar("orch_phase", default="")
_project: ContextVar[Optional[str]] = ContextVar("orch_project", default=None)
_task: ContextVar[Optional[str]] = ContextVar("orch_task", default=None)
_session: ContextVar[Optional[str]] = ContextVar("orch_session", default=None)

_file_handler_attached = False


def setup() -> None:
    """Attach a rotating trace file handler. Safe to call multiple times."""
    global _file_handler_attached
    if _file_handler_attached:
        return
    path = settings.orch_trace_file
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    )
    handler.setLevel(logging.DEBUG)
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    _file_handler_attached = True
    log.info(
        "event=trace_ready path=%s",
        path,
    )


def bind(
    *,
    cycle: Optional[int] = None,
    phase: Optional[str] = None,
    project_id: Optional[str] = None,
    task: Optional[str] = None,
    session_id: Optional[str] = None,
) -> None:
    """Set context fields included on every subsequent trace line."""
    if cycle is not None:
        _cycle.set(cycle)
    if phase is not None:
        _phase.set(phase)
    if project_id is not None:
        _project.set(project_id)
    if task is not None:
        _task.set(task)
    if session_id is not None:
        _session.set(session_id)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _escape(text: str) -> str:
    return text.replace("\n", "\\n").replace("\r", "")


def _quote(value: Any) -> str:
    s = _escape(str(value))
    if not s:
        return '""'
    if any(c in s for c in (' ', '"', "=")):
        return f'"{s}"'
    return s


def _kv(**fields: Any) -> str:
    parts: list[str] = []
    cycle = _cycle.get()
    phase = _phase.get()
    project = _project.get()
    task = _task.get()
    session = _session.get()
    if cycle is not None:
        parts.append(f"cycle={cycle}")
    if phase:
        parts.append(f"phase={phase}")
    if project:
        parts.append(f"project={project}")
    if task:
        parts.append(f"task={_quote(_truncate(task, 120))}")
    if session:
        parts.append(f"session={session}")
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            parts.append(f"{key}={str(value).lower()}")
        else:
            parts.append(f"{key}={_quote(value)}")
    return " ".join(parts)


def event(level: int, name: str, **fields: Any) -> None:
    log.log(level, "event=%s %s", name, _kv(**fields))


def info(name: str, **fields: Any) -> None:
    event(logging.INFO, name, **fields)


def warning(name: str, **fields: Any) -> None:
    event(logging.WARNING, name, **fields)


def error(name: str, **fields: Any) -> None:
    event(logging.ERROR, name, **fields)


def debug(name: str, **fields: Any) -> None:
    event(logging.DEBUG, name, **fields)


def phase_transition(from_phase: str, to_phase: str, **extra: Any) -> None:
    _phase.set(to_phase)
    info("phase_transition", from_phase=from_phase, to_phase=to_phase, **extra)


def lifecycle(action: str, **extra: Any) -> None:
    info(f"lifecycle_{action}", **extra)


def guardrail(kind: str, detail: str, **extra: Any) -> None:
    warning("guardrail", kind=kind, detail=detail, **extra)


def agent_result(
    *,
    ok: bool,
    session_id: str,
    agent_phase: str,
    tokens: int = 0,
    timed_out: bool = False,
    error: Optional[str] = None,
    final_message: Optional[str] = None,
    event_count: int = 0,
) -> None:
    bind(session_id=session_id)
    fn = info if ok else warning
    fn(
        "agent_result",
        ok=ok,
        agent_phase=agent_phase,
        session_id=session_id,
        tokens=tokens,
        timed_out=timed_out,
        error=error,
        final_message=_truncate(final_message or "", 200) or None,
        events=event_count,
    )


def verify_result(
    *,
    ok: bool,
    returncode: int,
    command: str = "",
    stdout: str = "",
    stderr: str = "",
    truncated: bool = False,
) -> None:
    fn = info if ok else warning
    fn(
        "verify_result",
        ok=ok,
        returncode=returncode,
        command=command or None,
        truncated=truncated,
    )
    if not ok:
        output_snippet("verify_stdout", stdout)
        output_snippet("verify_stderr", stderr)


def output_snippet(label: str, text: str, limit: int = 1500) -> None:
    if not text or not text.strip():
        return
    snippet = text if len(text) <= limit else text[-limit:]
    debug(label, text=_truncate(_escape(snippet), limit))


def git_action(action: str, **extra: Any) -> None:
    info(f"git_{action}", **extra)
