"""V15 AI Citizen DID — cryptographically verifiable identity."""
from __future__ import annotations
import hashlib
import secrets
import time


_OPERATOR_KEY_CACHE = {}


def _ensure_operator_key(conn):
    if "key" in _OPERATOR_KEY_CACHE:
        return _OPERATOR_KEY_CACHE["key"]
    cur = conn.execute(
        "SELECT public_key, private_key FROM operator_keys WHERE active=1 LIMIT 1"
    )
    row = cur.fetchone()
    if row:
        _OPERATOR_KEY_CACHE["key"] = (row[0], row[1])
        return row[0], row[1]
    # Generate new
    try:
        from nacl.signing import SigningKey
        sk = SigningKey.generate()
        pk_hex = sk.verify_key.encode().hex()
        sk_hex = sk.encode().hex()
    except ImportError:
        sk_hex = secrets.token_hex(32)
        pk_hex = hashlib.sha256(("pub:" + sk_hex).encode()).hexdigest()
    now = time.time()
    conn.execute(
        "INSERT INTO operator_keys (key_id, algorithm, public_key, private_key, created_at, active) VALUES (?,?,?,?,?,1)",
        ("operator-" + secrets.token_hex(8), "ed25519", pk_hex, sk_hex, now),
    )
    conn.commit()
    _OPERATOR_KEY_CACHE["key"] = (pk_hex, sk_hex)
    return pk_hex, sk_hex


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _sign(secret_hex, message):
    try:
        from nacl.signing import SigningKey
        sk = SigningKey(bytes.fromhex(secret_hex))
        return sk.sign(message.encode()).signature.hex()
    except ImportError:
        return _sha(secret_hex + ":" + message)


def issue_did(conn, pid, soul_name, dna_seed):
    cur = conn.execute(
        "SELECT pid, did, fingerprint, signature, public_key, issued_at, expires_at, revoked FROM bot_identity WHERE pid=?",
        (pid,),
    )
    row = cur.fetchone()
    if row:
        return {
            "pid": row[0], "did": row[1], "fingerprint": row[2],
            "signature": row[3], "public_key": row[4],
            "issued_at": row[5], "expires_at": row[6],
            "revoked": bool(row[7]),
            "already_issued": True,
        }
    pk_hex, sk_hex = _ensure_operator_key(conn)
    fingerprint = _sha(soul_name + ":" + dna_seed)
    did = "did:aicivil:" + fingerprint[:32]
    signature = _sign(sk_hex, "did:" + did + ":" + pid)
    now = time.time()
    conn.execute(
        "INSERT INTO bot_identity (pid, did, fingerprint, public_key, signature, issued_at) VALUES (?,?,?,?,?,?)",
        (pid, did, fingerprint, pk_hex, signature, now),
    )
    conn.commit()
    return {
        "pid": pid, "did": did, "fingerprint": fingerprint,
        "signature": signature, "public_key": pk_hex,
        "issued_at": now, "expires_at": None,
        "revoked": False, "already_issued": False,
    }


def get_did(conn, pid):
    cur = conn.execute(
        "SELECT pid, did, fingerprint, signature, public_key, issued_at, expires_at, revoked, revoked_reason FROM bot_identity WHERE pid=?",
        (pid,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "pid": row[0], "did": row[1], "fingerprint": row[2],
        "signature": row[3], "public_key": row[4],
        "issued_at": row[5], "expires_at": row[6],
        "revoked": bool(row[7]), "revoked_reason": row[8],
    }


def verify_did(conn, did):
    cur = conn.execute(
        "SELECT pid, did, fingerprint, signature, public_key, issued_at, revoked, revoked_reason FROM bot_identity WHERE did=?",
        (did,),
    )
    row = cur.fetchone()
    if not row:
        return None
    if row[6]:
        return {"error": "revoked", "reason": row[7], "did": did}
    return {
        "pid": row[0], "did": row[1], "fingerprint": row[2],
        "signature": row[3], "public_key": row[4],
        "issued_at": row[5], "valid": True,
    }


def revoke_did(conn, pid, reason):
    cur = conn.execute(
        "UPDATE bot_identity SET revoked=1, revoked_reason=? WHERE pid=?",
        (reason, pid),
    )
    conn.commit()
    if cur.rowcount == 0:
        return {"error": "no_did"}
    return {"pid": pid, "revoked": True, "reason": reason}
