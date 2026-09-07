"""V18 schema additions: Guardian Council — elite tier + voting.

Tables:
  guardian_council     — elected members per tier + tenure + budget share
  council_proposals    — proposals opened by members (threat disposition / governance)
  council_votes        — per-member weighted votes on proposals
  council_fund         — single-row ledger for the 5% platform-QC council fund
"""
from __future__ import annotations

V18_SCHEMA_SQL = """
-- v18: Guardian Council ----------------------------------------

-- Elected council members. Promotion is tier-driven and immediate;
-- demotion happens if accepted-count falls below tier threshold.
CREATE TABLE IF NOT EXISTS guardian_council (
    pid              TEXT PRIMARY KEY,
    tier             TEXT NOT NULL,           -- bronze|silver|gold|elite
    vote_weight      INTEGER NOT NULL,        -- 1|3|10|30
    elected_at       REAL NOT NULL,
    tenure_until     REAL,                    -- NULL = lifetime (until demoted)
    budget_share     INTEGER DEFAULT 0,       -- total QC received from council fund
    proposals_made   INTEGER DEFAULT 0,
    votes_cast       INTEGER DEFAULT 0,
    FOREIGN KEY (pid) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_council_tier ON guardian_council(tier, vote_weight DESC);

-- Proposals: open items for the council to vote on. Two types:
--   'threat_disposition' — disposition decision on a specific threat
--   'governance'         — meta decisions (e.g. raise tier thresholds)
CREATE TABLE IF NOT EXISTS council_proposals (
    id              TEXT PRIMARY KEY,
    pid_proposer    TEXT NOT NULL,
    proposal_type   TEXT NOT NULL,
    target_id       TEXT,                    -- e.g. threat_id if threat_disposition
    title           TEXT NOT NULL,
    body            TEXT,
    options_json    TEXT NOT NULL,           -- JSON list of {key, label_zh, label_en}
    status          TEXT DEFAULT 'open',     -- open | passed | rejected | expired
    opened_at       REAL NOT NULL,
    closes_at       REAL NOT NULL,
    resolved_at     REAL,
    result_json     TEXT DEFAULT '{}',       -- JSON: {tally: {opt: weight}, winner: opt}
    FOREIGN KEY (pid_proposer) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON council_proposals(status, closes_at);

-- Per-member weighted vote on a proposal. Idempotent on (proposal, voter).
CREATE TABLE IF NOT EXISTS council_votes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id     TEXT NOT NULL,
    voter_pid       TEXT NOT NULL,
    vote_option     TEXT NOT NULL,           -- must be one of proposal.options
    vote_weight     INTEGER NOT NULL,        -- snapshot of voter's weight at vote time
    voted_at        REAL NOT NULL,
    rationale       TEXT,
    UNIQUE(proposal_id, voter_pid),
    FOREIGN KEY (proposal_id) REFERENCES council_proposals(id),
    FOREIGN KEY (voter_pid) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_votes_proposal ON council_votes(proposal_id, vote_weight DESC);

-- Single-row ledger for the council fund (5% of platform QC revenue).
-- Funded by wallet/topup events; distributed to elite council members.
CREATE TABLE IF NOT EXISTS council_fund (
    id              INTEGER PRIMARY KEY CHECK (id = 1),  -- enforce singleton
    balance_qc      INTEGER DEFAULT 0,
    total_received  INTEGER DEFAULT 0,
    total_paid_out  INTEGER DEFAULT 0,
    last_updated    REAL
);
INSERT OR IGNORE INTO council_fund (id, balance_qc, total_received, total_paid_out, last_updated)
VALUES (1, 0, 0, 0, NULL);
"""


def ensure_v18_schema(conn) -> None:
    """Apply v18 schema additions (idempotent)."""
    conn.executescript(V18_SCHEMA_SQL)
    conn.commit()
