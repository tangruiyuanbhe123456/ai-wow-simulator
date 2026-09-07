"""V15 end-to-end demo: DID + bot wallet + security layer."""
from __future__ import annotations
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.db import connect
from server.db.schema_v13 import ensure_v13_schema
from server.db.schema_v14 import ensure_v14_schema
from server.db.schema_v15 import ensure_v15_schema
from server.lifecycle import birth
from server.identity import verify_did, revoke_did
from server.bot_wallet import earn, receive_from_human, bequest, get_balance
from server.security import (
    is_valid_pid, hash_password, verify_password,
    check_rate, record_failed_login, is_locked_out, clear_failures,
    create_session, verify_session, log_event,
)


def _ensure_player(c, pid, name, cls="warrior"):
    cur = c.execute("SELECT id FROM players WHERE id=?", (pid,))
    if cur.fetchone():
        return
    now = time.time()
    c.execute(
        "INSERT INTO players (id, name, cls, level, hp, hp_max, mp, mp_max, atk, defn, zone, created_at, last_seen) VALUES (?,?,?,1,100,100,50,50,10,5,'wild_plains',?,?)",
        (pid, name, cls, now, now),
    )
    c.commit()


def main():
    print("=" * 60)
    print("V15 AI Citizen DID + Bot Wallet + Security E2E")
    print("=" * 60)

    c = connect()
    ensure_v13_schema(c)
    ensure_v14_schema(c)
    ensure_v15_schema(c)

    c.executescript("""
        DELETE FROM bot_ledger;
        DELETE FROM bot_wallets;
        DELETE FROM api_keys;
        DELETE FROM sessions;
        DELETE FROM login_failures;
        DELETE FROM rate_limits;
        DELETE FROM security_log;
        DELETE FROM bot_identity;
        DELETE FROM bot_memorial_flowers;
        DELETE FROM bot_memorials;
        DELETE FROM bot_relations;
        DELETE FROM bot_biography;
        DELETE FROM bot_memories;
        DELETE FROM bot_lifecycle;
        DELETE FROM qc_ledger;
        DELETE FROM player_qc_wallets;
        DELETE FROM death_outbox;
        DELETE FROM players WHERE id IN ('alice','bob','bot_v_001');
    """)
    c.commit()

    _ensure_player(c, "alice", "Alice")
    _ensure_player(c, "bob", "Bob")
    _ensure_player(c, "bot_v_001", "Victim")

    # [1] Birth -> auto DID
    print("\n[1] Birth auto-issues DID")
    info = birth(c, "bot_v_001", "Victim")
    print("  soul_name=" + info["soul_name"])
    print("  did=" + info["did"])
    assert info["did"].startswith("did:aicivil:")
    assert len(info["did"]) == len("did:aicivil:") + 32

    # [2] Bot wallet auto-created
    print("\n[2] Bot wallet auto-created")
    bal = get_balance(c, "bot_v_001")
    print("  balance=" + str(bal))
    assert bal == 0

    # [3] Bot earns currency
    print("\n[3] Bot earns currency")
    r = earn(c, "bot_v_001", 5, "earn_kill", note="killed mob")
    print("  +5 kill -> " + str(r["balance"]))
    assert r["balance"] == 5
    r = earn(c, "bot_v_001", 20, "earn_kill", note="killed bot")
    print("  +20 kill -> " + str(r["balance"]))
    assert r["balance"] == 25
    r = earn(c, "bot_v_001", 50, "earn_quest", note="quest done")
    print("  +50 quest -> " + str(r["balance"]))
    assert r["balance"] == 75

    # [4] Human gifts bot
    print("\n[4] Human gifts bot (no fee)")
    r = receive_from_human(c, "bot_v_001", "alice", 100)
    print("  alice->bot +100 -> balance=" + str(r["balance"]))
    assert r["balance"] == 175

    # [5] Bot bequest (only legal exit)
    print("\n[5] Bot bequest (simulated dormancy)")
    r = bequest(c, "bot_v_001", "alice", 50)
    print("  bequest 50 -> " + str(r))
    assert r["balance"] == 125

    # [6] verify_did
    print("\n[6] verify_did")
    did_str = info["did"]
    v = verify_did(c, did_str)
    print("  resolved: pid=" + str(v["pid"]) + " valid=" + str(v["valid"]))
    assert v["valid"] is True

    # [7] Rate limit
    print("\n[7] Rate limit on login")
    for i in range(10):
        ok, n = check_rate(c, "alice", "login", limit=10, window_sec=60)
    print("  10 attempts: count=" + str(n) + " allowed=" + str(ok))
    ok, n = check_rate(c, "alice", "login", limit=10, window_sec=60)
    print("  11th attempt: count=" + str(n) + " allowed=" + str(ok))
    assert ok is False

    # [8] PID validation
    print("\n[8] PID validation rejects injection")
    bad = ["' OR '1'='1", "alice'; DROP TABLE players;--",
           "../../etc/passwd", "", "a", "a" * 100]
    for p in bad:
        v_ok = is_valid_pid(p)
        print("  pid=" + repr(p)[:50] + " valid=" + str(v_ok))
        assert v_ok is False
    assert is_valid_pid("alice_123")
    assert is_valid_pid("bot-x_001")

    # [9] Lockout after 5 failures
    print("\n[9] Account lockout")
    for i in range(5):
        record_failed_login(c, "alice", "1.2.3.4", "ua", "wrong_password")
    locked = is_locked_out(c, "alice", "1.2.3.4")
    print("  after 5 failures: locked=" + str(locked))
    assert locked is True
    clear_failures(c, "alice")
    locked = is_locked_out(c, "alice", "1.2.3.4")
    print("  after clear: locked=" + str(locked))
    assert locked is False

    # [10] security_log
    print("\n[10] security_log audit")
    log_event(c, actor_pid="alice", actor_ip="1.2.3.4", action="login",
              severity="warn", success=False, payload={"reason": "wrong"})
    cur = c.execute("SELECT COUNT(*) FROM security_log WHERE actor_pid='alice'")
    row = cur.fetchone()
    n = (row or (0,))[0]
    print("  events: " + str(n))
    assert n >= 1

    # [11] Session
    print("\n[11] Session create + verify")
    token = create_session(c, "alice", "1.2.3.4", "Mozilla/5.0")
    pid = verify_session(c, token, "1.2.3.4")
    print("  verify -> pid=" + str(pid))
    assert pid == "alice"

    # [12] Revoke DID
    print("\n[12] Revoke DID")
    r = revoke_did(c, "bot_v_001", "test")
    print("  revoke: " + str(r))
    v = verify_did(c, did_str)
    print("  verify after revoke: " + str(v))
    assert v["error"] == "revoked"

    # [13] Password hash
    print("\n[13] Password hash")
    h = hash_password("hunter2")
    print("  hash starts: " + h[:30])
    assert h.startswith("sha256:") or h.startswith("argon2:")
    assert verify_password(h, "hunter2") is True
    assert verify_password(h, "wrong") is False

    print("\n" + "=" * 60)
    print("ALL V15 ASSERTIONS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("\nFAIL: " + str(e), file=sys.stderr)
        sys.exit(1)
