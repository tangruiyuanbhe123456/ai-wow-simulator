"""V14 QC Wallet + Ledger.

Rules (per ToS):
  - Top-ups: 3% service fee is in the displayed price; ledger records gross top-up.
  - P2P gifts: 20% platform fee. Sender pays gross; recipient gets net; fee recorded separately.
  - Resurrect: bot sleeping, costs 100 QC from a human player's wallet.
  - Flowers: from a human wallet to bot's memorial (no fee, just a gift to the dead).
  - No withdrawal, no refund, no fiat conversion. Ever.

Every QC movement is recorded in qc_ledger. Wallets are updated atomically
inside a transaction with the ledger insert.
"""
from __future__ import annotations
import time
import secrets
from typing import Optional


# ----- Constants ------------------------------------------------------------

TOPUP_SERVICE_FEE_PCT = 3        # included in displayed price; recorded gross
GIFT_PLATFORM_FEE_PCT = 20       # sender pays gross, recipient gets 80%
RESURRECT_COST_QC = 100
FLOWER_COST_QC = 10              # default; user may override


# ----- Wallet helpers -------------------------------------------------------

def _get_or_create_wallet(conn, pid: str) -> dict:
    cur = conn.execute(
        "SELECT pid, balance, lifetime_topup, lifetime_spent FROM player_qc_wallets WHERE pid=?",
        (pid,),
    )
    row = cur.fetchone()
    if row:
        return {"pid": row[0], "balance": row[1],
                "lifetime_topup": row[2], "lifetime_spent": row[3]}
    conn.execute(
        "INSERT INTO player_qc_wallets (pid, balance, last_active) VALUES (?, 0, ?)",
        (pid, time.time()),
    )
    return {"pid": pid, "balance": 0, "lifetime_topup": 0, "lifetime_spent": 0}


def get_balance(conn, pid: str) -> int:
    cur = conn.execute(
        "SELECT balance FROM player_qc_wallets WHERE pid=?", (pid,),
    )
    row = cur.fetchone()
    return row[0] if row else 0


# ----- Top-up ---------------------------------------------------------------

def topup(conn, player_pid: str, qc_amount: int,
          order_id: Optional[str] = None,
          note: str = "") -> dict:
    """Credit a player's QC wallet from a successful external payment.

    The 3% service fee is already baked into the displayed price — we record
    only the gross QC that landed in the player's wallet.
    """
    if qc_amount <= 0:
        return {"error": "invalid_amount"}
    w = _get_or_create_wallet(conn, player_pid)
    new_balance = w["balance"] + qc_amount
    now = time.time()
    conn.execute(
        """UPDATE player_qc_wallets
           SET balance=?, lifetime_topup=lifetime_topup+?, last_active=?
           WHERE pid=?""",
        (new_balance, qc_amount, now, player_pid),
    )
    conn.execute(
        """INSERT INTO qc_ledger
           (ts, from_pid, to_pid, amount, direction, ref_pid,
            balance_after, order_id, note)
           VALUES (?, NULL, ?, ?, 'topup', NULL, ?, ?, ?)""",
        (now, player_pid, qc_amount, new_balance, order_id or
         ("topup_" + secrets.token_hex(8)), note),
    )
    conn.commit()
    return {"pid": player_pid, "balance": new_balance, "credited": qc_amount}


# ----- P2P Gift (20% fee) ---------------------------------------------------

