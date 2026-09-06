"""V15 Security layer.

Covers:
  - Audit logging (every privileged action)
  - Rate limiting (per-pid sliding minute window)
  - Failed login tracking (account lockout)
  - Constant-time comparison helpers
  - PID validation (rejects injection attempts)
  - SQL injection sanitization at API boundary
"""
from __future__ import annotations
import hashlib
import hmac
import re
import secrets
import time
from typing import Optional


# ---- Validation -------------------------------------------------------------

_PID_RE = re.compile(r"^[A-Za-z0-9_\-]{3,64}$")


def is_valid_pid(pid: str) -> bool:
    """PID must be 3-64 chars, alphanumeric + underscore + dash.

    This rejects SQL injection attempts (no quotes, semicolons, spaces).
    """
    if not pid or not isinstance(pid, str):
        return False
    return bool(_PID_RE.match(pid))


def safe_text(s, max_len=500) -> Optional[str]:
    """Strip control chars, cap length. For user-supplied text fields."""
    if s is None:
        return None
    if not isinstance(s, str):
        return ""
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    return s[:max_len]


# ---- Hashing / crypto helpers ----------------------------------------------

def hash_password(password: str, salt: Optional[str] = None) -> str:
    """Argon2id preferred; SHA-256+salt fallback."""
    if salt is None:
        salt = secrets.token_hex(16)
    try:
        from argon2 import PasswordHasher
        ph = PasswordHasher()
        h = ph.hash(password)
        return "argon2:" + salt + ":" + h
    except ImportError:
        # SHA-256 fallback (less secure but always available)
        h = hashlib.sha256((salt + ":" + password).encode()).hexdigest()
        return "sha256:" + salt + ":" + h


def verify_password(stored: str, password: str) -> bool:
    """Constant-time verification. Returns False on any malformed hash."""
    if not stored or ":" not in stored:
        return False
    scheme, salt, _ = stored.split(":", 2)
    if scheme == "argon2":
        try:
            from argon2 import PasswordHasher
            from argon2.exceptions import VerifyMismatchError
            ph = PasswordHasher()
            try:
                ph.verify(stored.split(":", 2)[2], password)
                return True
            except VerifyMismatchError:
                return False
        except ImportError:
            return False
    elif scheme == "sha256":
        expected = hashlib.sha256((salt + ":" + password).encode()).hexdigest()
        actual = stored.split(":", 2)[2]
        return hmac.compare_digest(expected, actual)
    return False


def hash_token(token: str) -> str:
    """SHA-256 a bearer token for storage. Tokens are random; we never store them plain."""
    return hashlib.sha256(token.encode()).hexdigest()


# ---- Audit logging ----------------------------------------------------------

