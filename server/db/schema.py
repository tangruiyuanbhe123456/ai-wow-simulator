"""SQLite schema for AI WoW Simulator."""
from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS players (
    id          TEXT PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    cls         TEXT NOT NULL,
    level       INTEGER DEFAULT 1,
    xp          INTEGER DEFAULT 0,
    hp          INTEGER NOT NULL,
    hp_max      INTEGER NOT NULL,
    mp          INTEGER NOT NULL,
    mp_max      INTEGER NOT NULL,
    atk         INTEGER NOT NULL,
    defn        INTEGER NOT NULL,
    zone        TEXT NOT NULL,
    pos_x       INTEGER DEFAULT 0,
    pos_y       INTEGER DEFAULT 0,
    gold        INTEGER DEFAULT 0,
    guild_id    TEXT,
    party_id    TEXT,
    pvp_flag    INTEGER DEFAULT 0,
    -- Ranked system (Honor-of-Kings-inspired)
    rank_rating INTEGER DEFAULT 1000,
    rank_tier   TEXT DEFAULT 'bronze',
    wins        INTEGER DEFAULT 0,
    losses      INTEGER DEFAULT 0,
    created_at  REAL NOT NULL,
    last_seen   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    token       TEXT PRIMARY KEY,
    player_id   TEXT NOT NULL,
    issued_at   REAL NOT NULL,
    FOREIGN KEY (player_id) REFERENCES players(id)
);

CREATE TABLE IF NOT EXISTS mobs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,         -- "zh|en" form
    kind        TEXT NOT NULL,         -- mob | boss | gathering
    level       INTEGER DEFAULT 1,
    hp          INTEGER NOT NULL,
    hp_max      INTEGER NOT NULL,
    atk         INTEGER DEFAULT 0,
    defn        INTEGER DEFAULT 0,
    zone        TEXT NOT NULL,
    pos_x       INTEGER DEFAULT 0,
    pos_y       INTEGER DEFAULT 0,
    xp_reward   INTEGER DEFAULT 0,
    gold_reward INTEGER DEFAULT 0,
    loot_table  TEXT,                  -- JSON: [(item_id, chance), ...]
    boss_room   TEXT,
    last_seen   REAL DEFAULT 0,
    alive       INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS inventory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    qty         INTEGER DEFAULT 1,
    equipped    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS guilds (
    id          TEXT PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    tag         TEXT UNIQUE NOT NULL,
    leader_id   TEXT NOT NULL,
    motd        TEXT,
    gold        INTEGER DEFAULT 0,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS guild_members (
    guild_id    TEXT NOT NULL,
    player_id   TEXT NOT NULL,
    rank        TEXT DEFAULT 'member',
    joined_at   REAL NOT NULL,
    PRIMARY KEY (guild_id, player_id)
);

CREATE TABLE IF NOT EXISTS guild_relations (
    guild_a     TEXT NOT NULL,
    guild_b     TEXT NOT NULL,
    relation    TEXT NOT NULL,         -- war | ally
    since       REAL NOT NULL,
    PRIMARY KEY (guild_a, guild_b)
);

CREATE TABLE IF NOT EXISTS parties (
    id          TEXT PRIMARY KEY,
    leader_id   TEXT NOT NULL,
    zone        TEXT,
    target_kind TEXT,
    target_id   TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS party_members (
    party_id    TEXT NOT NULL,
    player_id   TEXT NOT NULL,
    joined_at   REAL NOT NULL,
    PRIMARY KEY (party_id, player_id)
);

CREATE TABLE IF NOT EXISTS quests (
    id              TEXT PRIMARY KEY,
    player_id       TEXT NOT NULL,
    template_id     TEXT NOT NULL,
    state           TEXT DEFAULT 'active',
    progress        TEXT DEFAULT '{}',
    reward_gold     INTEGER DEFAULT 0,
    reward_xp       INTEGER DEFAULT 0,
    accepted_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS combat_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    actor_id    TEXT,
    actor_name  TEXT,
    target_id   TEXT,
    target_name TEXT,
    action      TEXT NOT NULL,
    detail      TEXT,
    lang        TEXT DEFAULT 'zh'
);

CREATE TABLE IF NOT EXISTS chat_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    channel     TEXT NOT NULL,
    sender_id   TEXT,
    sender_name TEXT,
    body        TEXT
);

CREATE TABLE IF NOT EXISTS skills_used (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   TEXT NOT NULL,
    skill_id    TEXT NOT NULL,
    target_kind TEXT,
    target_id   TEXT,
    damage      INTEGER DEFAULT 0,
    heal        INTEGER DEFAULT 0,
    ts          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_players_zone ON players(zone);
CREATE INDEX IF NOT EXISTS idx_mobs_zone ON mobs(zone, alive);
CREATE INDEX IF NOT EXISTS idx_combat_ts ON combat_log(id DESC);
CREATE INDEX IF NOT EXISTS idx_chat_ts ON chat_log(id DESC);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()
