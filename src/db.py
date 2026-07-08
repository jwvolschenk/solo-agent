"""SQLite persistence layer.

One file (data/solo-agent.db) holds all subsystem state:
  - metrics_history     ring buffer of /metrics snapshots (24h)
  - activity_log        agent activity events (7d / 5000 rows)
  - directives          human->agent feedback (full lifecycle)
  - directive_history   status transition audit
  - cycles              Ralph loop cycle records
  - orch_state          single-row orchestrator state (for resume)
  - token_usage         per-day token totals (for the budget governor)

Uses stdlib sqlite3 + aiosqlite-style async wrappers (we keep a single threaded
connection per event-loop task via run_in_executor to avoid adding a dep).
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at  TEXT NOT NULL,
    prompt_tokens_seconds       REAL,
    predicted_tokens_seconds    REAL,
    prompt_tokens_total         INTEGER,
    tokens_predicted_total      INTEGER,
    requests_processing         INTEGER,
    requests_deferred           INTEGER,
    n_busy_slots_per_decode     REAL,
    n_tokens_max                INTEGER,
    raw_json                    TEXT
);

CREATE TABLE IF NOT EXISTS activity_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT NOT NULL,
    type         TEXT NOT NULL,
    message      TEXT NOT NULL,
    metadata_json TEXT,
    project_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(timestamp DESC);

CREATE TABLE IF NOT EXISTS directives (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    priority     TEXT NOT NULL DEFAULT 'normal',
    text         TEXT NOT NULL,
    current_status TEXT NOT NULL DEFAULT 'pending',
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS directive_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    directive_id TEXT NOT NULL,
    status       TEXT NOT NULL,
    seen_at      TEXT NOT NULL,
    FOREIGN KEY (directive_id) REFERENCES directives(id)
);
CREATE INDEX IF NOT EXISTS idx_dirhist ON directive_history(directive_id, seen_at);

CREATE TABLE IF NOT EXISTS cycles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_number    INTEGER NOT NULL,
    phase           TEXT NOT NULL DEFAULT 'idle',
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    outcome         TEXT NOT NULL DEFAULT 'running',
    snapshot_sha    TEXT,
    head_sha        TEXT,
    lines_changed   INTEGER NOT NULL DEFAULT 0,
    tokens_used     INTEGER NOT NULL DEFAULT 0,
    tasks_attempted INTEGER NOT NULL DEFAULT 0,
    tasks_passed    INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    summary         TEXT,
    agent_session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_cycles_num ON cycles(cycle_number DESC);

CREATE TABLE IF NOT EXISTS orch_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS token_usage (
    day    TEXT PRIMARY KEY,   -- YYYY-MM-DD
    tokens INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,      -- slug e.g. "brain-buzz"
    name            TEXT NOT NULL,
    goal            TEXT NOT NULL DEFAULT '',
    project_path    TEXT NOT NULL,
    verify_command  TEXT NOT NULL DEFAULT '',
    work_branch     TEXT NOT NULL DEFAULT 'solo-agent/auto',
    stop_after_cycle INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""

# Idempotent migrations for columns added after initial release.
# Wrapped in try/except so re-running init_db() on an existing DB is safe.
_MIGRATIONS = [
    "ALTER TABLE cycles ADD COLUMN project_id TEXT",
    "ALTER TABLE activity_log ADD COLUMN project_id TEXT",
]


# We use short-lived per-call connections (check_same_thread=False) rather than
# a shared connection, because FastAPI runs handlers across threads and sqlite3
# connections are thread-bound. Our write volume is low (a few rows per poll),
# so the overhead of opening a connection per write is negligible.
_lock = threading.Lock()


def _connect(path: Optional[Path | str] = None) -> sqlite3.Connection:
    # Resolve settings.db_path at CALL time so monkeypatching in tests works.
    if path is None:
        path = settings.db_path
    conn = sqlite3.connect(str(path), detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(path: Optional[Path | str] = None) -> None:
    """Create the schema if missing. Safe to call on every startup."""
    if path is None:
        path = settings.db_path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    try:
        conn.executescript(SCHEMA)
        # run idempotent migrations (ignore "duplicate column" on re-run)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()
    finally:
        conn.close()


@contextmanager
def write_conn() -> Iterator[sqlite3.Connection]:
    """Yield a fresh write connection (guarded so writes serialize)."""
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def query_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Run a read query on a throwaway connection (avoid blocking the writer)."""
    if not Path(settings.db_path).exists():
        return []  # DB not initialized yet; treat as empty
    conn = _connect()
    try:
        cur = conn.execute(sql, params)
        return cur.fetchall()
    except sqlite3.OperationalError:
        return []  # table missing -> not yet initialized
    finally:
        conn.close()


