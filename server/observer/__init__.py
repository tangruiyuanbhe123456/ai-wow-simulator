"""Server-side broadcaster / observer log helper."""
from __future__ import annotations
import time
import json
from typing import Any

from server.db import connect, init_schema


def recent_combat(limit: int = 50, lang: str = "zh") -> list:
    conn = connect()
    cur = conn.cursor()
    cur.execute("""SELECT ts, actor_name, action, target_name, detail FROM combat_log
                   ORDER BY id DESC LIMIT ?""", (limit,))
    return [dict(r) for r in cur.fetchall()]


def recent_chat(limit: int = 30) -> list:
    conn = connect()
    cur = conn.cursor()
    cur.execute("""SELECT ts, channel, sender_name, body FROM chat_log
                   ORDER BY id DESC LIMIT ?""", (limit,))
    return [dict(r) for r in cur.fetchall()]
