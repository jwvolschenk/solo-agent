"""Pydantic data models for all three subsystems.

These models are the single source of truth for shapes flowing through the API,
the WebSocket, and the DB layer. Keep them dependency-free except pydantic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ============================================================================
# Monitoring — llama-server snapshots
# ============================================================================


class HealthState(BaseModel):
    """Result of probing llama-server /health (defensive across versions)."""

    status: Literal["ok", "loading", "error", "no_slot_available", "offline"]
    http_status: int = 0  # 0 when unreachable
    message: str = ""  # human-readable detail (e.g. the error body, or "unreachable")
    slots_idle: Optional[int] = None  # present in classic schema only
    slots_processing: Optional[int] = None
    checked_at: datetime = Field(default_factory=datetime.utcnow)


class MetricsSnapshot(BaseModel):
    """One point-in-time reading from /metrics (Prometheus text, llamacpp: prefix).

    Fields default to None so a partial/offline response doesn't break the model.
    Throughput fields are gauges (t/s); the *_total fields are counters.
    """

    # Throughput (gauges, tokens/sec) — the headline numbers for the dashboard
    prompt_tokens_seconds: Optional[float] = None  # current prefill throughput
    predicted_tokens_seconds: Optional[float] = None  # current decode throughput

    # Counters — totals for the "tokens processed" panel
    prompt_tokens_total: Optional[int] = None
    tokens_predicted_total: Optional[int] = None
    prompt_seconds_total: Optional[float] = None
    tokens_predicted_seconds_total: Optional[float] = None
    n_decode_total: Optional[int] = None
    n_tokens_max: Optional[int] = None

    # Queue/slots (gauges)
    requests_processing: Optional[int] = None
    requests_deferred: Optional[int] = None
    n_busy_slots_per_decode: Optional[float] = None

    raw: dict[str, float] = Field(
        default_factory=dict, description="Every llamacpp:<name> -> value parsed, raw"
    )
    captured_at: datetime = Field(default_factory=datetime.utcnow)


class SlotInfo(BaseModel):
    """One slot from /slots. Defensive: classic + current schema fields."""

    id: int
    n_ctx: int = 0
    is_processing: bool = False
    # current schema fields (nested or otherwise) — present when available
    n_prompt_tokens: Optional[int] = None
    n_decoded: Optional[int] = None  # pulled from next_token when present
    generated_text: Optional[str] = None  # 'generated' field on current schema
    model: Optional[str] = None  # classic schema only
    raw: dict[str, Any] = Field(default_factory=dict)


class Props(BaseModel):
    """Model info + default generation params from /props. Defensive both schemas."""

    model_alias: Optional[str] = None
    model_path: Optional[str] = None
    total_slots: Optional[int] = None
    chat_template: Optional[str] = None
    bos_token: Optional[str] = None
    eos_token: Optional[str] = None
    n_ctx: Optional[int] = None
    # Sampling params — current schema nests under default_generation_settings.params,
    # classic is flat. We deep-get into a flat dict either way for the UI.
    params: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# Agent activity feed
# ============================================================================


class ActivityEvent(BaseModel):
    """An event posted by (or observed about) the running agent."""

    type: Literal["task", "tool", "file", "error", "system"] = "system"
    message: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)
    id: Optional[int] = None  # DB row id when read back


# ============================================================================
# Shared state files (tasks / journal / plan / summaries)
# ============================================================================


class TaskItem(BaseModel):
    """One line parsed from tasks.md."""

    status: Literal["todo", "in_progress", "done", "blocked"] = "todo"
    text: str
    raw: str = ""


class JournalEntry(BaseModel):
    """One entry from journal.md."""

    text: str
    raw: str = ""


class StateFile(BaseModel):
    """A parsed shared-state file."""

    name: str
    path: str
    exists: bool = False
    mtime: Optional[datetime] = None
    size: int = 0
    content: str = ""
    # structured payloads depending on file:
    tasks: list[TaskItem] = Field(default_factory=list)  # for tasks.md
    entries: list[JournalEntry] = Field(default_factory=list)  # for journal.md


# ============================================================================
# Directives (human -> agent feedback queue, full lifecycle)
# ============================================================================


DirectiveStatus = Literal["pending", "acknowledged", "done"]
DirectivePriority = Literal["high", "normal", "low"]


class Directive(BaseModel):
    """A single directive. Full lifecycle: pending -> acknowledged -> done."""

    id: str  # e.g. "d1", "d2"
    created_at: datetime
    priority: DirectivePriority = "normal"
    text: str
    status: DirectiveStatus = "pending"
    raw: str = ""  # original block text in directives.md


class DirectiveCreate(BaseModel):
    priority: DirectivePriority = "normal"
    text: str = Field(..., min_length=1)


class DirectiveUpdate(BaseModel):
    """PATCH payload. Only status may change (priority/text are immutable)."""

    status: DirectiveStatus


# ============================================================================
# Orchestrator (Ralph loop)
# ============================================================================


CycleOutcome = Literal["running", "passed", "failed", "reverted", "paused", "errored"]


class CycleRecord(BaseModel):
    """One completed (or in-progress) iteration of the Ralph loop."""

    id: Optional[int] = None
    cycle_number: int
    phase: str = "idle"  # current/last phase
    project_id: Optional[str] = None
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    outcome: CycleOutcome = "running"
    snapshot_sha: Optional[str] = None  # git sha before this cycle's changes
    head_sha: Optional[str] = None  # git sha after
    lines_changed: int = 0
    tokens_used: int = 0
    tasks_attempted: int = 0
    tasks_passed: int = 0
    error: Optional[str] = None
    summary: Optional[str] = None
    agent_session_id: Optional[str] = None


class OrchestratorState(BaseModel):
    """The controller's live state, surfaced via /api/orchestrator/state and the WS."""

    phase: str = "idle"  # idle|reflect|plan|execute|verify|record|paused|stopped|error
    running: bool = False
    cycle_number: int = 0
    current_task: Optional[str] = None
    last_outcome: Optional[CycleOutcome] = None
    last_error: Optional[str] = None
    last_snapshot_sha: Optional[str] = None
    # Budget tracking
    cycle_tokens_used: int = 0
    daily_tokens_used: int = 0
    daily_budget_date: Optional[str] = None  # YYYY-MM-DD, for daily reset
    # Stall detection
    consecutive_low_change_cycles: int = 0
    consecutive_fail_cycles: int = 0
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    agent_session_id: Optional[str] = None
    project_id: Optional[str] = None
    stop_after_cycle: bool = False  # soft-stop: finish this cycle then stop


