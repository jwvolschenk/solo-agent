"""Configuration for Solo Agent.

All settings come from environment variables (with sensible defaults) so the
same code runs locally and in Docker. See COLDSTART.md / docker-compose.yml.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Central configuration. Override any field via env var of the same name."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Server ----------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8090

    # --- llama-server (the thing being monitored) ------------------------------
    llama_server_url: str = "http://localhost:8080"
    poll_interval: float = 2.0  # seconds between metric polls
    http_timeout: float = 3.0  # per-request timeout to llama-server

    # --- Storage ---------------------------------------------------------------
    # SQLite lives under data/ (mounted volume in Docker).
    db_path: Path = REPO_ROOT / "data" / "solo-agent.db"
    metrics_retention_hours: int = 24
    activity_retention_days: int = 7
    activity_retention_rows: int = 5000

    # --- Shared state files ----------------------------------------------------
    # The agent's workspace (tasks.md, journal.md, plan.md, directives.md, ...).
    # Mounted into the container; defaults to ./workspace for local dev.
    state_dir: Path = REPO_ROOT / "workspace"

    # --- Orchestrator (Ralph loop) ---------------------------------------------
    # The folder the loop works in. This is BOTH the git target (the repo being
    # built/improved) AND OpenCode's --dir sandbox (where it opens its session).
    # Set via PROJECT_PATH env or the dashboard. May be empty/non-git — the
    # orchestrator auto-initializes git on start.
    project_path: Path = REPO_ROOT
    # The overarching goal, written to GOAL.md in the project. Free-form text
    # that drives every cycle. Set via GOAL env or the dashboard. Required to
    # start the loop (e.g. "Build a Tower Defense roguelite deckbuilder in Godot
    # with themes X, Y, Z"). The agent reads GOAL.md every session.
    goal: str = ""
    # Verification command, OPTIONAL. Empty string = no orchestrator-run gate;
    # the agent owns verification (runs whatever build/test the project uses).
    # Gates are project-specific and often unknown for a from-scratch build, so
    # the default is empty. Set only if you want a hard external gate, e.g.
    # "python -m pytest -q" or "godot --headless --check-only".
    verify_command: str = ""
    # Git isolation. The orchestrator never commits to base_branch directly.
    base_branch: str = "main"
    work_branch: str = "solo-agent/auto"
    auto_merge_to_base: bool = False

    # The agent command template. Substitutions: {repo} {title} {model} {prompt}.
    # Default drives OpenCode headlessly with a FRESH session each goal (Ralph
    # principle — no context carry-over). {session} is available if you want to
    # add --session/--continue for resumable runs, but we omit it by default
    # because passing a non-existent session id makes OpenCode error out.
    agent_command: str = (
        "opencode run --auto --format json "
        "--dir {repo} --title {title} --model {model} "
        '"{prompt}"'
    )
    agent_model: str = "llamacpp/qwen3.6-reap"

    # Per-goal timeout. OpenCode can hang indefinitely (issue #4255); never wait forever.
    per_goal_timeout_sec: float = 1800.0  # 30 min hard cap per goal
    cycle_timeout_sec: float = 7200.0  # 2h hard cap per full cycle
    max_trials_per_task: int = 3  # max reflect-retry attempts per backlog task
    max_steps_per_goal: int = 200  # soft cap on observed tool calls per goal

    # No token budgets — this runs against a local model, so cost is irrelevant
    # and the loop should churn 24/7. We still COUNT tokens for display, but they
    # never pause the loop. The real safety net is the guardrails (loop detector,
    # no-progress detector, git auto-revert, kill switch).

    # Diminishing-returns detector: N consecutive cycles with < threshold change
    # (or all verify-fail) => auto-pause and surface to the human.
    stall_detection_cycles: int = 3
    stall_min_lines_changed: int = 5

    # How the loop waits between cycles when running.
    inter_cycle_delay_sec: float = 5.0

    # Control surface for startup. The loop does NOT auto-start unless told to.
    autostart_orchestrator: bool = False

    def ensure_dirs(self) -> None:
        """Create runtime directories if missing."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)


# Single shared instance. Imported everywhere via `from src.config import settings`.
settings = Settings()  # type: ignore[call-arg]
settings.ensure_dirs()


# Convenience: phase enum lives here so it's importable from anywhere without cycles.
OrchestratorPhase = Literal[
    "idle", "reflect", "plan", "execute", "verify", "record", "paused", "stopped", "error"
]
