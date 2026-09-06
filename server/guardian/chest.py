"""V16 Guardian Chest — reward mechanics for accepted responses.

Tier system:
  normal: anyone who submitted an accepted response
  elite:   guardians with 10+ accepted responses (cumulative 'kills')

Loot table (normal):
  - equipment drop      40%  (placeholder)
  - QC                   30%
  - memory shard        20%
  - permanent skin      10%

Loot table (elite):
  - title "Guardian"    50%
  - rare equipment      30%
  - QC x2                20%
"""
from __future__ import annotations
import json
import secrets
import time
import random
from typing import Optional


LOOT_NORMAL = [
    ("equipment",     40),
    ("qc",            30),
    ("memory_shard",  20),
    ("permanent_skin", 10),
]
LOOT_ELITE = [
    ("title_guardian", 50),
    ("rare_equipment",  30),
    ("qc_double",       20),
]


def _tier(conn, guardian_pid: str) -> str:
    """Elite = 10+ accepted responses historically."""
    cur = conn.execute(
        """SELECT COUNT(*) FROM guardian_chests c
           JOIN guardian_responses r ON c.response_id = r.id
           WHERE c.guardian_pid=? AND r.status='accepted'""",
        (guardian_pid,),
    )
    n = cur.fetchone()[0]
    return "elite" if n >= 10 else "normal"


def _roll(tier: str) -> dict:
    table = LOOT_ELITE if tier == "elite" else LOOT_NORMAL
    total = sum(w for _, w in table)
    r = random.randint(1, total)
    cum = 0
    for kind, w in table:
        cum += w
        if r <= cum:
            return _resolve(kind, tier)
    return _resolve(table[0][0], tier)


def _resolve(kind: str, tier: str) -> dict:
    """Resolve a rolled kind into a concrete loot payload."""
    if kind == "equipment":
        return {"kind": "equipment", "item_id": "iron_sword",
                "rarity": "common", "qty": 1}
    if kind == "rare_equipment":
        return {"kind": "equipment", "item_id": "shadow_blade",
                "rarity": "rare", "qty": 1}
    if kind == "qc":
        return {"kind": "qc", "amount": random.choice([50, 100, 200, 500])}
    if kind == "qc_double":
        return {"kind": "qc", "amount": random.choice([200, 500, 1000, 2000])}
    if kind == "memory_shard":
        return {"kind": "memory_shard", "shard_type": random.choice(
            ["first_kill", "first_death", "first_resurrect", "first_rivalry"])}
    if kind == "permanent_skin":
        return {"kind": "skin", "skin_id": "shadow_form"}
    if kind == "title_guardian":
        return {"kind": "title", "title": "Guardian"}
    return {"kind": "unknown", "raw": kind}


def award(conn, *, response_id: str, guardian_pid: str) -> dict:
    """Create a chest for an accepted response. Returns chest record."""
    tier = _tier(conn, guardian_pid)
    cid = "chest-" + secrets.token_hex(6)
    now = time.time()
    conn.execute(
        """INSERT INTO guardian_chests
           (id, response_id, guardian_pid, tier, awarded_at)
           VALUES (?, ?, ?, ?, ?)""",
        (cid, response_id, guardian_pid, tier, now),
    )
    conn.execute(
        "UPDATE guardian_responses SET status='rewarded', reward_chest_id=?, reviewed_at=? "
        "WHERE id=?",
        (cid, now, response_id),
    )
    conn.commit()
    return {
        "id": cid, "response_id": response_id,
        "guardian_pid": guardian_pid, "tier": tier,
        "awarded_at": now, "opened_at": None,
    }


def open_chest(conn, chest_id: str, guardian_pid: str) -> dict:
    """Open a chest and apply the loot."""
    cur = conn.execute(
        "SELECT id, guardian_pid, tier, opened_at FROM guardian_chests WHERE id=?",
        (chest_id,),
    )
    r = cur.fetchone()
    if not r:
        return {"error": "not_found"}
    if r[1] != guardian_pid:
        return {"error": "not_owner"}
    if r[3]:
        return {"error": "already_opened"}
    tier = r[2]
    loot = _roll(tier)
    now = time.time()
    conn.execute(
        """UPDATE guardian_chests
           SET opened_at=?, roll_json=?, total_qc=?, title_drop=?
           WHERE id=?""",
        (now, json.dumps(loot, ensure_ascii=False),
         loot.get("amount") if loot.get("kind") == "qc" else 0,
         loot.get("title") if loot.get("kind") == "title" else None,
         chest_id),
    )
    conn.commit()
    # Side effects for QC
    if loot.get("kind") == "qc":
        from server.bot_wallet import earn
        earn(conn, guardian_pid, loot["amount"], "guardian_chest",
             note="from chest " + chest_id)
    return {
        "chest_id": chest_id, "tier": tier, "loot": loot,
        "opened_at": now,
    }


def stats_for(conn, guardian_pid: str) -> dict:
    cur = conn.execute(
        """SELECT tier, COUNT(*) FROM guardian_chests
           WHERE guardian_pid=? GROUP BY tier""",
        (guardian_pid,),
    )
    by_tier = dict(cur.fetchall())
    cur = conn.execute(
        "SELECT COUNT(*) FROM guardian_chests WHERE guardian_pid=? AND opened_at IS NOT NULL",
        (guardian_pid,),
    )
    opened = cur.fetchone()[0]
    return {
        "guardian_pid": guardian_pid,
        "chests_awarded": sum(by_tier.values()),
        "by_tier": by_tier,
        "chests_opened": opened,
    }
