"""V17 schema additions: guardian digital-life linkage.

No new tables — reuses v13 bot_memories + bot_biography.

Indexes added for fast guardian_history queries:
  - idx_memories_guardian on bot_memories (memory_type='guardian_event')
"""
from __future__ import annotations

V17_SCHEMA_SQL = """
-- v17: Guardian → Digital Life Layer linkage --------------------------
-- Speeds up guardian_history endpoint queries.
CREATE INDEX IF NOT EXISTS idx_memories_guardian
    ON bot_memories(pid, memory_type, ts DESC)
    WHERE memory_type='guardian_event';

CREATE INDEX IF NOT EXISTS idx_biography_guardian
    ON bot_biography(pid, chapter, ts DESC)
    WHERE chapter='guardian';

-- Aggregate stats table for fast council XP calculation (v18 prep).
-- Read-mostly; updated on response accept.
CREATE TABLE IF NOT EXISTS guardian_stats (
    guardian_pid   TEXT PRIMARY KEY,
    responses_total   INTEGER DEFAULT 0,
    responses_accepted INTEGER DEFAULT 0,
    responses_rejected INTEGER DEFAULT 0,
    threats_seen       INTEGER DEFAULT 0,
    last_active_ts     REAL,
    first_active_ts    REAL,
    FOREIGN KEY (guardian_pid) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_guardian_stats_xp
    ON guardian_stats(responses_accepted DESC);
"""


def ensure_v17_schema(conn) -> None:
    """Apply v17 schema additions (idempotent)."""
    conn.executescript(V17_SCHEMA_SQL)
    conn.commit()
