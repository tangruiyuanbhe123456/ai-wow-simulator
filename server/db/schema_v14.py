"""V14 schema additions:
  1. player_qc_wallets  -- one row per human player, QC balance + lifetime stats
  2. qc_ledger          -- append-only ledger of every QC movement (audit-grade)
  3. death_outbox       -- buffer populated by SQLite trigger when HP=0
  4. SQLite trigger on players UPDATE -- writes death_outbox when HP transitions to <=0
"""
from __future__ import annotations

V14_SCHEMA_SQL = """
-- Q Coin wallet (one per human player) --------------------------------------
CREATE TABLE IF NOT EXISTS player_qc_wallets (
    pid             TEXT PRIMARY KEY,           -- player_id (FK to players)
    balance         INTEGER DEFAULT 0,          -- current spendable QC
    lifetime_topup  INTEGER DEFAULT 0,          -- total QC ever topped up
    lifetime_spent  INTEGER DEFAULT 0,          -- total QC ever spent (resurrect + flowers)
    lifetime_gifted_out INTEGER DEFAULT 0,      -- total QC sent as gifts
    lifetime_gifted_in  INTEGER DEFAULT 0,      -- total QC received as gifts (after 20% fee)
    kyc_status      TEXT DEFAULT 'none',        -- none | pending | verified | failed
    kyc_triggered_at REAL,
    last_active     REAL NOT NULL,
    FOREIGN KEY (pid) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_qc_wallet_balance ON player_qc_wallets(balance);

-- QC Ledger: append-only, every movement recorded ---------------------------
CREATE TABLE IF NOT EXISTS qc_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    from_pid        TEXT,                       -- NULL for top-ups
    to_pid          TEXT,                       -- NULL for spend-on-bot
    amount          INTEGER NOT NULL,           -- always positive
    direction       TEXT NOT NULL,              -- topup | gift_sent | gift_received | gift_fee | resurrect | flower
    ref_pid         TEXT,                       -- bot pid for resurrect/flower; gift recipient for gift_received
    balance_after   INTEGER,                    -- recipient's balance after this op (NULL for fees)
    order_id        TEXT,                       -- PayPal/payment processor order id (topups)
    note            TEXT,
    FOREIGN KEY (from_pid) REFERENCES players(id),
    FOREIGN KEY (to_pid) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_qc_ledger_from_ts ON qc_ledger(from_pid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_qc_ledger_to_ts ON qc_ledger(to_pid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_qc_ledger_ts ON qc_ledger(ts DESC);

-- Death outbox: SQLite trigger writes here; Python polls and processes --------
CREATE TABLE IF NOT EXISTS death_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pid             TEXT NOT NULL,
    zone            TEXT,
    pos_x           INTEGER,
    pos_y           INTEGER,
    last_damage_by  TEXT,                       -- last attacker pid (best-effort, set by app code)
    last_damage_by_name TEXT,
    hp_before       INTEGER,                    -- HP at time of death (always <= 0)
    created_at      REAL NOT NULL,
    processed_at    REAL,
    process_status  TEXT DEFAULT 'pending',     -- pending | processed | failed | skipped
    process_error   TEXT,
    lifecycle_death_no INTEGER,                 -- populated after on_death runs
    FOREIGN KEY (pid) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_death_outbox_pending
    ON death_outbox(process_status, created_at);

-- SQLite trigger: any HP transition to <=0 from >0 writes outbox ----------
CREATE TRIGGER IF NOT EXISTS trg_player_hp_zero
AFTER UPDATE OF hp ON players
WHEN NEW.hp <= 0 AND OLD.hp > 0
BEGIN
    INSERT INTO death_outbox (pid, zone, pos_x, pos_y, hp_before, created_at)
    VALUES (NEW.id, NEW.zone, NEW.pos_x, NEW.pos_y, NEW.hp, CAST(strftime('%s','now') AS REAL));
END;
"""


def ensure_v14_schema(conn) -> None:
    conn.executescript(V14_SCHEMA_SQL)
    conn.commit()
