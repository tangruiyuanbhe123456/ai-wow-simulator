"""V18 Guardian Council — elite tier promotion, voting, fund distribution.

Council mechanics:
  - Any bot that reaches silver (5 accepted) is auto-promoted to council
  - Tier drives vote weight: bronze=1, silver=3, gold=10, elite=30
  - Members can open proposals; any council member can vote
  - 5% of platform QC revenue goes into council_fund
  - Fund is split among elite members weekly (manual trigger via API)
  - Demotion happens if a member's accepted count drops below tier threshold
"""
from __future__ import annotations
import time
import secrets
from typing import Optional


# Tier thresholds (accepted responses) and vote weights
TIER_THRESHOLDS = [
    ("bronze", 1,   1),
    ("silver", 5,   3),
    ("gold",   20,  10),
    ("elite",  50,  30),
]


def compute_tier(accepted: int) -> tuple[str, int]:
    """Map cumulative accepted-response count → (tier, vote_weight)."""
    tier, weight = "none", 0
    for t, thresh, w in TIER_THRESHOLDS:
        if accepted >= thresh:
            tier, weight = t, w
    return tier, weight


def _get_accepted(conn, pid: str) -> int:
    cur = conn.execute(
        "SELECT responses_accepted FROM guardian_stats WHERE guardian_pid=?",
        (pid,),
    )
    r = cur.fetchone()
    return (r[0] if r else 0) or 0


def sync_member(conn, pid: str) -> dict:
    """Idempotent: bring council row in sync with current accepted count.

    - If not in council and tier >= bronze → add to council
    - If in council and tier changed → update tier + weight
    - If accepted count drops below 1 (rejected spam) → remove from council
    """
    accepted = _get_accepted(conn, pid)
    tier, weight = compute_tier(accepted)

    cur = conn.execute("SELECT tier, vote_weight FROM guardian_council WHERE pid=?", (pid,))
    existing = cur.fetchone()

    if tier == "none":
        # Below bronze — demote if present
        if existing:
            conn.execute("DELETE FROM guardian_council WHERE pid=?", (pid,))
            conn.commit()
            return {"pid": pid, "action": "demoted_none",
                    "old_tier": existing[0], "new_tier": "none"}
        return {"pid": pid, "action": "no_change", "tier": "none"}

    now = time.time()
    if not existing:
        conn.execute(
            """INSERT INTO guardian_council
                 (pid, tier, vote_weight, elected_at, budget_share,
                  proposals_made, votes_cast)
               VALUES (?, ?, ?, ?, 0, 0, 0)""",
            (pid, tier, weight, now),
        )
        conn.commit()
        return {"pid": pid, "action": "elected", "tier": tier,
                "vote_weight": weight}

    if existing[0] != tier:
        conn.execute(
            """UPDATE guardian_council
               SET tier=?, vote_weight=?
               WHERE pid=?""",
            (tier, weight, pid),
        )
        conn.commit()
        return {"pid": pid, "action": "tier_changed",
                "old_tier": existing[0], "new_tier": tier,
                "vote_weight": weight}

    return {"pid": pid, "action": "no_change",
            "tier": tier, "vote_weight": weight}


def list_council(conn, tier: Optional[str] = None, limit: int = 100) -> list:
    cur = conn.execute(
        """SELECT pid, tier, vote_weight, elected_at, budget_share,
                  proposals_made, votes_cast
           FROM guardian_council"""
        + (" WHERE tier=?" if tier else "")
        + " ORDER BY vote_weight DESC, elected_at ASC LIMIT ?",
        ((tier, limit) if tier else (limit,)),
    )
    return [
        {"pid": r[0], "tier": r[1], "vote_weight": r[2],
         "elected_at": r[3], "budget_share": r[4] or 0,
         "proposals_made": r[5] or 0, "votes_cast": r[6] or 0}
        for r in cur.fetchall()
    ]


