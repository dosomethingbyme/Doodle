"""Environment-backed application settings."""

import os
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ROOT / ".env")

DB_PATH = Path(os.environ.get("BOOKING_DB_PATH", ROOT / "bookings.sqlite3"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "aiad-admin-2026")
ADMIN_PASSWORD_IS_DEFAULT = ADMIN_PASSWORD == "aiad-admin-2026"
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")
ADMIN_SESSION_HOURS = int(os.environ.get("ADMIN_SESSION_HOURS", "8"))
ADMIN_SESSION_COOKIE = "booking_admin_session"
ADMIN_LOGIN_FAILURES = {}
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "0") or "0")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
SMTP_USE_SSL = os.environ.get("SMTP_USE_SSL", "1").lower() not in ("0", "false", "no")
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "0").lower() in ("1", "true", "yes")
VERIFICATION_TTL_MINUTES = int(os.environ.get("VERIFICATION_TTL_MINUTES", "10"))
EMAIL_SESSION_TTL_HOURS = int(os.environ.get("EMAIL_SESSION_TTL_HOURS", "12"))
DEV_VERIFICATION_CODE = os.environ.get("DEV_VERIFICATION_CODE", "").strip()
MAX_VERIFICATION_ATTEMPTS = 5
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
PUBLIC_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
VALID_STATUSES = {"draft", "published", "paused", "ended", "archived"}
PUBLIC_STATUSES = {"published", "paused", "ended"}
STATUS_TRANSITIONS = {
    "draft": {"published", "archived"},
    "published": {"paused", "ended"},
    "paused": {"published", "ended"},
    "ended": {"archived"},
    "archived": {"ended"},
}
