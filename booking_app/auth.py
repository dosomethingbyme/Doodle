"""Persistent administrator accounts and sessions."""

from datetime import datetime, timedelta

from .config import ADMIN_SESSION_HOURS
from .database import now_text
from .security import new_token, token_hash, verify_password

ROLE_LEVEL = {"operator": 1, "owner": 2}


def authenticate(conn, username, password):
    user = conn.execute(
        "SELECT * FROM admin_users WHERE username=? COLLATE NOCASE AND is_active=1",
        (str(username or "").strip(),),
    ).fetchone()
    if not user or not verify_password(str(password or ""), user["password_hash"]):
        return None
    conn.execute("UPDATE admin_users SET last_login_at=? WHERE id=?", (now_text(), user["id"]))
    return dict(user)


def create_session(conn, user_id):
    raw_token = new_token(32)
    csrf_token = new_token(24)
    created = datetime.now()
    expires = created + timedelta(hours=ADMIN_SESSION_HOURS)
    conn.execute(
        """INSERT INTO admin_sessions
        (token_hash,user_id,csrf_token,expires_at,created_at,last_seen_at)
        VALUES (?,?,?,?,?,?)""",
        (
            token_hash(raw_token),
            user_id,
            csrf_token,
            expires.strftime("%Y-%m-%d %H:%M:%S"),
            created.strftime("%Y-%m-%d %H:%M:%S"),
            created.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return raw_token, csrf_token, expires


def load_session(conn, raw_token):
    if not raw_token:
        return None
    row = conn.execute(
        """SELECT s.token_hash,s.csrf_token,s.expires_at,u.id user_id,u.username,u.role,u.is_active
        FROM admin_sessions s JOIN admin_users u ON u.id=s.user_id
        WHERE s.token_hash=?""",
        (token_hash(raw_token),),
    ).fetchone()
    if not row or not row["is_active"]:
        return None
    try:
        expires = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    if expires < datetime.now():
        conn.execute("DELETE FROM admin_sessions WHERE token_hash=?", (row["token_hash"],))
        return None
    conn.execute("UPDATE admin_sessions SET last_seen_at=? WHERE token_hash=?", (now_text(), row["token_hash"]))
    return dict(row)


def revoke_session(conn, raw_token):
    if raw_token:
        conn.execute("DELETE FROM admin_sessions WHERE token_hash=?", (token_hash(raw_token),))


def revoke_user_sessions(conn, user_id):
    conn.execute("DELETE FROM admin_sessions WHERE user_id=?", (user_id,))


def has_role(session, required_role):
    return bool(session and ROLE_LEVEL.get(session.get("role"), 0) >= ROLE_LEVEL.get(required_role, 99))


def cleanup_sessions(conn):
    conn.execute("DELETE FROM admin_sessions WHERE expires_at<?", (now_text(),))
