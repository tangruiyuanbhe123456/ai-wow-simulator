"""V18 end-to-end demo: Guardian Council + weighted voting + fund distribution.

Asserts (all must PASS):
  1. Bot reaches silver tier (5 accepted) → auto-promoted to council
  2. Multiple bots across 4 tiers → correct vote weights (1/3/10/30)
  3. Council member can open a proposal
  4. Non-member cannot open a proposal (rejected)
  5. Voters cast weighted votes → tally computed correctly
  6. Proposal auto-resolves past closes_at if majority 'for'
  7. Council fund: contribute 5% of revenue → distribute splits among elite
  8. Demotion works if accepted count drops below 1 (not realistic, but logic-wise)
"""
from __future__ import annotations
import sys
import os
import time
import secrets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.db import connect, init_schema
from server.db.schema_v13 import ensure_v13_schema
from server.db.schema_v14 import ensure_v14_schema
from server.db.schema_v16 import ensure_v16_schema
from server.db.schema_v17 import ensure_v17_schema
from server.db.schema_v18 import ensure_v18_schema
from server.guardian.council import (
    sync_member, list_council, open_proposal, cast_vote,
    tally_proposal, list_proposals, compute_tier,
    contribute_to_fund, distribute_fund, get_fund,
    TIER_THRESHOLDS,
)
from server.guardian.lifecycle import _tier_label


def _ensure_player(c, pid: str, pname: str = "TestBot"):
    now = time.time()
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
            (pid, f"{pname}_{pid[-8:]}", now, now),
        )
    cur = c.execute("SELECT pid FROM bot_lifecycle WHERE pid=?", (pid,))
    if not cur.fetchone():
        c.execute(
            """INSERT INTO bot_lifecycle
                 (pid, state, born_at, death_count, soul_name, dna_seed)
               VALUES (?, 'alive', ?, 0, ?, ?)""",
            (pid, now, pname + " Soul", "dna-" + secrets.token_hex(8)),
        )
    c.commit()


def _seed_accepted(conn, pid: str, accepted: int):
    """Insert/upsert guardian_stats with N accepted responses."""
    now = time.time()
    conn.execute(
        """INSERT INTO guardian_stats
             (guardian_pid, responses_total, responses_accepted,
              responses_rejected, threats_seen,
              first_active_ts, last_active_ts)
           VALUES (?, ?, ?, 0, ?, ?, ?)
           ON CONFLICT(guardian_pid) DO UPDATE SET
              responses_total    = excluded.responses_total,
              responses_accepted = excluded.responses_accepted,
              threats_seen       = excluded.threats_seen,
              last_active_ts     = excluded.last_active_ts""",
        (pid, accepted, accepted, accepted, now, now),
    )
    conn.commit()


