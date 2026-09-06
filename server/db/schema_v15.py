"""V15 schema additions."""
from __future__ import annotations

V15_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bot_identity (
    pid TEXT PRIMARY KEY,
    did TEXT UNIQUE NOT NULL,
    fingerprint TEXT NOT NULL,
    public_key TEXT,
    signature TEXT,
    issued_at REAL NOT NULL,
    expires_at REAL,
    revoked INTEGER DEFAULT 0,
    revoked_reason TEXT,
    FOREIGN KEY (pid) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_bot_identity_did ON bot_identity(did);

CREATE TABLE IF NOT EXISTS bot_wallets (
    pid TEXT PRIMARY KEY,
    balance INTEGER DEFAULT 0,
    lifetime_earned INTEGER DEFAULT 0,
    lifetime_received INTEGER DEFAULT 0,
    lifetime_bequested INTEGER DEFAULT 0,
    last_active REAL NOT NULL,
    FOREIGN KEY (pid) REFERENCES players(id)
);

CREATE TABLE IF NOT EXISTS bot_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    from_pid TEXT,
    to_pid TEXT,
    amount INTEGER NOT NULL,
    direction TEXT NOT NULL,
    ref_pid TEXT,
    balance_after INTEGER,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_bot_ledger_ts ON bot_ledger(ts DESC);
CREATE INDEX IF NOT EXISTS idx_bot_ledger_to_pid ON bot_ledger(to_pid, ts DESC);

CREATE TABLE IF NOT EXISTS security_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor_ip TEXT,
    actor_pid TEXT,
    action TEXT NOT NULL,
    severity TEXT DEFAULT 'info',
    payload TEXT DEFAULT '{}',
    request_id TEXT,
    success INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_security_log_ts ON security_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_security_log_actor_pid ON security_log(actor_pid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_security_log_severity ON security_log(severity, ts DESC);

CREATE TABLE IF NOT EXISTS rate_limits (
    actor_pid TEXT NOT NULL,
    action TEXT NOT NULL,
    window_start REAL NOT NULL,
    count INTEGER DEFAULT 0,
    PRIMARY KEY (actor_pid, action)
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    pid TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_used REAL NOT NULL,
    ip TEXT,
    user_agent TEXT,
    revoked INTEGER DEFAULT 0,
    FOREIGN KEY (pid) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_pid ON sessions(pid);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS api_keys (
    key_hash TEXT PRIMARY KEY,
    pid TEXT NOT NULL,
    label TEXT,
    scopes TEXT DEFAULT 'read,play',
    issued_at REAL NOT NULL,
    last_used REAL,
    expires_at REAL,
    revoked INTEGER DEFAULT 0,
    FOREIGN KEY (pid) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_api_keys_pid ON api_keys(pid);

CREATE TABLE IF NOT EXISTS login_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    pid TEXT,
    ip TEXT NOT NULL,
    user_agent TEXT,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_login_failures_pid_ts ON login_failures(pid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_login_failures_ip_ts ON login_failures(ip, ts DESC);

CREATE TABLE IF NOT EXISTS operator_keys (
    key_id TEXT PRIMARY KEY,
    algorithm TEXT NOT NULL,
    public_key TEXT NOT NULL,
    private_key TEXT,
    created_at REAL NOT NULL,
    active INTEGER DEFAULT 1
);
"""


def ensure_v15_schema(conn) -> None:
    conn.executescript(V15_SCHEMA_SQL)
    conn.commit()