def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    rows = query_all(sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def insert_metrics(snapshot) -> None:
    """Insert a MetricsSnapshot + prune to retention window."""
    import json

    with write_conn() as c:
        c.execute(
            """INSERT INTO metrics_history
               (captured_at, prompt_tokens_seconds, predicted_tokens_seconds,
                prompt_tokens_total, tokens_predicted_total, requests_processing,
                requests_deferred, n_busy_slots_per_decode, n_tokens_max, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot.captured_at.isoformat(),
                snapshot.prompt_tokens_seconds,
                snapshot.predicted_tokens_seconds,
                snapshot.prompt_tokens_total,
                snapshot.tokens_predicted_total,
                snapshot.requests_processing,
                snapshot.requests_deferred,
                snapshot.n_busy_slots_per_decode,
                snapshot.n_tokens_max,
                json.dumps(snapshot.raw),
            ),
        )
        # prune
        cutoff = (datetime.utcnow() - timedelta(hours=settings.metrics_retention_hours)).isoformat()
        c.execute("DELETE FROM metrics_history WHERE captured_at < ?", (cutoff,))


def fetch_metrics_history(minutes: int = 60) -> list[sqlite3.Row]:
    since = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
    return query_all(
        "SELECT * FROM metrics_history WHERE captured_at >= ? ORDER BY captured_at ASC",
        (since,),
    )


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


def insert_activity(event) -> int:
    import json

    with write_conn() as c:
        cur = c.execute(
            "INSERT INTO activity_log (timestamp, type, message, metadata_json, project_id) VALUES (?,?,?,?,?)",
            (
                event.timestamp.isoformat(),
                event.type,
                event.message,
                json.dumps(event.metadata),
                event.project_id,
            ),
        )
        _prune_activity(c)
        c.commit()
        return int(cur.lastrowid)


def _prune_activity(c: sqlite3.Connection) -> None:
    cutoff = (datetime.utcnow() - timedelta(days=settings.activity_retention_days)).isoformat()
    c.execute("DELETE FROM activity_log WHERE timestamp < ?", (cutoff,))
    c.execute(
        """DELETE FROM activity_log WHERE id NOT IN (
               SELECT id FROM activity_log ORDER BY id DESC LIMIT ?
           )""",
        (settings.activity_retention_rows,),
    )


def fetch_activity(limit: int = 50, project_id: str = "") -> list[sqlite3.Row]:
    if project_id:
        return query_all(
            "SELECT * FROM activity_log WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        )
    return query_all(
        "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
    )


# ---------------------------------------------------------------------------
# Directives
# ---------------------------------------------------------------------------


def upsert_directive(d) -> None:
    """Insert or update a directive (by id). Records status transitions."""
    with write_conn() as c:
        row = c.execute(
            "SELECT id, current_status FROM directives WHERE id = ?", (d.id,)
        ).fetchone()
        if row is None:
            c.execute(
                """INSERT INTO directives
                   (id, created_at, priority, text, current_status, first_seen_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    d.id,
                    d.created_at.isoformat(),
                    d.priority,
                    d.text,
                    d.status,
                    datetime.utcnow().isoformat(),
                ),
            )
            c.execute(
                "INSERT INTO directive_history (directive_id, status, seen_at) VALUES (?,?,?)",
                (d.id, d.status, datetime.utcnow().isoformat()),
            )
        else:
            prev = row["current_status"]
            if prev != d.status:
                c.execute(
                    "UPDATE directives SET current_status = ? WHERE id = ?",
                    (d.status, d.id),
                )
                c.execute(
                    "INSERT INTO directive_history (directive_id, status, seen_at) VALUES (?,?,?)",
                    (d.id, d.status, datetime.utcnow().isoformat()),
                )
        c.commit()


def fetch_directives() -> list[sqlite3.Row]:
    return query_all("SELECT * FROM directives ORDER BY created_at ASC")


def next_directive_id() -> str:
    """Mint the next directive id: max existing 'dN' + 1, else d1."""
    row = query_one("SELECT id FROM directives ORDER BY id DESC LIMIT 1")
    if row is None:
        return "d1"
    try:
        n = int(row["id"].lstrip("d"))
    except (ValueError, IndexError):
        n = 0
    return f"d{n + 1}"


# ---------------------------------------------------------------------------
# Cycles & orchestrator state
# ---------------------------------------------------------------------------


def insert_cycle(rec) -> int:
    with write_conn() as c:
        cur = c.execute(
            """INSERT INTO cycles
               (cycle_number, phase, project_id, started_at, ended_at, outcome, snapshot_sha,
                head_sha, lines_changed, tokens_used, tasks_attempted, tasks_passed,
                error, summary, agent_session_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec.cycle_number,
                rec.phase,
                getattr(rec, "project_id", None),
                rec.started_at.isoformat(),
                rec.ended_at.isoformat() if rec.ended_at else None,
                rec.outcome,
                rec.snapshot_sha,
                rec.head_sha,
                rec.lines_changed,
                rec.tokens_used,
                rec.tasks_attempted,
                rec.tasks_passed,
                rec.error,
                rec.summary,
                rec.agent_session_id,
            ),
        )
        c.commit()
        return int(cur.lastrowid)


def update_cycle(cycle_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals: list[Any] = []
    for k, v in fields.items():
        if isinstance(v, datetime):
            v = v.isoformat()
        vals.append(v)
    vals.append(cycle_id)
    with write_conn() as c:
        c.execute(f"UPDATE cycles SET {cols} WHERE id = ?", vals)
        c.commit()


def fetch_cycles(limit: int = 50, project_id: str = "") -> list[sqlite3.Row]:
    if project_id:
        return query_all(
            "SELECT * FROM cycles WHERE project_id = ? ORDER BY cycle_number DESC LIMIT ?",
            (project_id, limit),
        )
    return query_all(
        "SELECT * FROM cycles ORDER BY cycle_number DESC LIMIT ?", (limit,)
    )


# --- orchestrator key/value state (single logical document, JSON-encoded) ----


def get_orch_state(project_id: str = "") -> dict[str, Any]:
    key = f"state:{project_id}" if project_id else "state"
    row = query_one("SELECT value FROM orch_state WHERE key = ?", (key,))
    if row is None:
        return {}
    import json

    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return {}


def set_orch_state(state: dict[str, Any], project_id: str = "") -> None:
    import json

    key = f"state:{project_id}" if project_id else "state"
    payload = json.dumps(state, default=str)
    with write_conn() as c:
        c.execute(
            """INSERT INTO orch_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, payload),
        )
        c.commit()


