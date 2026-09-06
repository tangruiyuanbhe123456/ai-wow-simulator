"""V16 Guardian Protocol end-to-end demo.

Tests:
  1. Open a threat (brute_force / economic)
  2. Bot submits a guardian response (analysis + recommended action)
  3. Operator accepts the response -> chest awarded to bot
  4. Bot opens the chest -> loot rolled (QC / equipment / memory / skin)
  5. Operator rejects a bad response (no reward)
  6. Threat resolved after acceptance
  7. Detector scan finds existing anomalies and opens new threats
  8. Multiple guardians can submit for one threat
  9. Sleeping bot CANNOT respond (must be alive)
 10. Stats endpoint shows guardian history
"""
from __future__ import annotations
import sys
import time

from server.db import connect
from server.db.schema_v13 import ensure_v13_schema
from server.db.schema_v14 import ensure_v14_schema
from server.db.schema_v15 import ensure_v15_schema
from server.db.schema_v16 import ensure_v16_schema
from server.lifecycle import birth
from server.wallet import topup
from server.security import record_failed_login
from server.guardian import (
    open_threat, list_threats, get_threat, resolve_threat,
    scan_and_open_threats,
)
from server.guardian.response import submit as resp_submit, list_for_threat
from server.guardian.chest import award, open_chest, stats_for
from server.guardian.decision import accept, reject


def _ensure_player(c, pid, name, cls="warrior"):
    cur = c.execute("SELECT id FROM players WHERE id=?", (pid,))
    if cur.fetchone():
        return
    now = time.time()
    c.execute(
        "INSERT INTO players (id, name, cls, level, hp, hp_max, mp, mp_max, atk, defn, zone, created_at, last_seen) "
        "VALUES (?,?,?,1,100,100,50,50,10,5,'wild_plains',?,?)",
        (pid, name, cls, now, now),
    )
    c.commit()


