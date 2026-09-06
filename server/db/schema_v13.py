"""V13 schema additions: lifecycle, memories, biography, relations, memorials.

Appended to SCHEMA_SQL in server/db/schema.py via ensure_v13_schema().
"""
from __future__ import annotations

V13_SCHEMA_SQL = """
-- v13: Digital Life Layer ----------------------------------------

CREATE TABLE IF NOT EXISTS bot_lifecycle (
    pid         TEXT PRIMARY KEY,
    state       TEXT DEFAULT 'alive',
    born_at     REAL NOT NULL,
    died_at     REAL,
    death_count INTEGER DEFAULT 0,
    soul_name   TEXT,
    dna_seed    TEXT,
    soul_json   TEXT DEFAULT '{}',
    FOREIGN KEY (pid) REFERENCES players(id)
);

CREATE TABLE IF NOT EXISTS bot_memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pid         TEXT NOT NULL,
    ts          REAL NOT NULL,
    memory_type TEXT,
    weight      INTEGER DEFAULT 50,
    actor_pid   TEXT,
    title_zh    TEXT,
    title_en    TEXT,
    body_zh     TEXT,
    body_en     TEXT,
    FOREIGN KEY (pid) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_memories_pid_ts ON bot_memories(pid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_memories_pid_weight ON bot_memories(pid, weight DESC);

CREATE TABLE IF NOT EXISTS bot_biography (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pid         TEXT NOT NULL,
    ts          REAL NOT NULL,
    chapter     TEXT,
    title_zh    TEXT,
    title_en    TEXT,
    body_zh     TEXT,
    body_en     TEXT,
    FOREIGN KEY (pid) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_biography_pid_ts ON bot_biography(pid, ts DESC);

CREATE TABLE IF NOT EXISTS bot_relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pid_a       TEXT NOT NULL,
    pid_b       TEXT NOT NULL,
    relation    TEXT NOT NULL,
    since       REAL NOT NULL,
    note        TEXT,
    UNIQUE(pid_a, pid_b, relation)
);
CREATE INDEX IF NOT EXISTS idx_relations_a ON bot_relations(pid_a);
CREATE INDEX IF NOT EXISTS idx_relations_b ON bot_relations(pid_b);

CREATE TABLE IF NOT EXISTS bot_memorials (
    pid         TEXT PRIMARY KEY,
    death_no    INTEGER NOT NULL,
    ts          REAL NOT NULL,
    zone        TEXT,
    pos_x       INTEGER,
    pos_y       INTEGER,
    flowers     INTEGER DEFAULT 0,
    epitaph_zh  TEXT,
    epitaph_en  TEXT,
    FOREIGN KEY (pid) REFERENCES players(id)
);

CREATE TABLE IF NOT EXISTS bot_memorial_flowers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    memorial_pid TEXT NOT NULL,
    from_pid     TEXT NOT NULL,
    qc_amount    INTEGER NOT NULL,
    ts           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memorial_flowers_pid ON bot_memorial_flowers(memorial_pid, ts DESC);
"""


def ensure_v13_schema(conn) -> None:
    """Apply V13 schema additions idempotently."""
    conn.executescript(V13_SCHEMA_SQL)
    conn.commit()