def log_event(conn, *, actor_ip: str = "", actor_pid: str = "",
              action: str, severity: str = "info",
              payload: Optional[dict] = None,
              request_id: str = "",
              success: bool = True) -> None:
    """Append an entry to security_log. Never include passwords or tokens."""
    import json
    safe_payload = {}
    if payload:
        for k, v in payload.items():
            if k.lower() in ("password", "token", "secret", "private_key"):
                continue
            safe_payload[k] = v
    conn.execute(
        """INSERT INTO security_log
           (ts, actor_ip, actor_pid, action, severity, payload, request_id, success)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), actor_ip or "", actor_pid or "", action, severity,
         json.dumps(safe_payload, ensure_ascii=False),
         request_id or ("req-" + secrets.token_hex(8)),
         1 if success else 0),
    )
    conn.commit()


# ---- Rate limiting ----------------------------------------------------------

# Conservative defaults; tune in production
DEFAULT_LIMITS = {
    "login":         (10,  60),     # 10 attempts per 60s per pid
    "topup":         (5,   60),     # 5 topups per minute
    "gift":          (20,  60),     # 20 gifts per minute
    "resurrect":     (10,  300),    # 10 resurrects per 5 minutes
    "flower":        (30,  60),     # 30 flowerings per minute
    "register":      (3,   3600),   # 3 new accounts per hour per IP
    "general_post":  (60,  60),     # 60 POSTs per minute per pid
    "general_get":   (300, 60),     # 300 GETs per minute per pid
}


def check_rate(conn, actor_pid: str, action: str,
               limit: Optional[int] = None,
               window_sec: Optional[int] = None) -> tuple[bool, int]:
    """Returns (allowed, current_count). Sliding window approximation.

    Uses minute buckets for simplicity. For strict sliding window,
    switch to Redis ZSET in production.
    """
    if limit is None or window_sec is None:
        limit, window_sec = DEFAULT_LIMITS.get(action, (60, 60))

    now = time.time()
    window_start = (now // window_sec) * window_sec
    # Note: this resets at window boundaries (not perfectly sliding but adequate)
    cur = conn.execute(
        """SELECT count, window_start FROM rate_limits
           WHERE actor_pid=? AND action=?""",
        (actor_pid, action),
    )
    row = cur.fetchone()
    if row is None or row[1] != window_start:
        # New window
        conn.execute(
            """INSERT OR REPLACE INTO rate_limits
               (actor_pid, action, window_start, count)
               VALUES (?, ?, ?, 1)""",
            (actor_pid, action, window_start),
        )
        conn.commit()
        return (True, 1)
    new_count = row[0] + 1
    conn.execute(
        "UPDATE rate_limits SET count=? WHERE actor_pid=? AND action=?",
        (new_count, actor_pid, action),
    )
    conn.commit()
    return (new_count <= limit, new_count)


# ---- Failed login tracking & lockout ----------------------------------------

MAX_FAILED_LOGINS = 5
LOCKOUT_WINDOW_SEC = 900  # 15 minutes


def record_failed_login(conn, pid: Optional[str], ip: str,
                        user_agent: str, reason: str) -> int:
    """Record a failed login. Returns total failures in the lockout window."""
    conn.execute(
        """INSERT INTO login_failures
           (ts, pid, ip, user_agent, reason)
           VALUES (?, ?, ?, ?, ?)""",
        (time.time(), pid, ip, user_agent, reason),
    )
    conn.commit()
    return _count_recent_failures(conn, pid, ip)


def is_locked_out(conn, pid: Optional[str], ip: str) -> bool:
    """Account-lockout: too many failures recently from this pid OR ip."""
    return _count_recent_failures(conn, pid, ip) >= MAX_FAILED_LOGINS


def _count_recent_failures(conn, pid: Optional[str], ip: str) -> int:
    cutoff = time.time() - LOCKOUT_WINDOW_SEC
    if pid:
        cur = conn.execute(
            "SELECT COUNT(*) FROM login_failures WHERE pid=? AND ts >= ?",
            (pid, cutoff),
        )
        n = cur.fetchone()[0]
        if n >= MAX_FAILED_LOGINS:
            return n
    cur = conn.execute(
        "SELECT COUNT(*) FROM login_failures WHERE ip=? AND ts >= ?",
        (ip, cutoff),
    )
    return cur.fetchone()[0]


def clear_failures(conn, pid: str) -> None:
    """Clear failure history on successful login."""
    conn.execute("DELETE FROM login_failures WHERE pid=?", (pid,))
    conn.commit()


# ---- Session management -----------------------------------------------------

SESSION_TTL_SEC = 30 * 60  # 30 minutes


def create_session(conn, pid: str, ip: str, user_agent: str) -> str:
    """Create a session. Returns the bearer token (plaintext, never stored)."""
    token = secrets.token_urlsafe(32)
    token_h = hash_token(token)
    now = time.time()
    conn.execute(
        """INSERT INTO sessions
           (token_hash, pid, issued_at, expires_at, last_used, ip, user_agent)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (token_h, pid, now, now + SESSION_TTL_SEC, now, ip, user_agent),
    )
    conn.commit()
    log_event(conn, actor_pid=pid, actor_ip=ip, action="session_create",
              payload={"ttl": SESSION_TTL_SEC})
    return token


def verify_session(conn, token: str, request_ip: str) -> Optional[str]:
    """Verify a session token. Returns pid if valid, None otherwise.

    IP binding: warns (but doesn't reject) if token used from a different IP,
    to allow mobile network changes.
    """
    if not token:
        return None
    token_h = hash_token(token)
    cur = conn.execute(
        """SELECT pid, expires_at, revoked, ip FROM sessions
           WHERE token_hash=?""",
        (token_h,),
    )
    row = cur.fetchone()
    if not row:
        log_event(conn, actor_ip=request_ip, action="session_verify",
                  severity="warn", success=False,
                  payload={"reason": "unknown_token"})
        return None
    pid, expires_at, revoked, ip = row
    if revoked:
        log_event(conn, actor_pid=pid, actor_ip=request_ip, action="session_verify",
                  severity="warn", success=False,
                  payload={"reason": "revoked"})
        return None
    if expires_at < time.time():
        log_event(conn, actor_pid=pid, actor_ip=request_ip, action="session_verify",
                  severity="info", success=False,
                  payload={"reason": "expired"})
        return None
    # Touch last_used
    conn.execute(
        "UPDATE sessions SET last_used=? WHERE token_hash=?",
        (time.time(), token_h),
    )
    conn.commit()
    if ip and request_ip and ip != request_ip:
        log_event(conn, actor_pid=pid, actor_ip=request_ip, action="session_ip_change",
                  severity="warn",
                  payload={"original_ip": ip, "new_ip": request_ip})
    return pid


def revoke_session(conn, token: str) -> bool:
    token_h = hash_token(token)
    cur = conn.execute(
        "UPDATE sessions SET revoked=1 WHERE token_hash=?",
        (token_h,),
    )
    conn.commit()
    return cur.rowcount > 0


# ---- Request ID (for log correlation) ---------------------------------------

def make_request_id() -> str:
    return "req-" + secrets.token_hex(8)
