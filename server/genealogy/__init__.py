"""V13 Genealogy: relations between bots."""
from __future__ import annotations
import time


_VALID = {"mentor", "parent", "rival", "ally", "sworn"}


def add(conn, pid_a: str, pid_b: str, relation: str, note: str = "") -> dict:
    """Add a relation. Idempotent."""
    if relation not in _VALID:
        return {"error": f"invalid_relation:{relation}", "valid": sorted(_VALID)}
    symmetric = relation in ("rival", "ally", "sworn")
    now = time.time()
    inserted = []
    for a, b in ([(pid_a, pid_b)] + ([(pid_b, pid_a)] if symmetric else [])):
        try:
            conn.execute(
                """INSERT INTO bot_relations (pid_a, pid_b, relation, since, note)
                   VALUES (?, ?, ?, ?, ?)""",
                (a, b, relation, now, note),
            )
            inserted.append((a, b))
        except Exception:
            pass  # already exists
    conn.commit()
    return {"ok": True, "relation": relation, "inserted": inserted}


def tree(conn, pid: str) -> dict:
    """Return the bot's full relation graph: mentors, rivals, allies, etc."""
    cur = conn.execute(
        """SELECT pid_b, relation, since, note FROM bot_relations
           WHERE pid_a=? ORDER BY since DESC""",
        (pid,),
    )
    out = {rel: [] for rel in _VALID}
    for r in cur.fetchall():
        rel = r[1]
        if rel in out:
            out[rel].append({"pid": r[0], "since": r[2], "note": r[3]})
    return out
