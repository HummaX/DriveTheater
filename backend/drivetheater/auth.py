from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from .db import Database


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return base64.urlsafe_b64encode(salt + digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(stored.encode())
        return hmac.compare_digest(raw[16:], hashlib.scrypt(password.encode(), salt=raw[:16], n=2**14, r=8, p=1))
    except (ValueError, TypeError):
        return False


def ensure_admin(db: Database, username: str, password: str | None) -> None:
    with db.transaction() as conn:
        exists = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if not exists and password:
            conn.execute("INSERT INTO users VALUES (?,?,?)", (username, hash_password(password), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))


def make_session(db: Database, username: str) -> str:
    token = secrets.token_urlsafe(32)
    with db.transaction() as conn:
        conn.execute("INSERT INTO sessions VALUES (?,?,?)", (session_hash(token), username, int(time.time()) + 7 * 86400))
    return token




def session_user(db: Database, token: str | None) -> str | None:
    if not token:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT username FROM sessions WHERE token_hash=? AND expires_at>?",
            (session_hash(token), int(time.time())),
        ).fetchone()
    return row["username"] if row else None


def session_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def revoke_session(db: Database, token: str | None) -> None:
    if not token:
        return
    with db.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (session_hash(token),))


def revoke_user_sessions(db: Database, username: str) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE username=?", (username,))
