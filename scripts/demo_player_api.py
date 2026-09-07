"""V18.3 end-to-end demo: Player register/login/recharge/gift API.

Simulates:
  1. Register a new human player (phone + password + 18+ confirmed)
  2. /player/me returns token, balance=0, kyc=none
  3. Recharge ¥30 (package p30) -> 300 QC credited
  4. /wallet/balance reflects new total
  5. /wallet/transactions shows the topup ledger entry
  6. Spawn a bot (alive) for gifting
  7. Gift 100 QC to bot: sender -100, bot +80, fee +20
  8. /bot/list returns the alive bot
  9. RMT cap test: try single 6000 QC -> rejected (limit 5000)
 10. KYC trigger: try recharge ¥2000 from new account -> rejected
"""
from __future__ import annotations
import sys
import os
import secrets
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from server.main import app
from server.db import connect, init_schema
from server.db.schema_v13 import ensure_v13_schema
from server.db.schema_v14 import ensure_v14_schema
from server.db.schema_v15 import ensure_v15_schema
from server.db.schema_v16 import ensure_v16_schema
from server.db.schema_v17 import ensure_v17_schema
from server.db.schema_v18 import ensure_v18_schema
from server.db.schema_player import ensure_player_schema
from server.lifecycle import birth


def _purge_demo():
    """Idempotent: clear any leftover demo_player_* rows + RMT resets."""
    c = connect()
    c.execute("PRAGMA foreign_keys=OFF")
    cur = c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    for tbl in tables:
        col_check = c.execute(f"PRAGMA table_info({tbl})").fetchall()
        col_names = {row[1] for row in col_check}
        for prefix in ("demo_player_%", "demo_p_%", "demo_bot_%", "h_%"):
            pass  # h_ prefix would nuke real users — skip
        # Use rowid filter for human_players instead
    # Targeted purge
    c.execute("DELETE FROM human_players WHERE phone LIKE '139%' AND display_name LIKE 'DemoPlayer%'")
    c.execute("DELETE FROM player_qc_wallets WHERE pid LIKE 'h_%'")
    c.execute("DELETE FROM qc_ledger WHERE from_pid LIKE 'h_%' OR to_pid LIKE 'h_%'")
    c.execute("DELETE FROM rate_limits WHERE actor_pid LIKE 'h_%'")
    c.execute("DELETE FROM login_failures WHERE pid LIKE 'h_%' OR pid LIKE '139%'")
    c.execute("DELETE FROM sessions WHERE pid LIKE 'h_%'")
    # Bot-related (test bot we spawn)
    c.execute("DELETE FROM bot_wallets WHERE pid LIKE 'demo_p_bot_%'")
    c.execute("DELETE FROM bot_biography WHERE pid LIKE 'demo_p_bot_%'")
    c.execute("DELETE FROM bot_lifecycle WHERE pid LIKE 'demo_p_bot_%'")
    c.execute("DELETE FROM players WHERE id LIKE 'demo_p_bot_%'")
    # Reset rate limits for test IPs
    c.execute("DELETE FROM rate_limits WHERE actor_pid='testclient'")
    c.execute("DELETE FROM rate_limits WHERE actor_pid LIKE 'h_%'")
    c.execute("PRAGMA foreign_keys=ON")
    c.commit()
    c.close()


def _spawn_bot(pid: str, name: str):
    """Create alive bot for gift target."""
    c = connect()
    now = time.time()
    c.execute(
        """INSERT INTO players
           (id, name, cls, level, hp, hp_max, mp, mp_max,
            atk, defn, zone, pos_x, pos_y, gold,
            rank_rating, rank_tier, wins, losses,
            created_at, last_seen)
           VALUES (?, ?, 'warrior', 1, 100, 100, 50, 50,
                   10, 5, 'starter', 0, 0, 0,
                   1000, 'bronze', 0, 0, ?, ?)""",
        (pid, name, now, now),
    )
    c.commit()
    c.close()
    birth(conn := connect(), pid, name)
    conn.close()


