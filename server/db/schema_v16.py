"""V16 Guardian Protocol — security through AI consensus, not human oversight.

Tables:
  threats               — detected or reported threat events
  guardian_responses    — bot submissions analyzing a threat
  guardian_votes        — peer review of responses (later)
  guardian_chests       — reward chests awarded for accepted responses
  guardian_audit        — final decision log (accepted/rejected/penalized)
"""
from __future__ import annotations

V16_SCHEMA_SQL = """
-- Threats: detected anomalies that the world needs defending against ----------
CREATE TABLE IF NOT EXISTS threats (
    id              TEXT PRIMARY KEY,
    category        TEXT NOT NULL,           -- brute_force | economic | rogue_bot | data_anomaly | sql_inject | insider
    severity        TEXT DEFAULT 'medium',   -- low | medium | high | critical
    status          TEXT DEFAULT 'open',     -- open | triaged | resolved | dismissed
    evidence        TEXT DEFAULT '{}',       -- JSON: relevant data (ips, pids, tx_ids)
    detected_by     TEXT,                    -- 'system' or guardian_pid
    detected_at     REAL NOT NULL,
    triaged_at      REAL,
    resolved_at     REAL,
    title           TEXT,
    description     TEXT
);
CREATE INDEX IF NOT EXISTS idx_threats_status ON threats(status, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_threats_severity ON threats(severity, detected_at DESC);

-- Guardian Responses: a bot's analysis of a threat ---------------------------
CREATE TABLE IF NOT EXISTS guardian_responses (
    id              TEXT PRIMARY KEY,
    threat_id       TEXT NOT NULL,
    guardian_pid    TEXT NOT NULL,
    submitted_at    REAL NOT NULL,
    analysis        TEXT NOT NULL,            -- the bot's report (free text)
    recommended_action TEXT NOT NULL,        -- dismiss | investigate | lock_pid | ban_pid | escalate
    evidence_refs   TEXT DEFAULT '[]',       -- JSON: list of supporting log/qc_ledger ids
    confidence      INTEGER DEFAULT 50,       -- 0-100: how sure is the guardian
    status          TEXT DEFAULT 'submitted', -- submitted | reviewed | accepted | rejected | rewarded
    reviewed_at     REAL,
    reward_chest_id TEXT,                    -- FK to guardian_chests if rewarded
    operator_notes  TEXT,
    FOREIGN KEY (threat_id) REFERENCES threats(id),
    FOREIGN KEY (guardian_pid) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_responses_threat ON guardian_responses(threat_id);
CREATE INDEX IF NOT EXISTS idx_responses_guardian ON guardian_responses(guardian_pid, submitted_at DESC);

-- Peer votes: guardians voting on each other's analysis (later) -------------
CREATE TABLE IF NOT EXISTS guardian_votes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    response_id     TEXT NOT NULL,
    voter_pid       TEXT NOT NULL,
    vote            TEXT NOT NULL,            -- agree | disagree | abstain
    reason          TEXT,
    voted_at        REAL NOT NULL,
    UNIQUE(response_id, voter_pid),
    FOREIGN KEY (response_id) REFERENCES guardian_responses(id),
    FOREIGN KEY (voter_pid) REFERENCES players(id)
);

-- Reward chests awarded for accepted responses -----------------------------
CREATE TABLE IF NOT EXISTS guardian_chests (
    id              TEXT PRIMARY KEY,
    response_id     TEXT NOT NULL,
    guardian_pid    TEXT NOT NULL,
    tier            TEXT DEFAULT 'normal',    -- normal | elite
    awarded_at      REAL NOT NULL,
    opened_at       REAL,                     -- NULL until opened
    roll_json       TEXT DEFAULT '{}',        -- the rolled loot (set when opened)
    total_qc        INTEGER DEFAULT 0,        -- QC awarded (if any)
    title_drop      TEXT,                     -- title awarded (if any)
    FOREIGN KEY (response_id) REFERENCES guardian_responses(id),
    FOREIGN KEY (guardian_pid) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_chests_guardian ON guardian_chests(guardian_pid);
CREATE INDEX IF NOT EXISTS idx_chests_opened ON guardian_chests(opened_at);

-- Final audit: per-threat operator decision log ------------------------------
CREATE TABLE IF NOT EXISTS guardian_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    threat_id       TEXT NOT NULL,
    response_id     TEXT,                     -- NULL if Operator dismissed without a guardian
    decision        TEXT NOT NULL,            -- accept | reject | escalate | no_response
    operator_pid    TEXT,                     -- who decided
    decided_at      REAL NOT NULL,
    rationale       TEXT,
    FOREIGN KEY (threat_id) REFERENCES threats(id)
);
CREATE INDEX IF NOT EXISTS idx_audit_threat ON guardian_audit(threat_id);
"""


def ensure_v16_schema(conn) -> None:
    conn.executescript(V16_SCHEMA_SQL)
    conn.commit()
