"""
sidecar/effect_log.py — SQLite-backed WAL for Effect Log, Semantic Checkpoints,
                         Filesystem Snapshots, and Trunk/Fork registry.

Tables:
  - sessions:    one row per session
  - effect_log:  one row per tool execution or LLM generation (append-only)
  - checkpoints: one row per session — messages + cursor (upserted atomically)
  - snapshots:   one row per filesystem snapshot (for REPLAYABLE_EXPENSIVE recovery)
  - trunk:       one row per trunk version (canonical sandbox state)
  - forks:       one row per parallel task fork
"""
import sqlite3
import json
import uuid
from datetime import datetime
from pathlib import Path

DB_PATH = Path("sidecar_data/effect_log.db")


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _migrate(conn: sqlite3.Connection, sql: str):
    """Run a schema migration statement, ignoring 'duplicate column' errors."""
    try:
        conn.execute(sql)
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id   TEXT PRIMARY KEY,
                created_at   TEXT,
                status       TEXT DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS effect_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id       TEXT NOT NULL,
                step             INTEGER NOT NULL,
                event_type       TEXT NOT NULL,
                tool_name        TEXT,
                effect           TEXT,
                command          TEXT,
                result           TEXT,
                idempotency_key  TEXT,
                timestamp        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                session_id   TEXT PRIMARY KEY,
                messages     TEXT NOT NULL,
                cursor       INTEGER NOT NULL DEFAULT 0,
                snapshot_id  TEXT
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id   TEXT PRIMARY KEY,
                session_id    TEXT NOT NULL,
                step          INTEGER NOT NULL,
                storage_path  TEXT NOT NULL,
                size_bytes    INTEGER,
                created_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trunk (
                trunk_id      TEXT PRIMARY KEY,
                parent_id     TEXT,
                snapshot_path TEXT NOT NULL,
                effect_cursor INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS forks (
                fork_id       TEXT PRIMARY KEY,
                trunk_id      TEXT NOT NULL,
                status        TEXT DEFAULT 'active',
                changeset     TEXT,
                created_at    TEXT NOT NULL,
                resolved_at   TEXT
            );
        """)

    # ── Schema migrations (idempotent — safe to run on existing DBs) ───────────
    _migrate(conn, "ALTER TABLE checkpoints ADD COLUMN snapshot_id TEXT")



# ── Session ────────────────────────────────────────────────────────────────────

def create_session() -> str:
    session_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, created_at) VALUES (?, ?)",
            (session_id, datetime.now().isoformat())
        )
        conn.execute(
            "INSERT INTO checkpoints (session_id, messages, cursor) VALUES (?, ?, ?)",
            (session_id, "[]", 0)
        )
    return session_id


# ── Effect Log ─────────────────────────────────────────────────────────────────

def log_tool_event(session_id: str, tool_name: str, effect: str, command: str, result: str) -> int:
    """Write a completed tool execution into the effect log. Returns the step number."""
    idem_key = str(uuid.uuid4())
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO effect_log
               (session_id, step, event_type, tool_name, effect, command, result, idempotency_key, timestamp)
               VALUES (?,
                       (SELECT COALESCE(MAX(step)+1, 0) FROM effect_log WHERE session_id=?),
                       'tool_execution', ?, ?, ?, ?, ?, ?)""",
            (session_id, session_id, tool_name, effect, command, result, idem_key,
             datetime.now().isoformat())
        )
        # Return the step, not the rowid
        row = conn.execute(
            "SELECT step FROM effect_log WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return row["step"] if row else 0


def log_llm_event(session_id: str, content_blocks: list) -> int:
    """Record an LLM generation event. Returns the step number."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO effect_log
               (session_id, step, event_type, command, result, timestamp)
               VALUES (?,
                       (SELECT COALESCE(MAX(step)+1, 0) FROM effect_log WHERE session_id=?),
                       'llm_generation', ?, ?, ?)""",
            (session_id, session_id,
             json.dumps(content_blocks, ensure_ascii=False),
             "",
             datetime.now().isoformat())
        )
        row = conn.execute(
            "SELECT step FROM effect_log WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return row["step"] if row else 0


def get_effect_log_from(session_id: str, from_step: int = 0) -> list[dict]:
    """Fetch all tool_execution events from step onwards (for replay)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM effect_log
               WHERE session_id=? AND step >= ? AND event_type='tool_execution'
               ORDER BY step""",
            (session_id, from_step)
        ).fetchall()
    return [dict(r) for r in rows]


