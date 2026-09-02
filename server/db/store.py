"""SQLite Store wrapper for AI WoW Simulator.

Provides:
- connect(): returns a sqlite3.Connection with row factory + FK on
- init_schema(conn): idempotent schema setup
- row_to_dict(cursor_row): convenience
- jdump(obj) / jload(s): safe JSON helpers
- Store class: thin helper for common queries (optional convenience)
"""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any

from server.config import DB_PATH
from server.db.schema import SCHEMA_SQL, ensure_schema


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Return a sqlite3 connection. Default DB path is from config."""
    p = str(path or DB_PATH)
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # BUGFIX: busy_timeout lets concurrent writers wait instead of immediately
    # raising "database is locked". 5s is plenty for the FastAPI sync worker
    # pool (currently 40 threads).
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    # BUGFIX: synchronous=NORMAL is safe with WAL and faster than FULL.
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def jdump(obj: Any) -> str:
    """JSON dump that always succeeds (returns 'null' on bad input)."""
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return "null"


def jload(s: str | None, default: Any = None) -> Any:
    """JSON load with fallback default."""
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


class Store:
    """Thin convenience wrapper — optional. Most modules use raw cursor."""

    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or connect()

    def init(self) -> None:
        init_schema(self.conn)