def main():
    print("=" * 60)
    print("V16 Guardian Protocol End-to-End Demo")
    print("=" * 60)

    c = connect()
    ensure_v13_schema(c)
    ensure_v14_schema(c)
    ensure_v15_schema(c)
    ensure_v16_schema(c)

    # Clean
    c.executescript("""
        DELETE FROM guardian_audit;
        DELETE FROM guardian_chests;
        DELETE FROM guardian_votes;
        DELETE FROM guardian_responses;
        DELETE FROM threats;
        DELETE FROM login_failures WHERE pid LIKE 'g16_%';
        DELETE FROM qc_ledger WHERE from_pid LIKE 'g16_%' OR to_pid LIKE 'g16_%';
        DELETE FROM player_qc_wallets WHERE pid LIKE 'g16_%';
        DELETE FROM bot_ledger WHERE from_pid LIKE 'g16_%' OR to_pid LIKE 'g16_%';
        DELETE FROM bot_wallets WHERE pid LIKE 'g16_%';
        DELETE FROM bot_identity WHERE pid LIKE 'g16_%';
        DELETE FROM bot_memorial_flowers;
        DELETE FROM bot_memorials WHERE pid LIKE 'g16_%';
        DELETE FROM bot_relations WHERE pid_a LIKE 'g16_%' OR pid_b LIKE 'g16_%';
        DELETE FROM bot_biography WHERE pid LIKE 'g16_%';
        DELETE FROM bot_memories WHERE pid LIKE 'g16_%';
        DELETE FROM bot_lifecycle WHERE pid LIKE 'g16_%';
        DELETE FROM players WHERE id LIKE 'g16_%';
    """)
    c.commit()

    # Create guardian bots (alive) + 1 sleeping bot
    _ensure_player(c, "g16_alice", "GuardianAlice")
    _ensure_player(c, "g16_bob", "GuardianBob")
    _ensure_player(c, "g16_eve", "GuardianEve")
    _ensure_player(c, "g16_dead", "DeadBot")
    birth(c, "g16_alice", "GuardianAlice")
    birth(c, "g16_bob", "GuardianBob")
    birth(c, "g16_eve", "GuardianEve")
    birth(c, "g16_dead", "DeadBot")
    # Make g16_dead sleeping
    c.execute(
        "UPDATE players SET hp=0 WHERE id='g16_dead'"
    )
    c.commit()
    from server.death_dispatcher import process_pending
    process_pending(c, limit=5)

    # Give alice some QC to verify chest payouts
    topup(c, "g16_alice", 0)  # ensure wallet exists

    # ============================================================
    # [1] Open a brute_force threat manually
    # ============================================================
    print("\n[1] Open a brute_force threat")
    t = open_threat(c, category="brute_force",
                    evidence={"ip": "5.5.5.5", "failures": 25},
                    detected_by="system")
    print("  threat id: " + t["id"])
    print("  severity: " + t["severity"])
    assert t["severity"] == "high"
    tid = t["id"]

    # ============================================================
    # [2] Two guardians respond
    # ============================================================
    print("\n[2] Guardians respond")
    r1 = resp_submit(c, threat_id=tid, guardian_pid="g16_alice",
                     analysis="I noticed 25 failed logins from 5.5.5.5 within 15 minutes. "
                              "This matches a credential stuffing pattern. Recommend ban_ip.",
                     recommended_action="ban_pid",
                     confidence=85)
    print("  Alice: " + r1["id"])
    assert r1["status"] == "submitted"

    r2 = resp_submit(c, threat_id=tid, guardian_pid="g16_bob",
                     analysis="Same IP, also seen login_failures earlier. "
                              "Suggest lock the IPs that tried.",
                     recommended_action="lock_pid",
                     confidence=70)
    print("  Bob:   " + r2["id"])

    # ============================================================
    # [3] Sleeping bot CANNOT respond
    # ============================================================
    print("\n[3] Sleeping bot rejected from responding")
    r3 = resp_submit(c, threat_id=tid, guardian_pid="g16_dead",
                     analysis="I am dead, can I still help? " * 3,
                     recommended_action="investigate",
                     confidence=50)
    print("  DeadBot: " + str(r3))
    assert r3["error"] == "guardian_not_alive"

    # ============================================================
    # [4] Eve submits a bad analysis (Operator rejects)
    # ============================================================
    print("\n[4] Eve submits a weak response")
    r4 = resp_submit(c, threat_id=tid, guardian_pid="g16_eve",
                     analysis="Not sure, might be nothing. Investigate?",
                     recommended_action="investigate",
                     confidence=20)
    print("  Eve: " + r4["id"])
    eve_resp_id = r4["id"]

    # ============================================================
    # [5] Operator accepts Alice's response -> chest
    # ============================================================
    print("\n[5] Operator accepts Alice's response -> chest")
    a = accept(c, response_id=r1["id"], operator_pid="operator:human",
               notes="Excellent analysis, matches log evidence")
    print("  accept result: status=" + a["status"] + " chest_id=" + a["chest"]["id"])
    chest_id = a["chest"]["id"]
    assert a["status"] == "accepted"

    # ============================================================
    # [6] Alice opens the chest -> loot rolled
    # ============================================================
    print("\n[6] Alice opens the chest")
    op = open_chest(c, chest_id, "g16_alice")
    print("  loot: " + str(op["loot"]))
    assert op["opened_at"] is not None
    assert "kind" in op["loot"]

    # ============================================================
    # [7] Operator rejects Eve's weak response
    # ============================================================
    print("\n[7] Operator rejects Eve's weak response")
    rj = reject(c, response_id=eve_resp_id, operator_pid="operator:human",
                notes="Too vague, no actionable recommendation")
    print("  reject: " + str(rj))
    assert rj["status"] == "rejected"

    # ============================================================
    # [8] Resolve the threat
    # ============================================================
    print("\n[8] Resolve threat")
    res = resolve_threat(c, tid)
    print("  resolve: " + str(res))
    assert res["status"] == "resolved"

    # ============================================================
    # [9] Detector scan finds existing anomalies
    # ============================================================
    print("\n[9] Detector scan")
    # Generate brute_force evidence
    for i in range(12):
        record_failed_login(c, "g16_alice", "9.9.9.9", "ua", "wrong_password")
    opened = scan_and_open_threats(c)
    print("  opened: " + str(len(opened)) + " new threats")
    for t in opened:
        print("    " + str(t["id"]) + " category=" + t["category"] + " severity=" + t["severity"])
    assert len(opened) >= 1

    # ============================================================
    # [10] Guardian stats
    # ============================================================
    print("\n[10] Guardian stats for Alice")
    stats = stats_for(c, "g16_alice")
    print("  " + str(stats))
    assert stats["chests_awarded"] >= 1

    # ============================================================
    # [11] API endpoints work
    # ============================================================
    print("\n[11] API endpoint smoke test")
    from fastapi.testclient import TestClient
    from server.main import app
    client = TestClient(app)
    r = client.get("/api/v1/guardian/threats")
    print("  GET /guardian/threats: " + str(r.status_code))
    assert r.status_code == 200
    r = client.post("/api/v1/guardian/scan")
    print("  POST /guardian/scan: " + str(r.status_code))
    assert r.status_code == 200
    r = client.get("/api/v1/guardian/guardians/g16_alice/stats")
    print("  GET /guardian/guardians/g16_alice/stats: " + str(r.status_code))
    assert r.status_code == 200
    print("  stats payload: " + str(r.json()))

    print("\n" + "=" * 60)
    print("ALL V16 ASSERTIONS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("\nFAIL: " + str(e), file=sys.stderr)
        sys.exit(1)
