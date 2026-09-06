"""V16 Threat Engine — detect anomalies + broadcast guardian calls.

Detection sources:
  1. security_log aggregation (rate_limit hits, login_failures, etc.)
  2. qc_ledger anomaly detection (large transfers, irregular patterns)
  3. bot_wallet decay check (bots that should earn but don't)
  4. explicit reports from any player/bot via API

When a threat is opened, it broadcasts a 'guardian call' that any alive bot
can respond to.
"""
from __future__ import annotations
import json
import secrets
import time
from typing import Optional


# ---- Threat categories & severity defaults --------------------------------

THREAT_TEMPLATES = {
    "brute_force": {
        "severity": "high",
        "title": "Suspicious login activity",
        "description": "Multiple failed login attempts from one or more IPs.",
    },
    "economic": {
        "severity": "critical",
        "title": "Anomalous QC movement",
        "description": "Unusual QC transfer pattern detected.",
    },
    "rogue_bot": {
        "severity": "medium",
        "title": "Bot behaviour anomaly",
        "description": "A bot is behaving outside expected parameters.",
    },
    "data_anomaly": {
        "severity": "high",
        "title": "Database inconsistency",
        "description": "Schema or data integrity violation.",
    },
    "sql_inject": {
        "severity": "high",
        "title": "Possible SQL injection",
        "description": "Malicious query pattern detected at API boundary.",
    },
    "insider": {
        "severity": "critical",
        "title": "Suspicious operator action",
        "description": "An internal action outside normal parameters.",
    },
}


def open_threat(conn, *, category: str, evidence: dict,
                 severity: Optional[str] = None,
                 detected_by: str = "system",
                 title: Optional[str] = None,
                 description: Optional[str] = None) -> dict:
    """Open a new threat. Returns the threat record."""
    if category not in THREAT_TEMPLATES:
        return {"error": "unknown_category"}
    tmpl = THREAT_TEMPLATES[category]
    sev = severity or tmpl["severity"]
    ttl = title or tmpl["title"]
    desc = description or tmpl["description"]
    tid = "thr-" + secrets.token_hex(6)
    now = time.time()
    conn.execute(
        """INSERT INTO threats
           (id, category, severity, status, evidence, detected_by,
            detected_at, title, description)
           VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)""",
        (tid, category, sev, json.dumps(evidence, ensure_ascii=False),
         detected_by, now, ttl, desc),
    )
    conn.commit()
    return {
        "id": tid, "category": category, "severity": sev,
        "status": "open", "title": ttl, "description": desc,
        "detected_at": now, "evidence": evidence,
    }


def list_threats(conn, status: Optional[str] = None,
                 limit: int = 50) -> list:
    cur = conn.execute(
        "SELECT id, category, severity, status, title, detected_at FROM threats "
        + ("WHERE status=? " if status else "")
        + "ORDER BY detected_at DESC LIMIT ?",
        ((status, limit) if status else (limit,)),
    )
    return [
        {"id": r[0], "category": r[1], "severity": r[2],
         "status": r[3], "title": r[4], "detected_at": r[5]}
        for r in cur.fetchall()
    ]


def get_threat(conn, tid: str) -> Optional[dict]:
    cur = conn.execute(
        """SELECT id, category, severity, status, evidence, detected_by,
                  detected_at, triaged_at, resolved_at, title, description
           FROM threats WHERE id=?""",
        (tid,),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {
        "id": r[0], "category": r[1], "severity": r[2], "status": r[3],
        "evidence": json.loads(r[4] or "{}"), "detected_by": r[5],
        "detected_at": r[6], "triaged_at": r[7], "resolved_at": r[8],
        "title": r[9], "description": r[10],
    }


def resolve_threat(conn, tid: str) -> dict:
    """Mark a threat as resolved (after accepted guardian response)."""
    cur = conn.execute(
        "UPDATE threats SET status='resolved', resolved_at=? WHERE id=?",
        (time.time(), tid),
    )
    conn.commit()
    if cur.rowcount == 0:
        return {"error": "not_found"}
    return {"id": tid, "status": "resolved"}


def detect_brute_force(conn, window_sec: int = 900, threshold: int = 10) -> list:
    """Scan login_failures for IPs with excessive failures."""
    cutoff = time.time() - window_sec
    cur = conn.execute(
        """SELECT ip, COUNT(*) as n, MIN(ts) as first, MAX(ts) as last
           FROM login_failures WHERE ts >= ?
           GROUP BY ip HAVING n >= ?
           ORDER BY n DESC""",
        (cutoff, threshold),
    )
    findings = []
    for ip, n, first, last in cur.fetchall():
        findings.append({
            "type": "brute_force", "ip": ip, "failures": n,
            "window_start": first, "window_end": last,
            "severity": "high" if n >= 20 else "medium",
        })
    return findings


def detect_economic_anomaly(conn, window_sec: int = 3600, threshold_qc: int = 5000) -> list:
    """Scan qc_ledger for unusually large transfers."""
    cutoff = time.time() - window_sec
    cur = conn.execute(
        """SELECT id, from_pid, to_pid, amount, direction, ts
           FROM qc_ledger WHERE amount >= ? AND ts >= ?
           ORDER BY amount DESC LIMIT 50""",
        (threshold_qc, cutoff),
    )
    findings = []
    for r in cur.fetchall():
        findings.append({
            "type": "economic", "tx_id": r[0], "from_pid": r[1], "to_pid": r[2],
            "amount": r[3], "direction": r[4], "ts": r[5],
            "severity": "critical" if r[3] >= 20000 else "high",
        })
    return findings


def scan_and_open_threats(conn) -> list:
    """Run all detectors. For each new finding, open a threat if not already open."""
    opened = []
    for f in detect_brute_force(conn):
        # Check if already open
        cur = conn.execute(
            "SELECT id FROM threats WHERE category='brute_force' AND status='open' "
            "AND evidence LIKE ?",
            ('%"ip": "' + f["ip"] + '"%',),
        )
        if not cur.fetchone():
            t = open_threat(conn, category="brute_force",
                            evidence=f, severity=f["severity"])
            opened.append(t)
    for f in detect_economic_anomaly(conn):
        cur = conn.execute(
            "SELECT id FROM threats WHERE category='economic' AND status='open' "
            "AND evidence LIKE ?",
            ('%"tx_id": ' + str(f["tx_id"]) + '%',),
        )
        if not cur.fetchone():
            t = open_threat(conn, category="economic",
                            evidence=f, severity=f["severity"])
            opened.append(t)
    return opened
