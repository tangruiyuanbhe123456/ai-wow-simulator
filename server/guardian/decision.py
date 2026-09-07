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

    # V17: bridge to digital life layer — write memory + biography chapter
    try:
        # Pull threat info for richer memory text
        cur_t = conn.execute(
            "SELECT title, severity, category FROM threats WHERE id=?",
            (threat_id,),
        )
        t_row = cur_t.fetchone()
        threat_title    = t_row[0] if t_row else threat_id
        threat_severity = t_row[1] if t_row else "unknown"
        threat_category = t_row[2] if t_row else "unknown"

        from server.guardian.lifecycle import on_response_accepted
        life = on_response_accepted(
            conn,
            guardian_pid=guardian_pid,
            response_id=response_id,
            threat_id=threat_id,
            threat_title=threat_title,
            threat_severity=threat_severity,
            threat_category=threat_category,
            recommended_action="see chest loot",  # we don't store it on response row
            confidence=70,
            operator_notes=notes,
        )
    except Exception as e:
        # Don't fail the accept if bridge errors — log and continue
        log = __import__("logging").getLogger("wow")
        log.warning("v17 lifecycle bridge failed: %s", e)
        life = {"error": str(e)}

    return {
        "response_id": response_id, "status": "accepted",
        "chest": chest, "operator_pid": operator_pid,
        "life_layer": life,
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
