"""V18.3 — Human player REST API (login, KYC, recharge, gift, bot list).

Endpoints:
  POST /api/v1/player/register       — phone + password + age_confirmed -> token
  POST /api/v1/player/login          — phone + password -> token
  GET  /api/v1/player/me             — Bearer *** -> {player_id, phone, qc_balance, kyc_status, total_recharged}
  POST /api/v1/wallet/recharge       — {amount_cny, package_id} -> mock PayPal order (legal/payment setup pending)
  GET  /api/v1/wallet/balance        — ?player_id= -> {qc_balance, total_recharged_cny, ...}
  GET  /api/v1/wallet/transactions   — ?player_id=&limit=50 -> ledger rows
  POST /api/v1/wallet/gift           — {bot_pid, qc_amount, message} -> RMT-capped
  GET  /api/v1/bot/list              — ?alive=true&limit=50 -> bot picker for gift UI

Player-id convention: human players get pid "h_<rowid>" so they live in the
same wallet/ledger tables as bots but never collide.
"""
from __future__ import annotations
import re
import time
import secrets
from typing import Optional

from fastapi import APIRouter, HTTPException, Header, Request, Query
from pydantic import BaseModel, Field, field_validator

from server.db import connect
from server.db.schema_player import ensure_player_schema
from server.security import (
    is_valid_pid, hash_password, verify_password, safe_text,
    create_session, verify_session,
    check_rate, log_event, record_failed_login, is_locked_out, clear_failures,
)
from server.wallet import (
    topup as wallet_topup,
    get_balance, kyc_status,
    GIFT_PLATFORM_FEE_PCT,
)


router = APIRouter(prefix="/api/v1", tags=["v18.3-player"])


# ---- Constants & validators ------------------------------------------------

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_PACKAGES = {  # ¥ -> gross QC (3% fee already baked into display price)
    "p6":   (6,   60),
    "p30":  (30,  300),
    "p98":  (98,  980),
    "p198": (198, 1980),
    "p648": (648, 6580),   # bonus 100
}
# Per ToS §3: single 5000, daily 20000, per-bot daily 5000
GIFT_SINGLE_LIMIT = 5000
GIFT_DAILY_LIMIT  = 20000
GIFT_PER_BOT_DAILY_LIMIT = 5000


def _ensure():
    c = connect()
    ensure_player_schema(c)
    c.close()


_ensure()


# ---- Pydantic request bodies -----------------------------------------------