# --- token budget -----------------------------------------------------------


def add_tokens(day: str, tokens: int) -> int:
    """Add tokens to a day's total and return the new total."""
    with write_conn() as c:
        c.execute(
            """INSERT INTO token_usage (day, tokens) VALUES (?, ?)
               ON CONFLICT(day) DO UPDATE SET tokens = tokens + excluded.tokens""",
            (day, tokens),
        )
        c.commit()
    row = query_one("SELECT tokens FROM token_usage WHERE day = ?", (day,))
    return int(row["tokens"]) if row else 0


def tokens_for_day(day: str) -> int:
    row = query_one("SELECT tokens FROM token_usage WHERE day = ?", (day,))
    return int(row["tokens"]) if row else 0


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def insert_project(p) -> None:
    """Insert a new project row. p must have: id, name, goal, project_path,
    verify_command, work_branch, stop_after_cycle, created_at, updated_at."""
    with write_conn() as c:
        c.execute(
            """INSERT INTO projects
               (id, name, goal, project_path, verify_command, work_branch,
                stop_after_cycle, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                p["id"], p["name"], p["goal"], p["project_path"],
                p["verify_command"], p["work_branch"],
                int(p["stop_after_cycle"]), p["created_at"], p["updated_at"],
            ),
        )
        c.commit()


def update_project(project_id: str, **fields) -> None:
    """Update one or more fields on a project."""
    if not fields:
        return
    allowed = {"name", "goal", "project_path", "verify_command", "work_branch", "stop_after_cycle", "updated_at"}
    cols = {k: v for k, v in fields.items() if k in allowed}
    if not cols:
        return
    set_clause = ", ".join(f"{k} = ?" for k in cols)
    vals = list(cols.values()) + [project_id]
    with write_conn() as c:
        c.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", vals)
        c.commit()


def fetch_project(project_id: str):
    return query_one("SELECT * FROM projects WHERE id = ?", (project_id,))


def fetch_projects() -> list[sqlite3.Row]:
    return query_all("SELECT * FROM projects ORDER BY created_at ASC")


def delete_project(project_id: str) -> None:
    with write_conn() as c:
        c.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        c.commit()


def set_active_project(project_id: str) -> None:
    """Store the active project id in the orch_state KV table."""
    with write_conn() as c:
        c.execute(
            """INSERT INTO orch_state (key, value) VALUES ('active_project', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (project_id,),
        )
        c.commit()


def get_active_project() -> Optional[str]:
    row = query_one("SELECT value FROM orch_state WHERE key = 'active_project'")
    return row["value"] if row else None