# ============================================================================
# Projects
# ============================================================================


class Project(BaseModel):
    """A project the orchestrator can work on. Persisted in DB."""

    id: str  # slug
    name: str
    goal: str = ""
    project_path: str
    verify_command: str = ""
    work_branch: str = "solo-agent/auto"
    stop_after_cycle: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    # denormalized fields for the UI (not stored in the projects table):
    is_active: bool = False
    orch_phase: str = "idle"


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1)
    goal: str = ""
    project_path: str = Field(..., min_length=1)
    verify_command: str = ""


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    goal: Optional[str] = None
    project_path: Optional[str] = None
    verify_command: Optional[str] = None


# ============================================================================
# Aggregated dashboard snapshot (sent over WebSocket)
# ============================================================================


class DashboardSnapshot(BaseModel):
    """Everything the frontend needs in one WS message."""

    health: Optional[HealthState] = None
    metrics: Optional[MetricsSnapshot] = None
    slots: list[SlotInfo] = Field(default_factory=list)
    activity: list[ActivityEvent] = Field(default_factory=list)
    directives: list[Directive] = Field(default_factory=list)
    orchestrator: Optional[OrchestratorState] = None
    current_cycle: Optional[CycleRecord] = None
    sent_at: datetime = Field(default_factory=datetime.utcnow)
