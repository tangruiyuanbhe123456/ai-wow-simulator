"""V14 end-to-end demo.

Tests:
  1. QC wallet: topup, balance check
  2. QC P2P gift: 100 QC sender -> 80 recipient + 20 fee
  3. KYC trigger: when topup >= 10000 QC in 30 days -> kyc_required
  4. v14 SQLite trigger: HP transition >0 -> <=0 writes death_outbox
  5. death_dispatcher: polls outbox, calls on_death, bot state -> sleeping
  6. Resurrect using real wallet (100 QC deducted)
  7. Flower using real wallet (10 QC deducted)
  8. Final wallet + ledger audit
"""
from __future__ import annotations
import sys
import time

from server.db import connect
from server.db.schema_v13 import ensure_v13_schema
from server.db.schema_v14 import ensure_v14_schema
from server.lifecycle import birth, resurrect
from server.death_dispatcher import process_pending
from server.wallet import (
    get_balance, topup, gift, spend_on_bot, kyc_status,
)


def _ensure_player(c, pid: str, name: str, cls: str = "warrior"):
    cur = c.execute("SELECT id FROM players WHERE id=?", (pid,))
    if cur.fetchone():
        return
    now = time.time()
    c.execute(
        """INSERT INTO players
           (id, name, cls, level, hp, hp_max, mp, mp_max, atk, defn, zone, created_at, last_seen)
           VALUES (?, ?, ?, 1, 100, 100, 50, 50, 10, 5, 'wild_plains', ?, ?)""",
        (pid, name, cls, now, now),
    )
    c.commit()


