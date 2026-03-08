"""
sidecar/effect_log.py — SQLite-backed WAL for Effect Log and Semantic Checkpoints.

Two tables in one DB to allow atomic cursor + messages updates:
  - effect_log: one row per tool execution (intent + completion combined)
  - checkpoints: one row per session, stores LLM messages JSON and effect_log cursor
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
                session_id  TEXT PRIMARY KEY,
                messages    TEXT NOT NULL,
                cursor      INTEGER NOT NULL DEFAULT 0
            );
        """)


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


def log_tool_event(session_id: str, tool_name: str, effect: str, command: str, result: str) -> int:
    """Write a completed tool execution into the effect log and return its step id."""
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
        return cur.lastrowid


def log_llm_event(session_id: str, content_blocks: list) -> int:
    """Record an LLM generation event."""
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
        return cur.lastrowid


def save_checkpoint(session_id: str, messages: list, cursor: int):
    """Atomically update LLM history and effect log cursor in the same DB transaction."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO checkpoints (session_id, messages, cursor) VALUES (?, ?, ?)",
            (session_id, json.dumps(messages, ensure_ascii=False), cursor)
        )


def load_checkpoint(session_id: str) -> tuple[list, int]:
    """Return (messages, cursor) for a session."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT messages, cursor FROM checkpoints WHERE session_id=?", (session_id,)
        ).fetchone()
    if row:
        return json.loads(row["messages"]), row["cursor"]
    return [], 0


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