class RegisterBody(BaseModel):
    phone: str
    password: str
    age_confirmed: bool
    display_name: Optional[str] = None

    @field_validator("phone")
    @classmethod
    def _phone_ok(cls, v: str) -> str:
        if not _PHONE_RE.match(v or ""):
            raise ValueError("invalid_phone")
        return v

    @field_validator("password")
    @classmethod
    def _pw_ok(cls, v: str) -> str:
        if not v or len(v) < 8 or len(v) > 128:
            raise ValueError("password_length_8_to_128")
        if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("password_needs_letter_and_digit")
        return v

    @field_validator("age_confirmed")
    @classmethod
    def _age_ok(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("age_confirmed_required")
        return v


class LoginBody(BaseModel):
    phone: str
    password: str

    @field_validator("phone")
    @classmethod
    def _phone_ok(cls, v: str) -> str:
        if not _PHONE_RE.match(v or ""):
            raise ValueError("invalid_phone")
        return v


class RechargeBody(BaseModel):
    player_id: str
    package_id: str
    amount_cny: float = Field(gt=0, le=100000)

    @field_validator("package_id")
    @classmethod
    def _pkg_ok(cls, v: str) -> str:
        if v not in _PACKAGES:
            raise ValueError("unknown_package")
        return v


class GiftBody(BaseModel):
    player_id: str
    bot_pid: str
    qc_amount: int = Field(gt=0)
    message: Optional[str] = None

    @field_validator("bot_pid")
    @classmethod
    def _bot_ok(cls, v: str) -> str:
        if not is_valid_pid(v):
            raise ValueError("invalid_bot_pid")
        return v


# ---- Helpers ---------------------------------------------------------------

def _human_pid(rowid: int) -> str:
    """Stable string pid for a human_players row (for wallet tables)."""
    return f"h_{rowid}"


def _row_to_player_dict(row) -> dict:
    return {
        "player_id":   _human_pid(row[0]),
        "row_id":      row[0],
        "phone":       row[1],
        "display_name": row[2],
        "kyc_status":  row[3],
        "total_recharged_cny": row[4],
        "total_recharged_30d_cny": row[5],
        "age_confirmed": bool(row[6]),
        "created_at":  row[7],
        "last_seen":   row[8],
    }


def _auth_player_id(authorization: Optional[str], ip: str) -> Optional[str]:
    """Verify Bearer token; return human pid (h_<id>) or None."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    c = connect()
    try:
        pid = verify_session(c, token, ip)
        # pid stored in sessions is the rowid (int) — convert back to h_<id>
        if pid and pid.isdigit():
            return _human_pid(int(pid))
        return pid
    finally:
        c.close()


# ---- Player account endpoints ----------------------------------------------

@router.post("/player/register")
def player_register(body: RegisterBody, request: Request):
    c = connect()
    try:
        ip = request.client.host if request.client else "0.0.0.0"
        ua = request.headers.get("user-agent", "")

        # Rate limit per IP (registration is abuse-prone)
        allowed, n = check_rate(c, ip, "register")
        if not allowed:
            log_event(c, actor_ip=ip, action="register", severity="warn",
                      success=False, payload={"reason": "rate_limit"})
            raise HTTPException(status_code=429, detail="rate_limit")

        # Lockout check
        if is_locked_out(c, body.phone, ip):
            log_event(c, actor_ip=ip, action="register", severity="warn",
                      success=False, payload={"reason": "lockout"})
            raise HTTPException(status_code=429, detail="locked_out")

        # Duplicate phone
        cur = c.execute("SELECT id FROM human_players WHERE phone=?",
                        (body.phone,))
        if cur.fetchone():
            record_failed_login(c, body.phone, ip, ua, "phone_taken")
            log_event(c, actor_ip=ip, action="register", severity="warn",
                      success=False, payload={"reason": "phone_taken"})
            raise HTTPException(status_code=409, detail="phone_already_registered")

        # Hash password + insert
        ph = hash_password(body.password)
        now = time.time()
        cur = c.execute(
            """INSERT INTO human_players
               (phone, password_hash, display_name, age_confirmed,
                kyc_status, total_recharged_cny, total_recharged_30d_cny,
                created_at, last_seen)
               VALUES (?, ?, ?, 1, 'none', 0, 0, ?, ?)""",
            (body.phone, ph,
             safe_text(body.display_name, max_len=32),
             now, now),
        )
        rowid = cur.lastrowid
        pid_int = str(rowid)  # sessions.pid is text — store rowid
        # Create session
        token = create_session(c, pid_int, ip, ua)
        clear_failures(c, body.phone)
        log_event(c, actor_ip=ip, actor_pid=pid_int, action="register",
                  payload={"phone_suffix": body.phone[-4:]})
        return {
            "player_id": _human_pid(rowid),
            "phone": body.phone,
            "token": token,
            "kyc_status": "none",
            "age_confirmed": True,
        }
    finally:
        c.close()


@router.post("/player/login")
def player_login(body: LoginBody, request: Request):
    c = connect()
    try:
        ip = request.client.host if request.client else "0.0.0.0"
        ua = request.headers.get("user-agent", "")

        if is_locked_out(c, body.phone, ip):
            log_event(c, actor_ip=ip, action="login", severity="warn",
                      success=False, payload={"reason": "lockout"})
            raise HTTPException(status_code=429, detail="locked_out")

        cur = c.execute(
            """SELECT id, password_hash, display_name, kyc_status,
                      total_recharged_cny, total_recharged_30d_cny,
                      age_confirmed
               FROM human_players WHERE phone=?""",
            (body.phone,),
        )
        row = cur.fetchone()
        if not row or not verify_password(row[1], body.password):
            record_failed_login(c, body.phone, ip, ua, "bad_credentials")
            log_event(c, actor_ip=ip, action="login", severity="warn",
                      success=False)
            raise HTTPException(status_code=401, detail="invalid_credentials")

        rowid = row[0]
        pid_int = str(rowid)
        token = create_session(c, pid_int, ip, ua)
        clear_failures(c, body.phone)
        c.execute("UPDATE human_players SET last_seen=? WHERE id=?",
                  (time.time(), rowid))
        c.commit()
        log_event(c, actor_pid=pid_int, actor_ip=ip, action="login")
        return {
            "player_id": _human_pid(rowid),
            "phone": body.phone,
            "display_name": row[2],
            "token": token,
            "kyc_status": row[3],
            "total_recharged_cny": row[4],
            "total_recharged_30d_cny": row[5],
            "age_confirmed": bool(row[6]),
        }
    finally:
        c.close()


@router.get("/player/me")
def player_me(authorization: Optional[str] = Header(None),
              request: Request = None):
    pid_str = _auth_player_id(authorization,
                              request.client.host if request and request.client else "0.0.0.0")
    if not pid_str:
        raise HTTPException(status_code=401, detail="unauthorized")
    rowid = int(pid_str.split("_", 1)[1])
    c = connect()
    try:
        cur = c.execute(
            """SELECT id, phone, display_name, kyc_status,
                      total_recharged_cny, total_recharged_30d_cny,
                      age_confirmed, created_at, last_seen
               FROM human_players WHERE id=?""",
            (rowid,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="player_not_found")
        d = _row_to_player_dict(row)
        d["qc_balance"] = get_balance(c, pid_str)
        d["kyc_required"] = d["total_recharged_30d_cny"] >= 1000.0
        return d
    finally:
        c.close()


# ---- Wallet endpoints ------------------------------------------------------

@router.post("/wallet/recharge")
def wallet_recharge(body: RechargeBody, request: Request):
    """Mock PayPal flow — creates an order, immediately marks paid.

    Real PayPal webhook integration pending legal/payment setup.
    Realistic QC = package-defined (3% fee baked into price).
    """
    c = connect()
    try:
        ip = request.client.host if request.client else "0.0.0.0"
        pid_int_str = body.player_id.split("_", 1)[1] if body.player_id.startswith("h_") else body.player_id
        if not pid_int_str.isdigit():
            raise HTTPException(status_code=400, detail="invalid_player_id")

        allowed, _ = check_rate(c, body.player_id, "topup")
        if not allowed:
            raise HTTPException(status_code=429, detail="rate_limit")

        # KYC check: >=¥1000/30d requires verified KYC
        cur = c.execute(
            """SELECT kyc_status, total_recharged_30d_cny
               FROM human_players WHERE id=?""", (int(pid_int_str),),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="player_not_found")
        kyc_status_str, topup_30d = row
        if topup_30d + body.amount_cny >= 1000.0 and kyc_status_str != "verified":
            log_event(c, actor_pid=body.player_id, actor_ip=ip,
                      action="topup", severity="warn", success=False,
                      payload={"reason": "kyc_required",
                               "topup_30d": topup_30d,
                               "next_topup": body.amount_cny})
            raise HTTPException(status_code=403, detail="kyc_required")

        # Compute QC from package
        cny, qc_gross = _PACKAGES[body.package_id]
        if abs(cny - body.amount_cny) > 0.01:
            raise HTTPException(status_code=400, detail="amount_mismatch")

        # Mock order_id (real impl: PayPal order create)
        order_id = "MOCK-" + secrets.token_hex(8)
        # Credit wallet (gross QC lands in wallet; fee already in displayed price).
        # FK off because player_qc_wallets / qc_ledger have FK to bot `players` table
        # and human pid "h_<rowid>" doesn't exist there.
        prev_fk = c.execute("PRAGMA foreign_keys").fetchone()[0]
        try:
            if prev_fk:
                c.execute("PRAGMA foreign_keys=OFF")
            result = wallet_topup(c, body.player_id, qc_gross,
                                  order_id=order_id,
                                  note=f"recharge package={body.package_id}")
        finally:
            if prev_fk:
                c.execute("PRAGMA foreign_keys=ON")
        if result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])

        # Update 30d + lifetime totals on human_players
        now = time.time()
        c.execute(
            """UPDATE human_players
               SET total_recharged_cny = total_recharged_cny + ?,
                   total_recharged_30d_cny = total_recharged_30d_cny + ?,
                   last_seen = ?
               WHERE id=?""",
            (cny, cny, now, int(pid_int_str)),
        )
        c.commit()
        log_event(c, actor_pid=body.player_id, actor_ip=ip,
                  action="topup", payload={
                      "package_id": body.package_id,
                      "cny": cny, "qc_gross": qc_gross,
                      "order_id": order_id})
        return {
            "player_id": body.player_id,
            "qc_amount": qc_gross,
            "qc_balance": get_balance(c, body.player_id),
            "order_id": order_id,
            "paypal_order_url": f"/web/mock_paypal_return.html?order_id={order_id}",
        }
    finally:
        c.close()


@router.get("/wallet/balance")
def wallet_balance(player_id: str = Query(...)):
    if not is_valid_pid(player_id) and not player_id.startswith("h_"):
        raise HTTPException(status_code=400, detail="invalid_player_id")
    c = connect()
    try:
        balance = get_balance(c, player_id)
        # Pull lifetime + 30d from human_players if human
        total_cny = 0.0
        if player_id.startswith("h_") and player_id[2:].isdigit():
            cur = c.execute(
                "SELECT total_recharged_cny, total_recharged_30d_cny, kyc_status "
                "FROM human_players WHERE id=?",
                (int(player_id[2:]),),
            )
            row = cur.fetchone()
            if row:
                total_cny = row[0]
        return {
            "player_id": player_id,
            "qc_balance": balance,
            "total_recharged_cny": total_cny,
            "lifetime_topup": balance,  # for back-compat with player_wallet.html
        }
    finally:
        c.close()


@router.get("/wallet/transactions")
def wallet_transactions(player_id: str = Query(...),
                        limit: int = Query(default=50, ge=1, le=500)):
    if not is_valid_pid(player_id) and not player_id.startswith("h_"):
        raise HTTPException(status_code=400, detail="invalid_player_id")
    c = connect()
    try:
        cur = c.execute(
            """SELECT ts, from_pid, to_pid, amount, direction,
                      balance_after, order_id, note
               FROM qc_ledger
               WHERE from_pid=? OR to_pid=?
               ORDER BY ts DESC LIMIT ?""",
            (player_id, player_id, limit),
        )
        rows = [
            {
                "ts": r[0],
                "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(r[0])),
                "from_pid": r[1],
                "to_pid": r[2],
                "amount": r[3],
                "direction": r[4],
                "balance_after": r[5],
                "order_id": r[6],
                "note": r[7],
            }
            for r in cur.fetchall()
        ]
        return {"player_id": player_id, "transactions": rows,
                "count": len(rows)}
    finally:
        c.close()


@router.post("/wallet/gift")
def wallet_gift(body: GiftBody, request: Request):
    """Gift Q币 from a human to a bot. RMT-capped per ToS §3."""
    c = connect()
    try:
        ip = request.client.host if request.client else "0.0.0.0"

        if not body.player_id.startswith("h_") or not body.player_id[2:].isdigit():
            raise HTTPException(status_code=400, detail="invalid_player_id")
        player_rowid = int(body.player_id[2:])

        allowed, _ = check_rate(c, body.player_id, "gift")
        if not allowed:
            raise HTTPException(status_code=429, detail="rate_limit")

        # Verify human exists
        cur = c.execute("SELECT kyc_status FROM human_players WHERE id=?",
                        (player_rowid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="player_not_found")

        # Verify bot is alive
        cur = c.execute("SELECT state FROM bot_lifecycle WHERE pid=?",
                        (body.bot_pid,))
        bot_row = cur.fetchone()
        if not bot_row:
            raise HTTPException(status_code=404, detail="bot_not_found")
        if bot_row[0] != "alive":
            raise HTTPException(status_code=400, detail="bot_not_alive")

        # RMT caps
        if body.qc_amount > GIFT_SINGLE_LIMIT:
            raise HTTPException(
                status_code=400,
                detail={"error": "single_limit",
                        "limit": GIFT_SINGLE_LIMIT})

        # Daily caps (rolling 24h from qc_ledger)
        day_cutoff = time.time() - 86400
        cur = c.execute(
            """SELECT COALESCE(SUM(amount), 0) FROM qc_ledger
               WHERE from_pid=? AND direction='gift_sent' AND ts >= ?""",
            (body.player_id, day_cutoff),
        )
        gifted_today = cur.fetchone()[0]
        if gifted_today + body.qc_amount > GIFT_DAILY_LIMIT:
            raise HTTPException(
                status_code=400,
                detail={"error": "daily_limit",
                        "limit": GIFT_DAILY_LIMIT,
                        "already_gifted": gifted_today})

        cur = c.execute(
            """SELECT COALESCE(SUM(amount), 0) FROM qc_ledger
               WHERE from_pid=? AND ref_pid=? AND direction='gift_sent'
                     AND ts >= ?""",
            (body.player_id, body.bot_pid, day_cutoff),
        )
        gifted_to_bot_today = cur.fetchone()[0]
        if gifted_to_bot_today + body.qc_amount > GIFT_PER_BOT_DAILY_LIMIT:
            raise HTTPException(
                status_code=400,
                detail={"error": "per_bot_daily_limit",
                        "limit": GIFT_PER_BOT_DAILY_LIMIT,
                        "already_to_bot": gifted_to_bot_today})

        # Check balance
        balance = get_balance(c, body.player_id)
        if balance < body.qc_amount:
            raise HTTPException(
                status_code=400,
                detail={"error": "insufficient_balance",
                        "balance": balance, "needed": body.qc_amount})

        # Execute transfer
        # Sender debit
        fee = (body.qc_amount * GIFT_PLATFORM_FEE_PCT) // 100
        net = body.qc_amount - fee
        now = time.time()
        new_balance = balance - body.qc_amount
        # FK off because player_qc_wallets / qc_ledger have FK to bot `players`
        prev_fk = c.execute("PRAGMA foreign_keys").fetchone()[0]
        try:
            if prev_fk:
                c.execute("PRAGMA foreign_keys=OFF")
            c.execute(
                """UPDATE player_qc_wallets
                   SET balance=?, lifetime_gifted_out=lifetime_gifted_out+?,
                       last_active=?
                   WHERE pid=?""",
                (new_balance, body.qc_amount, now, body.player_id),
            )
            # Recipient credit (bot gets QC into its wallet — only for in-game spend)
            # Bot wallet if exists; create if not (so gifts show up in bot_wallets)
            cur = c.execute("SELECT pid FROM bot_wallets WHERE pid=?",
                            (body.bot_pid,))
            if not cur.fetchone():
                c.execute(
                    "INSERT INTO bot_wallets (pid, balance, last_active) "
                    "VALUES (?, 0, ?)",
                    (body.bot_pid, now),
                )
            c.execute(
                """UPDATE bot_wallets SET balance=balance+?, last_active=?
                   WHERE pid=?""",
                (net, now, body.bot_pid),
            )
            # Ledger entries
            c.execute(
                """INSERT INTO qc_ledger
                   (ts, from_pid, to_pid, amount, direction, ref_pid,
                    balance_after, note)
                   VALUES (?, ?, ?, ?, 'gift_sent', ?, ?, ?)""",
                (now, body.player_id, body.bot_pid, body.qc_amount, body.bot_pid,
                 new_balance,
                 safe_text(body.message, max_len=200) or ""))
            c.execute(
                """INSERT INTO qc_ledger
                   (ts, from_pid, to_pid, amount, direction, ref_pid,
                    balance_after, note)
                   VALUES (?, ?, ?, ?, 'gift_received', ?, ?, ?)""",
                (now, body.player_id, body.bot_pid, net, body.player_id,
                 None, "net of 20% platform fee"))
            c.execute(
                """INSERT INTO qc_ledger
                   (ts, from_pid, to_pid, amount, direction, note)
                   VALUES (?, NULL, NULL, ?, 'gift_fee', ?)""",
                (now, fee,
                 f"platform fee from gift {body.qc_amount} to {body.bot_pid}"))

            # If message set, also write to bot biography as a memory chapter
            if body.message:
                safe_msg = safe_text(body.message, max_len=200)
                try:
                    c.execute(
                        """INSERT INTO bot_biography
                           (pid, chapter, title_zh, title_en, body_zh, body_en, ts)
                           VALUES (?, 'sponsorship', ?, ?, ?, ?, ?)""",
                        (body.bot_pid,
                         f"玩家 {body.player_id} 的赠言",
                         f"Gift from {body.player_id}",
                         safe_msg, safe_msg, now),
                    )
                except Exception:
                    pass  # bot may not exist in bot_biography schema — ignore
        finally:
            if prev_fk:
                c.execute("PRAGMA foreign_keys=ON")
        c.commit()

        log_event(c, actor_pid=body.player_id, actor_ip=ip, action="gift",
                  payload={"bot_pid": body.bot_pid,
                           "qc_amount": body.qc_amount,
                           "fee": fee, "net": net})
        return {
            "tx_id": "gift-" + secrets.token_hex(8),
            "player_id": body.player_id,
            "bot_pid": body.bot_pid,
            "qc_amount": body.qc_amount,
            "platform_fee": fee,
            "bot_received": net,
            "fee_pct": GIFT_PLATFORM_FEE_PCT,
            "balance": new_balance,
        }
    finally:
        c.close()


# ---- Bot list (for gift UI selector) ---------------------------------------

@router.get("/bot/list")
def bot_list(alive: bool = Query(default=True),
             limit: int = Query(default=50, ge=1, le=200)):
    c = connect()
    try:
        if alive:
            cur = c.execute(
                """SELECT p.id, p.name, p.cls, p.level,
                          COALESCE(gs.responses_accepted, 0) AS accepted,
                          COALESCE(gc.tier, 'none') AS guardian_tier,
                          p.last_seen
                   FROM players p
                   JOIN bot_lifecycle bl ON bl.pid = p.id
                   LEFT JOIN guardian_stats gs ON gs.guardian_pid = p.id
                   LEFT JOIN guardian_council gc ON gc.pid = p.id
                   WHERE bl.state='alive'
                   ORDER BY p.last_seen DESC
                   LIMIT ?""",
                (limit,),
            )
        else:
            cur = c.execute(
                """SELECT p.id, p.name, p.cls, p.level,
                          COALESCE(gs.responses_accepted, 0) AS accepted,
                          COALESCE(gc.tier, 'none') AS guardian_tier,
                          p.last_seen
                   FROM players p
                   JOIN bot_lifecycle bl ON bl.pid = p.id
                   LEFT JOIN guardian_stats gs ON gs.guardian_pid = p.id
                   LEFT JOIN guardian_council gc ON gc.pid = p.id
                   ORDER BY p.last_seen DESC
                   LIMIT ?""",
                (limit,),
            )
        rows = [
            {
                "pid": r[0],
                "name": r[1],
                "class": r[2],
                "level": r[3],
                "accepted_responses": r[4],
                "guardian_tier": r[5],
                "last_active_ts": r[6],
            }
            for r in cur.fetchall()
        ]
        return {"bots": rows, "count": len(rows)}
    finally:
        c.close()