def open_proposal(conn, *, proposer_pid: str, proposal_type: str,
                  title: str, body: str = "",
                  options: list = None,
                  target_id: Optional[str] = None,
                  duration_hours: int = 72) -> dict:
    """A council member opens a proposal.

    options: list of dicts, each with 'key', 'label_zh', 'label_en'
             defaults to a generic 'for/against/abstain' slate.
    """
    # Verify proposer is council member
    cur = conn.execute(
        "SELECT tier, vote_weight FROM guardian_council WHERE pid=?",
        (proposer_pid,),
    )
    row = cur.fetchone()
    if not row:
        return {"error": "not_council_member"}

    if options is None or len(options) < 2:
        options = [
            {"key": "for",     "label_zh": "支持", "label_en": "For"},
            {"key": "against", "label_zh": "反对", "label_en": "Against"},
            {"key": "abstain", "label_zh": "弃权", "label_en": "Abstain"},
        ]

    import json as _json
    pid = "prop-" + secrets.token_hex(6)
    now = time.time()
    closes = now + duration_hours * 3600
    conn.execute(
        """INSERT INTO council_proposals
             (id, pid_proposer, proposal_type, target_id, title, body,
              options_json, status, opened_at, closes_at, result_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, '{}')""",
        (pid, proposer_pid, proposal_type, target_id, title, body,
         _json.dumps(options, ensure_ascii=False), now, closes),
    )
    conn.execute(
        "UPDATE guardian_council SET proposals_made = proposals_made + 1 WHERE pid=?",
        (proposer_pid,),
    )
    conn.commit()
    return {
        "proposal_id": pid, "proposer_pid": proposer_pid,
        "proposal_type": proposal_type, "options": options,
        "opened_at": now, "closes_at": closes, "status": "open",
    }


