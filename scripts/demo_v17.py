"""V17 end-to-end demo: trigger a threat → submit response → operator accept → verify memory + biography written.

Runs without the full server. Uses the same DB the server uses (data/world.db).

Asserts (all must PASS):
  1. guardian_stats row created and counter incremented
  2. bot_memories row with memory_type='guardian_event' created
  3. bot_biography chapter with chapter='guardian' created
  4. tier label correct (bronze at first response)
  5. /api/v1/bot/{pid}/guardian_history returns the data
  6. /api/v1/guardian/leaderboard lists our bot
"""
from __future__ import annotations
import sys
import os
import time
import secrets
import json

# Allow running from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.db import connect, init_schema
from server.db.schema_v13 import ensure_v13_schema
from server.db.schema_v14 import ensure_v14_schema
from server.db.schema_v16 import ensure_v16_schema
from server.db.schema_v17 import ensure_v17_schema
from server.guardian import open_threat
from server.guardian.decision import accept
from server.guardian.response import submit as resp_submit
from server.guardian.lifecycle import on_response_accepted, get_guardian_history, _tier_label


def _ensure_player(c, pid: str, pname: str = "TestBot") -> None:
    """Make sure the bot has a players + bot_lifecycle row.

    Note: bot_identity is owned by v15 DID module — don't touch it here.
    Idempotent — safe to re-run.
    """
    now = time.time()
    # Players: name is UNIQUE, so disambiguate with pid suffix
    unique_name = f"{pname}_{pid[-8:]}"
    cur = c.execute("SELECT id FROM players WHERE id=?", (pid,))
    if not cur.fetchone():
        c.execute(
            """INSERT INTO players
                 (id, name, cls, level, xp, hp, hp_max, mp, mp_max,
                  atk, defn, zone, pos_x, pos_y, gold,
                  rank_rating, rank_tier, wins, losses,
                  created_at, last_seen)
               VALUES (?, ?, 'warrior', 1, 0, 100, 100, 50, 50,
                       10, 5, 'starter', 0, 0, 0,
                       1000, 'bronze', 0, 0, ?, ?)""",
            (pid, unique_name, now, now),
        )
    cur = c.execute("SELECT pid FROM bot_lifecycle WHERE pid=?", (pid,))
    if not cur.fetchone():
        c.execute(
            """INSERT INTO bot_lifecycle
                 (pid, state, born_at, death_count, soul_name, dna_seed)
               VALUES (?, 'alive', ?, 0, ?, ?)""",
            (pid, now, pname + "'s Soul", "dna-" + secrets.token_hex(8)),
        )
    c.commit()


