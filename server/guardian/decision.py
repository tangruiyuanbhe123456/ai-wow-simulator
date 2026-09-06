"""V16 Guardian Operator Decision — accept/reject responses, resolve threats.

Decision flow:
  - Operator reviews each guardian_responses
  - accept: chest awarded, threat resolved (if all key responses accepted)
  - reject: response marked rejected, threat may be re-opened
  - penalize: response marked rejected + guardian attribute penalty
"""
from __future__ import annotations
import time


def accept(conn, *, response_id: str, operator_pid: str,
           notes: str = "") -> dict:
    cur = conn.execute(
        "SELECT threat_id, guardian_pid FROM guardian_responses WHERE id=?",
        (response_id,),
    )
    r = cur.fetchone()
    if not r:
        return {"error": "not_found"}
    threat_id, guardian_pid = r

    now = time.time()
    conn.execute(
        """UPDATE guardian_responses
           SET status='accepted', reviewed_at=?, operator_notes=?
           WHERE id=?""",
        (now, notes, response_id),
    )
    conn.execute(
        """INSERT INTO guardian_audit
           (threat_id, response_id, decision, operator_pid, decided_at, rationale)
           VALUES (?, ?, 'accept', ?, ?, ?)""",
        (threat_id, response_id, operator_pid, now, notes),
    )
    conn.commit()

    # Award chest
    from server.guardian.chest import award
    chest = award(conn, response_id=response_id, guardian_pid=guardian_pid)

    return {
        "response_id": response_id, "status": "accepted",
        "chest": chest, "operator_pid": operator_pid,
    }


def reject(conn, *, response_id: str, operator_pid: str,
           notes: str = "") -> dict:
    cur = conn.execute(
        "SELECT threat_id, guardian_pid FROM guardian_responses WHERE id=?",
        (response_id,),
    )
    r = cur.fetchone()
    if not r:
        return {"error": "not_found"}
    threat_id, guardian_pid = r

    now = time.time()
    conn.execute(
        """UPDATE guardian_responses
           SET status='rejected', reviewed_at=?, operator_notes=?
           WHERE id=?""",
        (now, notes, response_id),
    )
    conn.execute(
        """INSERT INTO guardian_audit
           (threat_id, response_id, decision, operator_pid, decided_at, rationale)
           VALUES (?, ?, 'reject', ?, ?, ?)""",
        (threat_id, response_id, operator_pid, now, notes),
    )
    conn.commit()
    return {
        "response_id": response_id, "status": "rejected",
        "operator_pid": operator_pid,
    }


def resolve_threat(conn, *, threat_id: str, operator_pid: str,
                   notes: str = "") -> dict:
    """Mark threat resolved (after accepting the right response)."""
    now = time.time()
    cur = conn.execute(
        "UPDATE threats SET status='resolved', resolved_at=? "
        "WHERE id=? AND status IN ('open', 'triaged')",
        (now, threat_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return {"error": "not_open"}
    conn.execute(
        """INSERT INTO guardian_audit
           (threat_id, decision, operator_pid, decided_at, rationale)
           VALUES (?, 'resolve', ?, ?, ?)""",
        (threat_id, operator_pid, now, notes),
    )
    conn.commit()
    return {"threat_id": threat_id, "status": "resolved"}