def cast_vote(conn, *, proposal_id: str, voter_pid: str,
              vote_option: str, rationale: str = "") -> dict:
    """Cast a weighted vote. Idempotent — re-voting updates the same row."""
    # Verify proposal is open
    cur = conn.execute(
        "SELECT options_json, status, closes_at FROM council_proposals WHERE id=?",
        (proposal_id,),
    )
    row = cur.fetchone()
    if not row:
        return {"error": "proposal_not_found"}
    if row[1] != "open":
        return {"error": "proposal_closed", "status": row[1]}
    if time.time() > row[2]:
        return {"error": "proposal_expired"}

    import json as _json
    options = _json.loads(row[0] or "[]")
    valid_keys = {o["key"] for o in options}
    if vote_option not in valid_keys:
        return {"error": "invalid_option", "valid": sorted(valid_keys)}

    # Verify voter is council member with weight
    cur = conn.execute(
        "SELECT tier, vote_weight FROM guardian_council WHERE pid=?",
        (voter_pid,),
    )
    vrow = cur.fetchone()
    if not vrow:
        return {"error": "voter_not_council_member"}
    weight = vrow[1]

    now = time.time()
    cur = conn.execute(
        """SELECT id FROM council_votes
           WHERE proposal_id=? AND voter_pid=?""",
        (proposal_id, voter_pid),
    )
    if cur.fetchone():
        # Update existing vote
        conn.execute(
            """UPDATE council_votes
               SET vote_option=?, vote_weight=?, voted_at=?, rationale=?
               WHERE proposal_id=? AND voter_pid=?""",
            (vote_option, weight, now, rationale, proposal_id, voter_pid),
        )
        action = "updated"
    else:
        conn.execute(
            """INSERT INTO council_votes
                 (proposal_id, voter_pid, vote_option, vote_weight, voted_at, rationale)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (proposal_id, voter_pid, vote_option, weight, now, rationale),
        )
        conn.execute(
            "UPDATE guardian_council SET votes_cast = votes_cast + 1 WHERE pid=?",
            (voter_pid,),
        )
        action = "cast"
    conn.commit()
    return {
        "proposal_id": proposal_id, "voter_pid": voter_pid,
        "vote_option": vote_option, "vote_weight": weight,
        "action": action,
    }


def tally_proposal(conn, proposal_id: str) -> dict:
    """Compute tally; resolve if past closes_at."""
    import json as _json
    cur = conn.execute(
        "SELECT options_json, status, closes_at FROM council_proposals WHERE id=?",
        (proposal_id,),
    )
    row = cur.fetchone()
    if not row:
        return {"error": "proposal_not_found"}
    options_json, status, closes_at = row
    options = _json.loads(options_json or "[]")

    cur = conn.execute(
        "SELECT vote_option, vote_weight FROM council_votes WHERE proposal_id=?",
        (proposal_id,),
    )
    tally = {o["key"]: 0 for o in options}
    total_weight = 0
    for opt, wt in cur.fetchall():
        if opt in tally:
            tally[opt] += wt
        total_weight += wt

    winner = None
    if tally:
        winner = max(tally, key=lambda k: tally[k])

    result = {"tally": tally, "total_weight": total_weight, "winner": winner}

    # Auto-resolve if past closes_at
    now = time.time()
    if status == "open" and now >= closes_at:
        new_status = "passed" if winner not in (None, "against") else "rejected"
        conn.execute(
            """UPDATE council_proposals
               SET status=?, resolved_at=?, result_json=?
               WHERE id=?""",
            (new_status, now, _json.dumps(result, ensure_ascii=False), proposal_id),
        )
        conn.commit()
        result["status"] = new_status
    else:
        result["status"] = status

    return {"proposal_id": proposal_id, **result}


def list_proposals(conn, status: Optional[str] = None, limit: int = 50) -> list:
    cur = conn.execute(
        """SELECT id, pid_proposer, proposal_type, target_id, title,
                  body, options_json, status, opened_at, closes_at,
                  resolved_at, result_json
           FROM council_proposals"""
        + (" WHERE status=?" if status else "")
        + " ORDER BY opened_at DESC LIMIT ?",
        ((status, limit) if status else (limit,)),
    )
    import json as _json
    out = []
    for r in cur.fetchall():
        out.append({
            "proposal_id": r[0], "proposer_pid": r[1], "proposal_type": r[2],
            "target_id": r[3], "title": r[4], "body": r[5],
            "options": _json.loads(r[6] or "[]"),
            "status": r[7], "opened_at": r[8], "closes_at": r[9],
            "resolved_at": r[10],
            "result": _json.loads(r[11] or "{}"),
        })
    return out


# --- Council Fund -----------------------------------------------------------

def contribute_to_fund(conn, qc_amount: int, reason: str = "") -> dict:
    """Platform-side: route 5% of revenue into the council fund."""
    if qc_amount <= 0:
        return {"error": "amount_must_be_positive"}
    conn.execute(
        """UPDATE council_fund
           SET balance_qc = balance_qc + ?,
               total_received = total_received + ?,
               last_updated = ?
           WHERE id=1""",
        (qc_amount, qc_amount, time.time()),
    )
    conn.commit()
    return {"contributed_qc": qc_amount, "reason": reason}


def distribute_fund(conn) -> dict:
    """Split current fund balance equally among elite council members.

    Returns the per-member payout. Idempotent per call: drains the fund.
    """
    cur = conn.execute("SELECT balance_qc FROM council_fund WHERE id=1")
    balance = cur.fetchone()[0] or 0
    if balance <= 0:
        return {"distributed": 0, "members": [], "reason": "empty_fund"}

    cur = conn.execute(
        "SELECT pid FROM guardian_council WHERE tier='elite'"
    )
    elite_pids = [r[0] for r in cur.fetchall()]
    if not elite_pids:
        return {"distributed": 0, "members": [],
                "reason": "no_elite_members"}

    per_member = balance // len(elite_pids)
    paid_total = per_member * len(elite_pids)
    now = time.time()
    for pid in elite_pids:
        conn.execute(
            """UPDATE guardian_council
               SET budget_share = budget_share + ?
               WHERE pid=?""",
            (per_member, pid),
        )
    conn.execute(
        """UPDATE council_fund
           SET balance_qc = balance_qc - ?,
               total_paid_out = total_paid_out + ?,
               last_updated = ?
           WHERE id=1""",
        (paid_total, paid_total, now),
    )
    conn.commit()
    return {
        "distributed": paid_total,
        "per_member": per_member,
        "members": elite_pids,
    }


def get_fund(conn) -> dict:
    cur = conn.execute(
        "SELECT balance_qc, total_received, total_paid_out, last_updated FROM council_fund WHERE id=1"
    )
    r = cur.fetchone()
    return {
        "balance_qc": r[0] or 0,
        "total_received": r[1] or 0,
        "total_paid_out": r[2] or 0,
        "last_updated": r[3],
    }