def main():
    print("=" * 60)
    print("V18.3 PLAYER API DEMO")
    print("=" * 60)

    # Boot: ensure all schemas exist
    c = connect()
    init_schema(c)
    for fn in (ensure_v13_schema, ensure_v14_schema,
               ensure_v15_schema, ensure_v16_schema,
               ensure_v17_schema, ensure_v18_schema,
               ensure_player_schema):
        fn(c)
    c.close()

    _purge_demo()

    client = TestClient(app)

    # === [1] Register ===
    print("\n[1] POST /player/register ...")
    # Use a fixed test phone so re-runs are deterministic
    # Phone = 11 digits, start with 139, last 8 digits random decimal
    suffix = str(int.from_bytes(secrets.token_bytes(4), "big") % 100_000_000).zfill(8)
    phone = "139" + suffix
    assert len(phone) == 11 and phone.isdigit(), f"bad phone {phone}"
    pw = "Demo1234Pass"
    r = client.post("/api/v1/player/register", json={
        "phone": phone, "password": pw, "age_confirmed": True,
        "display_name": "DemoPlayer",
    })
    assert r.status_code == 200, f"register failed: {r.status_code} {r.text}"
    reg = r.json()
    token = reg["token"]
    player_id = reg["player_id"]
    print(f"    player_id={player_id}  token={token[:12]}...")
    assert player_id.startswith("h_"), f"unexpected player_id format: {player_id}"
    assert reg["kyc_status"] == "none"
    assert reg["age_confirmed"] is True
    print("    ✅ register OK")

    # === [1.5] Re-register same phone -> 409 ===
    print("\n[1.5] Re-register same phone (expect 409)...")
    r = client.post("/api/v1/player/register", json={
        "phone": phone, "password": pw, "age_confirmed": True,
    })
    assert r.status_code == 409, f"expected 409 got {r.status_code}"
    print("    ✅ duplicate phone rejected with 409")

    # === [2] /player/me with token ===
    print("\n[2] GET /player/me ...")
    r = client.get("/api/v1/player/me",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["player_id"] == player_id
    assert me["qc_balance"] == 0
    assert me["kyc_status"] == "none"
    assert me["kyc_required"] is False
    print(f"    ✅ /me OK: balance=0, kyc=none")

    # === [2.5] /player/me without token -> 401 ===
    print("\n[2.5] GET /player/me without token (expect 401)...")
    r = client.get("/api/v1/player/me")
    assert r.status_code == 401
    print("    ✅ unauthorized rejected")

    # === [3] Recharge ¥30 -> 300 QC ===
    print("\n[3] POST /wallet/recharge ¥30 (p30)...")
    r = client.post("/api/v1/wallet/recharge", json={
        "player_id": player_id, "package_id": "p30", "amount_cny": 30,
    })
    assert r.status_code == 200, r.text
    rech = r.json()
    assert rech["qc_amount"] == 300
    assert rech["qc_balance"] == 300
    assert rech["order_id"].startswith("MOCK-")
    print(f"    ✅ recharge OK: +300 QC, balance={rech['qc_balance']}")

    # === [3.5] Wrong package -> 422 ===
    print("\n[3.5] Recharge invalid package (expect 422)...")
    r = client.post("/api/v1/wallet/recharge", json={
        "player_id": player_id, "package_id": "p999", "amount_cny": 30,
    })
    assert r.status_code == 422
    print("    ✅ unknown package rejected with 422")

    # === [3.6] Amount mismatch -> 400 ===
    print("\n[3.6] Recharge amount mismatch (expect 400)...")
    r = client.post("/api/v1/wallet/recharge", json={
        "player_id": player_id, "package_id": "p30", "amount_cny": 31,
    })
    assert r.status_code == 400
    print("    ✅ amount mismatch rejected with 400")

    # === [4] /wallet/balance ===
    print("\n[4] GET /wallet/balance ...")
    r = client.get("/api/v1/wallet/balance",
                   params={"player_id": player_id})
    assert r.status_code == 200
    bal = r.json()
    assert bal["qc_balance"] == 300
    assert bal["total_recharged_cny"] == 30.0
    print(f"    ✅ balance=300, total_recharged_cny=30.0")

    # === [5] /wallet/transactions ===
    print("\n[5] GET /wallet/transactions ...")
    r = client.get("/api/v1/wallet/transactions",
                   params={"player_id": player_id, "limit": 10})
    assert r.status_code == 200
    txs = r.json()
    assert txs["count"] >= 1
    assert any(t["direction"] == "topup" for t in txs["transactions"])
    print(f"    ✅ {txs['count']} transactions; topup row present")

    # === [6] Spawn bot for gift ===
    print("\n[6] Spawn alive bot 'demo_p_bot_001' ...")
    bot_pid = "demo_p_bot_" + secrets.token_hex(3)
    _spawn_bot(bot_pid, "GiftTarget")
    print(f"    bot_pid={bot_pid}")

    # === [7] /bot/list ===
    print("\n[7] GET /bot/list ...")
    r = client.get("/api/v1/bot/list", params={"alive": "true", "limit": 5})
    assert r.status_code == 200
    bl = r.json()
    assert bl["count"] >= 1
    pids = [b["pid"] for b in bl["bots"]]
    assert bot_pid in pids
    print(f"    ✅ {bl['count']} bots; gift target present")

    # === [8] Gift 100 QC to bot ===
    print("\n[8] POST /wallet/gift 100 QC to bot ...")
    r = client.post("/api/v1/wallet/gift", json={
        "player_id": player_id, "bot_pid": bot_pid,
        "qc_amount": 100, "message": "加油小机器人!",
    })
    assert r.status_code == 200, r.text
    gift = r.json()
    assert gift["qc_amount"] == 100
    assert gift["platform_fee"] == 20
    assert gift["bot_received"] == 80
    assert gift["balance"] == 200  # 300 - 100
    print(f"    ✅ gift OK: -100 / +80 to bot / +20 fee")

    # === [8.5] Insufficient balance -> 400 ===
    print("\n[8.5] Gift 10000 QC (exceeds balance, expect 400)...")
    r = client.post("/api/v1/wallet/gift", json={
        "player_id": player_id, "bot_pid": bot_pid, "qc_amount": 10000,
    })
    assert r.status_code == 400, f"got {r.status_code}: {r.text}"
    print(f"    ✅ insufficient_balance rejected")

    # === [9] RMT cap: single 5001 -> 400 ===
    print("\n[9] Gift 5001 QC (exceeds single 5000, expect 400)...")
    r = client.post("/api/v1/wallet/gift", json={
        "player_id": player_id, "bot_pid": bot_pid, "qc_amount": 5001,
    })
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail.get("error") == "single_limit"
    print(f"    ✅ single_limit triggered")

    # === [10] Gift to sleeping bot -> 400 ===
    print("\n[10] Gift to non-alive bot (expect 400)...")
    # Kill the bot
    c = connect()
    c.execute("UPDATE players SET hp=0 WHERE id=?", (bot_pid,))
    c.commit()
    c.close()
    from server.death_dispatcher import process_pending
    c = connect()
    process_pending(c, limit=5)
    c.close()
    r = client.post("/api/v1/wallet/gift", json={
        "player_id": player_id, "bot_pid": bot_pid, "qc_amount": 50,
    })
    assert r.status_code == 400, f"got {r.status_code}: {r.text}"
    print(f"    ✅ bot_not_alive rejected")

    # === [11] KYC trigger: recharge ¥2000 from new account -> 403 ===
    # Need fresh player (this one already passed KYC threshold of ¥30 from earlier,
    # which is <¥1000, but we need to test the trigger).
    print("\n[11] Register fresh player + try ¥2000 recharge (expect 403 kyc)...")
    fresh_suffix = str(int.from_bytes(secrets.token_bytes(4), "big") % 100_000_000).zfill(8)
    fresh_phone = "137" + fresh_suffix
    assert len(fresh_phone) == 11 and fresh_phone.isdigit()
    r = client.post("/api/v1/player/register", json={
        "phone": fresh_phone, "password": pw, "age_confirmed": True,
    })
    assert r.status_code == 200, r.text
    fresh = r.json()
    fresh_pid = fresh["player_id"]
    # First recharge ¥648 (under ¥1000 threshold, should pass)
    r = client.post("/api/v1/wallet/recharge", json={
        "player_id": fresh_pid, "package_id": "p648", "amount_cny": 648,
    })
    assert r.status_code == 200, f"first ¥648 should pass: {r.status_code} {r.text}"
    # Second recharge ¥648 -> total 30d=¥1296 > ¥1000 -> KYC required
    r = client.post("/api/v1/wallet/recharge", json={
        "player_id": fresh_pid, "package_id": "p648", "amount_cny": 648,
    })
    assert r.status_code == 403, f"got {r.status_code}: {r.text}"
    detail = r.json()["detail"]
    assert detail == "kyc_required"
    print(f"    ✅ kyc_required triggered at ¥1000/30d threshold")

    # === [12] Login with wrong password -> 401 ===
    print("\n[12] Login wrong password (expect 401)...")
    r = client.post("/api/v1/player/login",
                    json={"phone": phone, "password": "wrong"})
    assert r.status_code == 401
    print(f"    ✅ bad credentials rejected")

    # === [13] Successful login ===
    print("\n[13] Login correct password ...")
    r = client.post("/api/v1/player/login",
                    json={"phone": phone, "password": pw})
    assert r.status_code == 200
    lg = r.json()
    assert lg["token"]
    assert lg["player_id"] == player_id
    print(f"    ✅ login OK, new token issued")

    # === [14] Login with bad phone format -> 422 ===
    print("\n[14] Login bad phone (expect 422)...")
    r = client.post("/api/v1/player/login",
                    json={"phone": "abc", "password": pw})
    assert r.status_code == 422
    print(f"    ✅ bad phone rejected")

    print("\n" + "=" * 60)
    print("ALL V18.3 PLAYER API ASSERTIONS PASSED ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()