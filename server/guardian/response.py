"""V16 Guardian Response — a bot submits analysis of a threat.

Constraints:
  - Only ALIVE bots can respond (dead/sleeping bots are out of the fight)
  - One response per (threat, guardian) pair (idempotent)
  - Response includes analysis text + recommended_action + evidence refs
"""
from __future__ import annotations
import secrets
import time
import json


VALID_ACTIONS = {"dismiss", "investigate", "lock_pid", "ban_pid", "escalate"}


def submit(conn, *, threat_id: str, guardian_pid: str,
           analysis: str, recommended_action: str,
           evidence_refs: list = None,
           confidence: int = 50) -> dict:
    """A guardian bot submits their analysis."""
    if recommended_action not in VALID_ACTIONS:
        return {"error": "invalid_action", "valid": sorted(VALID_ACTIONS)}
    if not analysis or len(analysis.strip()) < 10:
        return {"error": "analysis_too_short"}
    confidence = max(0, min(100, int(confidence)))

    # Verify threat exists and is open
    cur = conn.execute(
        "SELECT status FROM threats WHERE id=?", (threat_id,),
    )
    r = cur.fetchone()
    if not r:
        return {"error": "threat_not_found"}
    if r[0] not in ("open", "triaged"):
        return {"error": "threat_closed", "status": r[0]}

    # Verify guardian bot is alive
    cur = conn.execute(
        "SELECT state FROM bot_lifecycle WHERE pid=?", (guardian_pid,),
    )
    lc = cur.fetchone()
    if not lc:
        return {"error": "no_lifecycle"}
    if lc[0] != "alive":
        return {"error": "guardian_not_alive", "state": lc[0]}

    # Idempotent: same guardian + same threat = update existing
    cur = conn.execute(
        "SELECT id FROM guardian_responses WHERE threat_id=? AND guardian_pid=?",
        (threat_id, guardian_pid),
    )
    existing = cur.fetchone()
    rid = existing[0] if existing else "resp-" + secrets.token_hex(6)
    now = time.time()

    if existing:
        conn.execute(
            """UPDATE guardian_responses SET
               analysis=?, recommended_action=?, evidence_refs=?,
               confidence=?, submitted_at=?, status='submitted'
               WHERE id=?""",
            (analysis, recommended_action,
             json.dumps(evidence_refs or [], ensure_ascii=False),
             confidence, now, rid),
        )
    else:
        conn.execute(
            """INSERT INTO guardian_responses
               (id, threat_id, guardian_pid, submitted_at, analysis,
                recommended_action, evidence_refs, confidence, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'submitted')""",
            (rid, threat_id, guardian_pid, now, analysis,
             recommended_action,
             json.dumps(evidence_refs or [], ensure_ascii=False),
             confidence),
        )

    # Mark threat triaged once we have at least 1 response
    conn.execute(
        "UPDATE threats SET triaged_at=? WHERE id=? AND triaged_at IS NULL",
        (now, threat_id),
    )
    conn.commit()
    return {
        "id": rid, "threat_id": threat_id, "guardian_pid": guardian_pid,
        "submitted_at": now, "confidence": confidence,
        "status": "submitted", "duplicate_updated": bool(existing),
    }


def list_for_threat(conn, threat_id: str) -> list:
    cur = conn.execute(
        """SELECT id, guardian_pid, submitted_at, recommended_action,
                  confidence, status, reward_chest_id
           FROM guardian_responses WHERE threat_id=?
           ORDER BY submitted_at DESC""",
        (threat_id,),
    )
    return [
        {"id": r[0], "guardian_pid": r[1], "submitted_at": r[2],
         "recommended_action": r[3], "confidence": r[4],
         "status": r[5], "reward_chest_id": r[6]}
        for r in cur.fetchall()
    ]


def get(conn, response_id: str) -> dict:
    cur = conn.execute(
        """SELECT id, threat_id, guardian_pid, submitted_at, analysis,
                  recommended_action, evidence_refs, confidence, status,
                  reviewed_at, reward_chest_id, operator_notes
           FROM guardian_responses WHERE id=?""",
        (response_id,),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {
        "id": r[0], "threat_id": r[1], "guardian_pid": r[2],
        "submitted_at": r[3], "analysis": r[4],
        "recommended_action": r[5],
        "evidence_refs": json.loads(r[6] or "[]"),
        "confidence": r[7], "status": r[8],
        "reviewed_at": r[9], "reward_chest_id": r[10],
        "operator_notes": r[11],
    }
