"""SQLite database layer for the world."""
from __future__ import annotations
import sqlite3
import json
import time
from pathlib import Path
from typing import Any, Iterable
from server.config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cls TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    xp INTEGER NOT NULL DEFAULT 0,
    hp INTEGER NOT NULL,
    hp_max INTEGER NOT NULL,
    mp INTEGER NOT NULL,
    mp_max INTEGER NOT NULL,
    atk INTEGER NOT NULL,
    defn INTEGER NOT NULL,
    zone TEXT NOT NULL DEFAULT 'starter_village',
    pos_x INTEGER NOT NULL DEFAULT 0,
    pos_y INTEGER NOT NULL DEFAULT 0,
    gold INTEGER NOT NULL DEFAULT 0,
    guild_id TEXT,
    party_id TEXT,
    pvp_flag INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    player_id TEXT NOT NULL,
    issued_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,  -- mob / boss / gathering
    level INTEGER NOT NULL,
    hp INTEGER NOT NULL,
    hp_max INTEGER NOT NULL,
    atk INTEGER NOT NULL,
    defn INTEGER NOT NULL,
    zone TEXT NOT NULL,
    pos_x INTEGER NOT NULL,
    pos_y INTEGER NOT NULL,
    xp_reward INTEGER NOT NULL DEFAULT 10,
    gold_reward INTEGER NOT NULL DEFAULT 5,
    loot_table TEXT NOT NULL DEFAULT '[]',
    boss_room TEXT,
    alive INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1,
    slot TEXT,
    equipped INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS skills_used (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    damage INTEGER NOT NULL DEFAULT 0,
    heal INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS guilds (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    tag TEXT NOT NULL,
    leader_id TEXT NOT NULL,
    motd TEXT NOT NULL DEFAULT '',
    gold INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS guild_members (
    guild_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    rank TEXT NOT NULL DEFAULT 'member',
    joined_at REAL NOT NULL,
    PRIMARY KEY (guild_id, player_id)
);

CREATE TABLE IF NOT EXISTS guild_relations (
    guild_a TEXT NOT NULL,
    guild_b TEXT NOT NULL,
    relation TEXT NOT NULL,  -- war / ally
    since REAL NOT NULL,
    PRIMARY KEY (guild_a, guild_b)
);

CREATE TABLE IF NOT EXISTS parties (
    id TEXT PRIMARY KEY,
    leader_id TEXT NOT NULL,
    zone TEXT NOT NULL,
    target_kind TEXT,
    target_id TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS party_members (
    party_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    joined_at REAL NOT NULL,
    PRIMARY KEY (party_id, player_id)
);

CREATE TABLE IF NOT EXISTS quests (
    id TEXT PRIMARY KEY,
    player_id TEXT NOT NULL,
    template_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    progress TEXT NOT NULL DEFAULT '{}',
    reward_gold INTEGER NOT NULL DEFAULT 0,
    reward_xp INTEGER NOT NULL DEFAULT 0,
    accepted_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS combat_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor_id TEXT,
    actor_name TEXT,
    action TEXT NOT NULL,
    target_id TEXT,
    target_name TEXT,
    detail TEXT,
    lang TEXT NOT NULL DEFAULT 'zh'
);

CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    channel TEXT NOT NULL,  -- world / guild / party
    sender_id TEXT,
    sender_name TEXT,
    body TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mobs_zone ON mobs(zone);
CREATE INDEX IF NOT EXISTS idx_inventory_player ON inventory(player_id);
CREATE INDEX IF NOT EXISTS idx_combat_ts ON combat_log(ts);
CREATE INDEX IF NOT EXISTS idx_chat_ts ON chat_log(ts);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def reset_all() -> None:
    """Drop the DB file. Used by bootstrap."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [row_to_dict(r) for r in rows]


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def jload(s: str | None) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None
