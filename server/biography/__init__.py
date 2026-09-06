"""V13 Biography: auto-written life story."""
from __future__ import annotations
import time


def append_chapter(conn, pid: str, chapter: str, *,
                   title_zh: str, title_en: str,
                   body_zh: str, body_en: str) -> int:
    """Append a biography chapter. Returns row id."""
    cur = conn.execute(
        """INSERT INTO bot_biography
           (pid, ts, chapter, title_zh, title_en, body_zh, body_en)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (pid, time.time(), chapter, title_zh, title_en, body_zh, body_en),
    )
    conn.commit()
    return cur.lastrowid


def list_biography(conn, pid: str) -> list[dict]:
    cur = conn.execute(
        """SELECT id, ts, chapter, title_zh, title_en, body_zh, body_en
           FROM bot_biography
           WHERE pid=?
           ORDER BY ts ASC""",
        (pid,),
    )
    rows = cur.fetchall()
    return [
        {
            "id": r[0], "ts": r[1], "chapter": r[2],
            "title_zh": r[3], "title_en": r[4],
            "body_zh": r[5], "body_en": r[6],
        }
        for r in rows
    ]
