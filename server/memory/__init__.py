"""V13 Memory: persistent event log + context loader."""
from __future__ import annotations
import time
from typing import Any


def record(conn, pid: str, memory_type: str, *,
           weight: int = 50,
           actor_pid: str | None = None,
           title_zh: str = "", title_en: str = "",
           body_zh: str = "", body_en: str = "") -> int:
    """Record a memory. Returns memory id."""
    cur = conn.execute(
        """INSERT INTO bot_memories
           (pid, ts, memory_type, weight, actor_pid,
            title_zh, title_en, body_zh, body_en)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pid, time.time(), memory_type, weight, actor_pid,
         title_zh, title_en, body_zh, body_en),
    )
    conn.commit()
    return cur.lastrowid


def list_memories(conn, pid: str, *, limit: int = 20,
                  min_weight: int = 0) -> list[dict]:
    cur = conn.execute(
        """SELECT id, ts, memory_type, weight, actor_pid,
                  title_zh, title_en, body_zh, body_en
           FROM bot_memories
           WHERE pid=? AND weight >= ?
           ORDER BY weight DESC, ts DESC
           LIMIT ?""",
        (pid, min_weight, limit),
    )
    rows = cur.fetchall()
    return [
        {
            "id": r[0], "ts": r[1], "memory_type": r[2], "weight": r[3],
            "actor_pid": r[4],
            "title_zh": r[5], "title_en": r[6],
            "body_zh": r[7], "body_en": r[8],
        }
        for r in rows
    ]


def load_context(conn, pid: str, n: int = 10) -> list[dict]:
    """Load top-N memories (by weight, then recency) for LLM context injection."""
    return list_memories(conn, pid, limit=n, min_weight=40)
