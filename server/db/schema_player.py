"""V18.3 — Human player schema (separate from bot `players` table).

Why a new table:
  - `players` holds *bots* (NPC combatants in the arena)
  - `human_players` holds real humans who top up, gift, and observe
  - They are disjoint identities: a human owns many bots, never the other way

Security / KYC:
  - phone is unique, regex-validated at API boundary (^1[3-9]\\d{9}$)
  - password_hash via security.hash_password (Argon2 preferred, SHA256 fallback)
  - kyc_status: none | pending | verified
  - total_recharged_30d_cny tracked for KYC trigger (>=¥1000 / 30d)
  - age_confirmed must be true at register (18+ gate)
"""
from __future__ import annotations

PLAYER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS human_players (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    phone               TEXT UNIQUE NOT NULL,
    password_hash       TEXT NOT NULL,
    display_name        TEXT,
    age_confirmed       INTEGER DEFAULT 0,
    kyc_status          TEXT DEFAULT 'none',     -- none|pending|verified
    total_recharged_30d_cny REAL DEFAULT 0.0,    -- rolling 30d sum, KYC trigger
    total_recharged_cny REAL DEFAULT 0.0,         -- lifetime
    created_at          REAL NOT NULL,
    last_seen           REAL
);
CREATE INDEX IF NOT EXISTS idx_human_phone ON human_players(phone);
CREATE INDEX IF NOT EXISTS idx_human_kyc ON human_players(kyc_status);
"""


def ensure_player_schema(conn) -> None:
    """Idempotent: safe to call at app startup."""
    conn.executescript(PLAYER_SCHEMA_SQL)
    conn.commit()