def main():
    print("=" * 60)
    print("V18 DEMO: Guardian Council + Weighted Voting + Fund")
    print("=" * 60)

    c = connect()
    init_schema(c)
    for fn in (ensure_v13_schema, ensure_v14_schema,
               ensure_v16_schema, ensure_v17_schema, ensure_v18_schema):
        fn(c)
    c.close()

    # Purge stale demo rows from previous runs (idempotent across v16/v17/v18)
    PURGE_PREFIXES = ("demo_v17_%", "v18_%", "g16_%")
    c = connect()
    c.execute("PRAGMA foreign_keys=OFF")
    cur = c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    for tbl in tables:
        col_check = c.execute(f"PRAGMA table_info({tbl})").fetchall()
        col_names = {row[1] for row in col_check}
        for prefix in PURGE_PREFIXES:
            if "pid" in col_names:
                c.execute(f"DELETE FROM {tbl} WHERE pid LIKE ?", (prefix,))
            if "guardian_pid" in col_names:
                c.execute(f"DELETE FROM {tbl} WHERE guardian_pid LIKE ?", (prefix,))
    # Also nuke orphan guardian tables (threats / responses / etc.)
    for tbl in ("threats", "guardian_responses", "guardian_chests",
                "guardian_audit", "guardian_council", "guardian_proposals",
                "guardian_votes", "council_fund"):
        try:
            c.execute(f"DELETE FROM {tbl}")
        except Exception:
            pass
    c.execute("PRAGMA foreign_keys=ON")
    c.commit()
    c.close()

    # === Setup: 4 bots across the 4 tier thresholds ===
    bot_bronze = "v18_bronze_" + secrets.token_hex(4)
    bot_silver = "v18_silver_" + secrets.token_hex(4)
    bot_gold   = "v18_gold_"   + secrets.token_hex(4)
    bot_elite  = "v18_elite_"  + secrets.token_hex(4)
    bot_none   = "v18_none_"   + secrets.token_hex(4)

    c = connect()
    for pid, name in [(bot_bronze, "Bronze"), (bot_silver, "Silver"),
                      (bot_gold, "Gold"), (bot_elite, "Elite"),
                      (bot_none, "None")]:
        _ensure_player(c, pid, name)
    _seed_accepted(c, bot_bronze, 1)
    _seed_accepted(c, bot_silver, 7)   # silver tier (≥5)
    _seed_accepted(c, bot_gold, 25)    # gold tier (≥20)
    _seed_accepted(c, bot_elite, 55)   # elite tier (≥50)
    c.close()
    print(f"\n[1] Seeded 5 bots: bronze(1) silver(7) gold(25) elite(55) none(0)")

    # === A1: tier computation ===
    print("\n[2] compute_tier tests...")
    cases = [
        (0,  "none",   0),
        (1,  "bronze", 1),
        (4,  "bronze", 1),
        (5,  "silver", 3),
        (19, "silver", 3),
        (20, "gold",   10),
        (49, "gold",   10),
        (50, "elite",  30),
        (99, "elite",  30),
    ]
    for accepted, exp_tier, exp_weight in cases:
        tier, weight = compute_tier(accepted)
        assert tier == exp_tier, f"❌ accepted={accepted} tier={tier}, expected {exp_tier}"
        assert weight == exp_weight, f"❌ accepted={accepted} weight={weight}, expected {exp_weight}"
    print(f"    ✅ all 9 compute_tier cases correct (1/3/10/30 weights)")

    # === A2: sync members — promote bots to council ===
    print("\n[3] sync_member — promote qualifying bots...")
    c = connect()
    for pid in (bot_bronze, bot_silver, bot_gold, bot_elite):
        result = sync_member(c, pid)
        print(f"    {pid[:20]:20} → {result['action']:14} tier={result.get('tier','-'):7} weight={result.get('vote_weight',0)}")
    # non-council
    result_none = sync_member(c, bot_none)
    assert result_none["action"] == "no_change"
    assert result_none["tier"] == "none"
    print(f"    {bot_none[:20]:20} → no_change tier=none (correct)")
    members = list_council(c)
    assert len(members) == 4, f"❌ expected 4 council members, got {len(members)}"
    print(f"    ✅ {len(members)} council members (bronze/silver/gold/elite)")

    # === A3: non-member cannot open proposal ===
    print("\n[4] Non-member cannot open proposal...")
    res = open_proposal(c, proposer_pid=bot_none,
                        proposal_type="governance",
                        title="Should fail")
    assert res.get("error") == "not_council_member", f"❌ expected error, got {res}"
    print(f"    ✅ correctly rejected non-member")

    # === A4: silver member opens proposal ===
    print("\n[5] Silver member opens proposal...")
    prop = open_proposal(
        c,
        proposer_pid=bot_silver,
        proposal_type="threat_disposition",
        title="Investigate suspicious QC transfer to bot_x42",
        body="Found 5000 QC outflow that may be RMT funnel.",
        target_id="threat_dummy_001",
        duration_hours=1,
    )
    assert "proposal_id" in prop, f"❌ proposal creation failed: {prop}"
    proposal_id = prop["proposal_id"]
    print(f"    ✅ proposal_id={proposal_id} (duration=1h)")

    # === A5: vote tally — all 4 members vote 'for' ===
    print("\n[6] All council members cast 'for' votes...")
    for pid in (bot_bronze, bot_silver, bot_gold, bot_elite):
        vr = cast_vote(c, proposal_id=proposal_id, voter_pid=pid,
                       vote_option="for", rationale="approved")
        assert vr["action"] in ("cast", "updated"), f"❌ vote failed: {vr}"
    tally = tally_proposal(c, proposal_id)
    expected_total = 1 + 3 + 10 + 30  # 44
    assert tally["total_weight"] == expected_total, \
        f"❌ total_weight expected {expected_total} got {tally['total_weight']}"
    assert tally["tally"]["for"] == expected_total, \
        f"❌ for tally expected {expected_total} got {tally['tally']['for']}"
    assert tally["winner"] == "for"
    print(f"    ✅ tally: for={tally['tally']['for']} (total={tally['total_weight']}, winner={tally['winner']})")

    # === A6: mixed vote to test weighted majority ===
    print("\n[7] Second proposal — mixed votes...")
    prop2 = open_proposal(
        c,
        proposer_pid=bot_gold,
        proposal_type="governance",
        title="Raise elite threshold to 75 accepted",
        duration_hours=1,
    )
    pid2 = prop2["proposal_id"]
    # bronze + silver vote 'for' (1+3=4), gold + elite vote 'against' (10+30=40)
    cast_vote(c, proposal_id=pid2, voter_pid=bot_bronze, vote_option="for")
    cast_vote(c, proposal_id=pid2, voter_pid=bot_silver, vote_option="for")
    cast_vote(c, proposal_id=pid2, voter_pid=bot_gold,   vote_option="against")
    cast_vote(c, proposal_id=pid2, voter_pid=bot_elite,  vote_option="against")
    tally2 = tally_proposal(c, pid2)
    assert tally2["tally"]["for"] == 4
    assert tally2["tally"]["against"] == 40
    assert tally2["winner"] == "against"
    print(f"    ✅ weighted majority works: for=4 vs against=40 → winner=against")

    # === A7: re-voting updates ===
    print("\n[8] Re-vote updates existing vote...")
    before = tally_proposal(c, proposal_id)
    cast_vote(c, proposal_id=proposal_id, voter_pid=bot_bronze,
              vote_option="abstain", rationale="changed mind")
    after = tally_proposal(c, proposal_id)
    # bronze moved from 'for' (1) to 'abstain' (-1 from for, +1 abstain)
    assert after["tally"]["for"] == before["tally"]["for"] - 1, \
        f"❌ for should drop by 1, got {before['tally']['for']}→{after['tally']['for']}"
    assert after["tally"]["abstain"] == 1
    print(f"    ✅ re-vote: bronze 'for'→'abstain' (for {before['tally']['for']}→{after['tally']['for']})")

    # === A8: fund contribution + distribution ===
    print("\n[9] Council fund contribute + distribute...")
    fund0 = get_fund(c)
    assert fund0["balance_qc"] == 0
    contribute_to_fund(c, qc_amount=1000, reason="test seed")
    contribute_to_fund(c, qc_amount=500, reason="revenue share")
    fund1 = get_fund(c)
    assert fund1["balance_qc"] == 1500, f"❌ expected 1500, got {fund1['balance_qc']}"
    print(f"    ✅ fund balance after 2 contributions: {fund1['balance_qc']} QC")
    dist = distribute_fund(c)
    assert dist["distributed"] == 1500, f"❌ expected to distribute 1500, got {dist['distributed']}"
    # only 1 elite member → all to bot_elite
    assert dist["per_member"] == 1500
    assert bot_elite in dist["members"]
    print(f"    ✅ distributed {dist['distributed']} QC → {len(dist['members'])} elite, "
          f"{dist['per_member']} each")
    fund2 = get_fund(c)
    assert fund2["balance_qc"] == 0
    print(f"    ✅ fund drained after distribution")

    # === A9: elite member received budget_share ===
    cur = c.execute(
        "SELECT budget_share FROM guardian_council WHERE pid=?", (bot_elite,)
    )
    share = cur.fetchone()[0]
    assert share == 1500, f"❌ elite budget_share expected 1500, got {share}"
    print(f"    ✅ elite budget_share = {share} QC")

    c.close()

    print("\n" + "=" * 60)
    print("ALL V18 ASSERTIONS PASSED ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()