def get_last_llm_step(session_id: str) -> int | None:
    """Returns the step number of the last llm_generation event, or None."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT step FROM effect_log
               WHERE session_id=? AND event_type='llm_generation'
               ORDER BY step DESC LIMIT 1""",
            (session_id,)
        ).fetchone()
    return row["step"] if row else None


# ── Checkpoints ────────────────────────────────────────────────────────────────

def save_checkpoint(session_id: str, messages: list, cursor: int, snapshot_id: str | None = None):
    """Atomically update LLM history, effect log cursor, and snapshot pointer."""
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO checkpoints (session_id, messages, cursor, snapshot_id)
               VALUES (?, ?, ?, ?)""",
            (session_id, json.dumps(messages, ensure_ascii=False), cursor, snapshot_id)
        )


def load_checkpoint(session_id: str) -> tuple[list, int, str | None]:
    """Return (messages, cursor, snapshot_id) for a session."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT messages, cursor, snapshot_id FROM checkpoints WHERE session_id=?",
            (session_id,)
        ).fetchone()
    if row:
        return json.loads(row["messages"]), row["cursor"], row["snapshot_id"]
    return [], 0, None


# ── Snapshots ──────────────────────────────────────────────────────────────────

def save_snapshot(session_id: str, snapshot_id: str, step: int,
                  storage_path: str, size_bytes: int | None = None) -> str:
    """Record a new filesystem snapshot in the DB."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO snapshots (snapshot_id, session_id, step, storage_path, size_bytes, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (snapshot_id, session_id, step, storage_path, size_bytes, datetime.now().isoformat())
        )
    return snapshot_id


def get_snapshot(snapshot_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
    return dict(row) if row else None


def get_latest_snapshot_before(session_id: str, step: int) -> dict | None:
    """Return the most recent snapshot taken at or before the given step."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM snapshots
               WHERE session_id=? AND step <= ?
               ORDER BY step DESC LIMIT 1""",
            (session_id, step)
        ).fetchone()
    return dict(row) if row else None


def list_snapshots(session_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE session_id=? ORDER BY step",
            (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Trunk ──────────────────────────────────────────────────────────────────────

def create_trunk(snapshot_path: str, effect_cursor: int = 0, parent_id: str | None = None) -> str:
    """Create a new trunk version. Returns trunk_id."""
    trunk_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO trunk (trunk_id, parent_id, snapshot_path, effect_cursor, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (trunk_id, parent_id, snapshot_path, effect_cursor, datetime.now().isoformat())
        )
    return trunk_id


def get_trunk(trunk_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trunk WHERE trunk_id=?", (trunk_id,)
        ).fetchone()
    return dict(row) if row else None


def get_latest_trunk() -> dict | None:
    """Return the most recently created trunk version."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trunk ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# ── Forks ──────────────────────────────────────────────────────────────────────

def create_fork(session_id: str, trunk_id: str) -> str:
    """Register a new fork. fork_id == session_id. Returns fork_id."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO forks (fork_id, trunk_id, status, created_at)
               VALUES (?, ?, 'active', ?)""",
            (session_id, trunk_id, datetime.now().isoformat())
        )
    return session_id


def get_fork(fork_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM forks WHERE fork_id=?", (fork_id,)
        ).fetchone()
    return dict(row) if row else None


def update_fork_status(fork_id: str, status: str, changeset: list | None = None):
    with get_conn() as conn:
        conn.execute(
            """UPDATE forks SET status=?, changeset=?, resolved_at=?
               WHERE fork_id=?""",
            (status,
             json.dumps(changeset) if changeset is not None else None,
             datetime.now().isoformat(),
             fork_id)
        )


def list_active_forks(trunk_id: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if trunk_id:
            rows = conn.execute(
                "SELECT * FROM forks WHERE status='active' AND trunk_id=?", (trunk_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM forks WHERE status='active'"
            ).fetchall()
    return [dict(r) for r in rows]