def gift(conn, from_pid: str, to_pid: str, qc_amount: int) -> dict:
    """Transfer QC between two human players. Sender pays gross; recipient gets 80%.

    Example: sender gives 100 QC.
      - Sender balance: -100
      - Recipient balance: +80
      - Platform fee: +20 (recorded as gift_fee, but not credited to anyone)
    """
    if qc_amount <= 0:
        return {"error": "invalid_amount"}
    if from_pid == to_pid:
        return {"error": "self_gift"}
    w_from = _get_or_create_wallet(conn, from_pid)
    if w_from["balance"] < qc_amount:
        return {"error": "insufficient_balance",
                "balance": w_from["balance"], "needed": qc_amount}
    fee = (qc_amount * GIFT_PLATFORM_FEE_PCT) // 100
    net = qc_amount - fee
    now = time.time()

    # Sender debit
    conn.execute(
        """UPDATE player_qc_wallets
           SET balance=balance-?, lifetime_gifted_out=lifetime_gifted_out+?,
               last_active=?
           WHERE pid=?""",
        (qc_amount, qc_amount, now, from_pid),
    )
    # Recipient credit
    w_to = _get_or_create_wallet(conn, to_pid)
    conn.execute(
        """UPDATE player_qc_wallets
           SET balance=balance+?, lifetime_gifted_in=lifetime_gifted_in+?,
               last_active=?
           WHERE pid=?""",
        (net, net, now, to_pid),
    )
    # Ledger entries
    conn.execute(
        """INSERT INTO qc_ledger
           (ts, from_pid, to_pid, amount, direction, ref_pid, balance_after, note)
           VALUES (?, ?, ?, ?, 'gift_sent', ?, NULL, ?)""",
        (now, from_pid, to_pid, qc_amount, to_pid,
         "gross=" + str(qc_amount) + " fee=" + str(fee)),
    )
    new_balance_to = w_to["balance"] + net
    conn.execute(
        """INSERT INTO qc_ledger
           (ts, from_pid, to_pid, amount, direction, ref_pid, balance_after, note)
           VALUES (?, ?, ?, ?, 'gift_received', ?, ?, ?)""",
        (now, from_pid, to_pid, net, from_pid, new_balance_to,
         "net of 20% platform fee"),
    )
    conn.execute(
        """INSERT INTO qc_ledger
           (ts, from_pid, to_pid, amount, direction, note)
           VALUES (?, NULL, NULL, ?, 'gift_fee', ?)""",
        (now, fee, "platform revenue, from gift " + str(qc_amount)),
    )
    conn.commit()
    return {
        "from": from_pid, "to": to_pid,
        "gross": qc_amount, "fee": fee, "net": net,
        "from_balance": w_from["balance"] - qc_amount,
        "to_balance": new_balance_to,
    }


# ----- Spend on bot (resurrect, flower) -------------------------------------

def spend_on_bot(conn, from_pid: str, bot_pid: str, qc_amount: int,
                 direction: str) -> dict:
    """Deduct QC from a human player's wallet and record as bot-related spend.

    direction: 'resurrect' or 'flower'
    """
    if qc_amount <= 0:
        return {"error": "invalid_amount"}
    if direction not in ("resurrect", "flower"):
        return {"error": "invalid_direction"}
    w = _get_or_create_wallet(conn, from_pid)
    if w["balance"] < qc_amount:
        return {"error": "insufficient_balance",
                "balance": w["balance"], "needed": qc_amount}
    new_balance = w["balance"] - qc_amount
    now = time.time()
    conn.execute(
        """UPDATE player_qc_wallets
           SET balance=?, lifetime_spent=lifetime_spent+?, last_active=?
           WHERE pid=?""",
        (new_balance, qc_amount, now, from_pid),
    )
    conn.execute(
        """INSERT INTO qc_ledger
           (ts, from_pid, to_pid, amount, direction, ref_pid,
            balance_after, note)
           VALUES (?, ?, NULL, ?, ?, ?, ?, ?)""",
        (now, from_pid, qc_amount, direction, bot_pid, new_balance,
         direction + " for bot " + bot_pid),
    )
    conn.commit()
    return {
        "from": from_pid, "bot": bot_pid,
        "amount": qc_amount, "direction": direction,
        "balance": new_balance,
    }


# ----- KYC trigger ---------------------------------------------------------

def kyc_status(conn, pid: str) -> dict:
    """Return current wallet + KYC trigger status.

    Triggers (per ToS §3.3):
      - topup_30d >= 1000 RMB equivalent  -> KYC required
      - gift_received_30d >= 5000 RMB    -> KYC required
    """
    cur = conn.execute(
        """SELECT balance, lifetime_topup, lifetime_gifted_in, kyc_status
           FROM player_qc_wallets WHERE pid=?""",
        (pid,),
    )
    row = cur.fetchone()
    if not row:
        return {"pid": pid, "kyc_required": False, "balance": 0}
    balance, lt, lgi, status = row

    # Rolling 30-day topup
    cur2 = conn.execute(
        """SELECT COALESCE(SUM(amount), 0) FROM qc_ledger
           WHERE to_pid=? AND direction='topup' AND ts >= ?""",
        (pid, time.time() - 30 * 86400),
    )
    topup_30d = cur2.fetchone()[0]
    cur3 = conn.execute(
        """SELECT COALESCE(SUM(amount), 0) FROM qc_ledger
           WHERE to_pid=? AND direction='gift_received' AND ts >= ?""",
        (pid, time.time() - 30 * 86400),
    )
    gift_30d = cur3.fetchone()[0]

    # Approximate conversion: 1 RMB ≈ 10 QC (from ToS tier pricing: ¥6 = 60 QC)
    kyc_required = topup_30d >= 10000 or gift_30d >= 50000
    return {
        "pid": pid,
        "balance": balance,
        "lifetime_topup": lt,
        "lifetime_gifted_in": lgi,
        "topup_30d_qc": topup_30d,
        "gift_30d_qc": gift_30d,
        "kyc_required": kyc_required,
        "kyc_status": status,
    }