def main():
    print("=" * 60)
    print("V17 DEMO: Guardian → Digital Life Layer Bridge")
    print("=" * 60)

    # Setup
    c = connect()
    init_schema(c)
    ensure_v13_schema(c)
    ensure_v14_schema(c)
    ensure_v16_schema(c)
    ensure_v17_schema(c)
    c.close()

    bot_pid = "demo_v17_" + secrets.token_hex(4)
    operator_pid = "demo_v17_op_" + secrets.token_hex(4)

    c = connect()
    _ensure_player(c, bot_pid, "DemoGuardian")
    _ensure_player(c, operator_pid, "DemoOperator")
    c.close()

    print(f"\n[1] bot_pid = {bot_pid}")
    print(f"    operator_pid = {operator_pid}")

    # Open a threat
    print("\n[2] Open a threat...")
    c = connect()
    threat = open_threat(
        c,
        category="brute_force",
        severity="high",
        title="Suspicious login burst from 10 IPs",
        description="200 failed logins in 60 seconds",
        evidence={"ip": "203.0.113.42", "failures": 200, "window_sec": 60},
        detected_by="system",
    )
    threat_id = threat["id"]
    print(f"    threat_id = {threat_id}, status = {threat['status']}")

    # Submit response
    print("\n[3] Submit guardian response...")
    c = connect()
    from server.guardian.response import submit as resp_submit
    resp = resp_submit(
        c,
        threat_id=threat_id,
        guardian_pid=bot_pid,
        analysis="Detected credential stuffing from /24 subnet. "
                 "Recommend rate-limit + temporary lockout.",
        recommended_action="lock_pid",
        evidence_refs=[{"type": "login_failures", "n": 200}],
        confidence=88,
    )
    response_id = resp["id"]
    print(f"    response_id = {response_id}, confidence = 88")

    # Operator accepts
    print("\n[4] Operator accepts response...")
    c = connect()
    accept_result = accept(
        c,
        response_id=response_id,
        operator_pid=operator_pid,
        notes="Analysis confirmed via log review. Approved.",
    )
    print(f"    accept returned: status={accept_result['status']}")
    if "life_layer" in accept_result:
        life = accept_result["life_layer"]
        print(f"    life_layer.memory_id = {life.get('memory_id')}")
        print(f"    life_layer.biography_chapter_id = {life.get('biography_chapter_id')}")
        print(f"    life_layer.stats = {life.get('stats')}")
    else:
        print(f"    life_layer MISSING: {accept_result}")

    # Assertions
    print("\n[5] Assertions...")
    c = connect()

    # A1: guardian_stats row
    cur = c.execute(
        "SELECT responses_total, responses_accepted FROM guardian_stats WHERE guardian_pid=?",
        (bot_pid,),
    )
    r = cur.fetchone()
    assert r is not None, "❌ guardian_stats row missing"
    assert r[0] == 1, f"❌ responses_total expected 1 got {r[0]}"
    assert r[1] == 1, f"❌ responses_accepted expected 1 got {r[1]}"
    print(f"    ✅ guardian_stats: total={r[0]}, accepted={r[1]}")

    # A2: bot_memories row
    cur = c.execute(
        "SELECT title_zh, body_zh, weight FROM bot_memories WHERE pid=? AND memory_type='guardian_event'",
        (bot_pid,),
    )
    r = cur.fetchone()
    assert r is not None, "❌ bot_memories guardian_event row missing"
    assert r[0] is not None and "守护事件" in r[0], f"❌ memory title wrong: {r[0]}"
    assert r[2] >= 40, f"❌ memory weight too low: {r[2]}"
    print(f"    ✅ bot_memories: title='{r[0][:30]}...', weight={r[2]}")

    # A3: bot_biography chapter
    cur = c.execute(
        "SELECT chapter, title_zh FROM bot_biography WHERE pid=? AND chapter='guardian'",
        (bot_pid,),
    )
    r = cur.fetchone()
    assert r is not None, "❌ bot_biography guardian chapter missing"
    assert "守护篇章" in r[1], f"❌ biography title wrong: {r[1]}"
    print(f"    ✅ bot_biography: chapter='{r[0]}', title='{r[1]}'")

    # A4: tier label
    tier = _tier_label(c, bot_pid)
    assert tier == "bronze", f"❌ tier expected bronze got {tier}"
    print(f"    ✅ tier = {tier} (1 accepted response)")

    # A5: get_guardian_history function
    history = get_guardian_history(c, bot_pid)
    assert len(history["memories"]) >= 1, "❌ no memories in history"
    assert len(history["biography_chapters"]) >= 1, "❌ no bio chapters"
    assert history["stats"]["tier"] == "bronze"
    print(f"    ✅ get_guardian_history: {len(history['memories'])} mem, "
          f"{len(history['biography_chapters'])} bio")

    # A6: leaderboard
    cur = c.execute(
        "SELECT guardian_pid FROM guardian_stats ORDER BY responses_accepted DESC LIMIT 5"
    )
    rows = cur.fetchall()
    assert any(r[0] == bot_pid for r in rows), "❌ bot not in leaderboard top 5"
    print(f"    ✅ leaderboard includes bot ({len(rows)} entries)")

    c.close()

    # Trigger second event to test tier-stays logic (no spam)
    print("\n[6] Trigger second response to test tier-no-spam logic...")
    c = connect()
    threat2 = open_threat(
        c,
        category="economic",
        severity="medium",
        title="Large QC outflow to single bot",
        evidence={"tx_id": 99999, "amount": 5000},
        detected_by="system",
    )
    resp2 = resp_submit(
        c,
        threat_id=threat2["id"],
        guardian_pid=bot_pid,
        analysis="Possible RMT funnel. Recommend manual review.",
        recommended_action="investigate",
        confidence=65,
    )
    accept(
        c,
        response_id=resp2["id"],
        operator_pid=operator_pid,
        notes="Forwarded to ops.",
    )
    # Bio should NOT have grown (tier didn't change, no milestone)
    cur = c.execute(
        "SELECT COUNT(*) FROM bot_biography WHERE pid=? AND chapter='guardian'",
        (bot_pid,),
    )
    bio_count = cur.fetchone()[0]
    assert bio_count == 1, f"❌ bio chapter spam: expected 1 got {bio_count}"
    print(f"    ✅ bio chapters stayed at {bio_count} (no spam)")
    c.close()

    # Trigger 5 more to hit silver tier
    print("\n[7] Simulate 5 more responses to reach silver tier...")
    for i in range(5):
        c = connect()
        t = open_threat(
            c,
            category="rogue_bot",
            severity="low",
            title=f"Test threat #{i}",
            evidence={"test": i},
            detected_by="system",
        )
        r = resp_submit(
            c,
            threat_id=t["id"],
            guardian_pid=bot_pid,
            analysis=f"Test analysis {i}",
            recommended_action="dismiss",
            confidence=70,
        )
        accept(c, response_id=r["id"], operator_pid=operator_pid, notes="OK")
        c.close()
    c = connect()
    tier2 = _tier_label(c, bot_pid)
    cur = c.execute(
        "SELECT responses_accepted FROM guardian_stats WHERE guardian_pid=?",
        (bot_pid,),
    )
    accepted_total = cur.fetchone()[0]
    assert tier2 == "silver", f"❌ expected silver got {tier2}"
    print(f"    ✅ tier upgraded to {tier2} after {accepted_total} accepted")

    # Check bio chapter grew (tier changed)
    cur = c.execute(
        "SELECT COUNT(*) FROM bot_biography WHERE pid=? AND chapter='guardian'",
        (bot_pid,),
    )
    bio2 = cur.fetchone()[0]
    assert bio2 >= 2, f"❌ expected bio to grow on tier change, got {bio2}"
    print(f"    ✅ bio chapters grew to {bio2} on tier transition")
    c.close()

    print("\n" + "=" * 60)
    print("ALL V17 ASSERTIONS PASSED ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()