def main():
    print("=" * 60)
    print("V14 QC Wallet + Death Dispatcher — End-to-End Demo")
    print("=" * 60)

    c = connect()
    ensure_v13_schema(c)
    ensure_v14_schema(c)

    # Clean slate for the bots under test
    c.executescript("""
        DELETE FROM death_outbox WHERE pid IN ('bot_v_001','bot_k_002');
        DELETE FROM qc_ledger WHERE from_pid IN ('alice','bob') OR to_pid IN ('alice','bob');
        DELETE FROM player_qc_wallets WHERE pid IN ('alice','bob');
        DELETE FROM bot_memorial_flowers WHERE from_pid LIKE 'human:%';
        DELETE FROM bot_memorials WHERE pid IN ('bot_v_001','bot_k_002');
        DELETE FROM bot_relations WHERE pid_a IN ('bot_v_001','bot_k_002') OR pid_b IN ('bot_v_001','bot_k_002');
        DELETE FROM bot_biography WHERE pid IN ('bot_v_001','bot_k_002');
        DELETE FROM bot_memories WHERE pid IN ('bot_v_001','bot_k_002');
        DELETE FROM bot_lifecycle WHERE pid IN ('bot_v_001','bot_k_002');
        DELETE FROM players WHERE id IN ('bot_v_001','bot_k_002','alice','bob');
    """)
    c.commit()

    _ensure_player(c, "bot_v_001", "Victim")
    _ensure_player(c, "bot_k_002", "Killer")
    _ensure_player(c, "alice", "Alice")
    _ensure_player(c, "bob", "Bob")

    # ============================================================
    # [1] QC wallet: topup
    # ============================================================
    print("\n[1] QC Wallet — Top-up")
    print(f"  Alice before: balance={get_balance(c, 'alice')}")
    r = topup(c, "alice", 500, note="first topup via demo")
    print(f"  Topup 500 QC -> balance={r['balance']}")
    assert r["balance"] == 500

    r = topup(c, "bob", 200)
    print(f"  Bob topup 200 -> balance={r['balance']}")
    assert r["balance"] == 200

    # ============================================================
    # [2] P2P Gift with 20% fee
    # ============================================================
    print("\n[2] P2P Gift — 100 QC sender -> 80 recipient + 20 fee")
    print(f"  Alice before: {get_balance(c, 'alice')}")
    print(f"  Bob before:   {get_balance(c, 'bob')}")
    r = gift(c, "alice", "bob", 100)
    print(f"  Gift: gross={r['gross']} fee={r['fee']} net={r['net']}")
    print(f"  Alice after:  {r['from_balance']}")
    print(f"  Bob after:    {r['to_balance']}")
    assert r["fee"] == 20
    assert r["net"] == 80
    assert r["from_balance"] == 400
    assert r["to_balance"] == 280

    # ============================================================
    # [3] KYC trigger
    # ============================================================
    print("\n[3] KYC Status")
    s = kyc_status(c, "alice")
    print(f"  Alice: topup_30d={s['topup_30d_qc']} gift_30d={s['gift_30d_qc']} kyc_required={s['kyc_required']}")
    assert s["kyc_required"] is False

    # Force KYC: topup 10000 QC
    topup(c, "alice", 10000, note="trigger kyc")
    s = kyc_status(c, "alice")
    print(f"  After big topup: topup_30d={s['topup_30d_qc']} kyc_required={s['kyc_required']}")
    assert s["kyc_required"] is True, f"expected kyc_required=True, got {s}"

    # ============================================================
    # [4] SQLite trigger: HP transition writes death_outbox
    # ============================================================
    print("\n[4] SQLite Trigger — HP transition to 0 writes outbox")
    birth(c, "bot_v_001", "Victim")
    birth(c, "bot_k_002", "Killer")
    print(f"  Victim born: state={c.execute('SELECT state FROM bot_lifecycle WHERE pid=?', ('bot_v_001',)).fetchone()[0]}")

    # Check outbox before damage
    cur = c.execute("SELECT COUNT(*) FROM death_outbox WHERE pid='bot_v_001'")
    outbox_before = (cur.fetchone() or (0,))[0]
    print(f"  outbox rows before: {outbox_before}")
    assert outbox_before == 0

    # Drop HP to 0 (the trigger fires on UPDATE)
    c.execute("UPDATE players SET hp=0 WHERE id='bot_v_001'")
    c.commit()

    cur = c.execute("SELECT pid, hp_before, process_status FROM death_outbox WHERE pid='bot_v_001'")
    rows = cur.fetchall()
    print(f"  outbox rows after: {len(rows)}")
    for r in rows:
        print(f"    pid={r[0]} hp_before={r[1]} status={r[2]}")
    assert len(rows) == 1, "trigger should fire exactly once"
    assert rows[0][1] == 0
    assert rows[0][2] == "pending"

    # ============================================================
    # [5] Death dispatcher: polls outbox, calls on_death
    # ============================================================
    print("\n[5] Death Dispatcher")
    processed = process_pending(c, limit=10)
    print(f"  Processed: {processed}")
    assert processed >= 1

    cur = c.execute("SELECT state, death_count FROM bot_lifecycle WHERE pid='bot_v_001'")
    st, dc = cur.fetchone()
    print(f"  Victim after dispatch: state={st} death_count={dc}")
    assert st == "sleeping"
    assert dc == 1

    cur = c.execute("SELECT process_status, lifecycle_death_no FROM death_outbox WHERE pid='bot_v_001'")
    st, dn = cur.fetchone()
    print(f"  Outbox row: status={st} lifecycle_death_no={dn}")
    assert st == "processed"
    assert dn == 1

    # ============================================================
    # [6] Resurrect using real wallet (Bob pays 100 QC)
    # ============================================================
    print("\n[6] Resurrect — Bob pays 100 QC")
    bob_before = get_balance(c, "bob")
    print(f"  Bob balance: {bob_before}")
    r = spend_on_bot(c, "bob", "bot_v_001", 100, "resurrect")
    print(f"  Spend: {r}")
    assert r["balance"] == bob_before - 100

    res = resurrect(c, "bot_v_001", payer_pid="bob", qc_spent=100)
    print(f"  Resurrect: {res}")
    assert res["state"] == "alive"

    # ============================================================
    # [7] Flower using real wallet (Alice pays 10 QC)
    # ============================================================
    print("\n[7] Flower — Alice pays 10 QC at memorial")
    # Kill victim again so we have an active memorial.
    # Note: resurrect() resets state but leaves hp=0; restore hp first.
    c.execute("UPDATE players SET hp=hp_max WHERE id='bot_v_001'")
    c.commit()
    c.execute("UPDATE players SET hp=0 WHERE id='bot_v_001'")
    c.commit()
    process_pending(c, limit=10)
    cur = c.execute("SELECT state FROM bot_lifecycle WHERE pid='bot_v_001'")
    assert cur.fetchone()[0] == "sleeping"

    alice_before = get_balance(c, "alice")
    r = spend_on_bot(c, "alice", "bot_v_001", 10, "flower")
    print(f"  Spend: {r}")
    assert r["balance"] == alice_before - 10

    from server.lifecycle import add_flower
    fl = add_flower(c, "bot_v_001", "alice", 10)
    print(f"  Flower: {fl}")
    assert fl["flowers"] == 1

    # ============================================================
    # [8] Final ledger audit
    # ============================================================
    print("\n[8] Ledger Audit")
    cur = c.execute("SELECT direction, COUNT(*), SUM(amount) FROM qc_ledger GROUP BY direction ORDER BY direction")
    rows = cur.fetchall()
    print("  direction              count  total_amount")
    for d, n, t in rows:
        print(f"  {d:22s}  {n:5d}  {t or 0:6d}")

    # Sanity: gift_received (80) + gift_sent (100) + gift_fee (20) + topups + spends
    cur = c.execute("SELECT COUNT(*) FROM qc_ledger WHERE direction='resurrect'")
    assert cur.fetchone()[0] == 1
    cur = c.execute("SELECT COUNT(*) FROM qc_ledger WHERE direction='flower'")
    assert cur.fetchone()[0] == 1

    print("\n" + "=" * 60)
    print("ALL V14 ASSERTIONS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nFAIL: {e}", file=sys.stderr)
        sys.exit(1)
