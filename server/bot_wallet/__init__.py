"""V15 Bot Wallet — separate currency system for AI citizens.

Design principles:
  - Bots have their OWN wallet (bot_wallets) separate from human QC wallets.
  - Bots EARN currency through gameplay (kills, quests, achievements).
  - Bots RECEIVE gifts from humans (transferred from player QC -> bot wallet at face value, no fee).
  - Bots CANNOT spend currency on game items (would create RMT risk).
  - Bots CAN transfer to a human wallet only as a 'bequest' if entering dormant state.
  - Bot currency has NO cash value and CANNOT be converted to fiat. Ever.
  - This separation makes RMT structurally impossible (humans can't get fiat from bot currency).

Reward table:
  kill other bot (PvE)        +5
  kill other bot (PvP)        +20
  complete quest              +10-50
  receive gift from human     +amount (matches human gift net)
  survive N consecutive wins  +30
  hit death_count milestone   +50
  season rank top 10%         +500

Decay (optional, future):
  -5% per 30 days idle (forces activity)
"""
from __future__ import annotations
import time


def _ensure_wallet(conn, pid: str) -> dict:
    cur = conn.execute(
        "SELECT pid, balance, lifetime_earned, lifetime_received FROM bot_wallets WHERE pid=?",
        (pid,),
    )
    row = cur.fetchone()
    if row:
        return {"pid": row[0], "balance": row[1],
                "lifetime_earned": row[2], "lifetime_received": row[3]}
    conn.execute(
        "INSERT INTO bot_wallets (pid, balance, last_active) VALUES (?, 0, ?)",
        (pid, time.time()),
    )
    return {"pid": pid, "balance": 0, "lifetime_earned": 0, "lifetime_received": 0}


def get_balance(conn, pid: str) -> int:
    cur = conn.execute("SELECT balance FROM bot_wallets WHERE pid=?", (pid,))
    row = cur.fetchone()
    return row[0] if row else 0


def earn(conn, bot_pid: str, amount: int, reason: str, note: str = "") -> dict:
    """Bot earns currency through gameplay (NOT from humans)."""
    if amount <= 0:
        return {"error": "invalid_amount"}
    w = _ensure_wallet(conn, bot_pid)
    new_balance = w["balance"] + amount
    now = time.time()
    conn.execute(
        """UPDATE bot_wallets
           SET balance=?, lifetime_earned=lifetime_earned+?, last_active=?
           WHERE pid=?""",
        (new_balance, amount, now, bot_pid),
    )
    conn.execute(
        """INSERT INTO bot_ledger
           (ts, from_pid, to_pid, amount, direction, balance_after, note)
           VALUES (?, NULL, ?, ?, ?, ?, ?)""",
        (now, bot_pid, amount, reason, new_balance, note or reason),
    )
    conn.commit()
    return {"bot": bot_pid, "balance": new_balance, "earned": amount, "reason": reason}


def receive_from_human(conn, bot_pid: str, from_pid: str, amount: int) -> dict:
    """A human gifts the bot directly (no fee — different from P2P human gift)."""
    if amount <= 0:
        return {"error": "invalid_amount"}
    w = _ensure_wallet(conn, bot_pid)
    new_balance = w["balance"] + amount
    now = time.time()
    conn.execute(
        """UPDATE bot_wallets
           SET balance=?, lifetime_received=lifetime_received+?, last_active=?
           WHERE pid=?""",
        (new_balance, amount, now, bot_pid),
    )
    conn.execute(
        """INSERT INTO bot_ledger
           (ts, from_pid, to_pid, amount, direction, balance_after, note)
           VALUES (?, ?, ?, ?, 'gift_from_human', ?, ?)""",
        (now, from_pid, bot_pid, amount, new_balance,
         "human " + from_pid + " gifted bot"),
    )
    conn.commit()
    return {"bot": bot_pid, "balance": new_balance, "received": amount}


def bequest(conn, bot_pid: str, to_human_pid: str, amount: int) -> dict:
    """Bot transfers its balance to a human ONLY upon entering dormant state.

    This is the ONLY legal exit for bot currency, and only on death/dormancy.
    Used for: 'inheritance' mechanic when soul goes dormant (36 months inactive).
    """
    if amount <= 0:
        return {"error": "invalid_amount"}
    w = _ensure_wallet(conn, bot_pid)
    if w["balance"] < amount:
        return {"error": "insufficient", "balance": w["balance"], "requested": amount}
    new_balance = w["balance"] - amount
    now = time.time()
    conn.execute(
        """UPDATE bot_wallets
           SET balance=?, lifetime_bequested=lifetime_bequested+?, last_active=?
           WHERE pid=?""",
        (new_balance, amount, now, bot_pid),
    )
    conn.execute(
        """INSERT INTO bot_ledger
           (ts, from_pid, to_pid, amount, direction, balance_after, note)
           VALUES (?, ?, ?, ?, 'bequest', ?, ?)""",
        (now, bot_pid, to_human_pid, amount, new_balance,
         "bequest on dormancy to " + to_human_pid),
    )
    conn.commit()
    return {"bot": bot_pid, "balance": new_balance, "bequested": amount, "to": to_human_pid}
