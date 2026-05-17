#!/usr/bin/env python3
import csv
import hmac
import json
import os
import re
import secrets
import smtplib
import sqlite3
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent


def load_env_file(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ROOT / ".env")

DB_PATH = Path(os.environ.get("BOOKING_DB_PATH", ROOT / "bookings.sqlite3"))
SLOT_MINUTES = 15
VALID_TIMES = [
    *(f"{hour:02d}:{minute:02d}" for hour in range(9, 12) for minute in range(0, 60, SLOT_MINUTES)),
    *(f"{hour:02d}:{minute:02d}" for hour in range(13, 17) for minute in range(0, 60, SLOT_MINUTES)),
]
BOOKING_START_DATE = date(2026, 5, 20)
BOOKING_END_DATE = date(2026, 5, 22)
VALID_WEEKDAYS = (2, 3, 4)
WEEKDAY_LABEL = "周三、周四、周五"
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "aiad-admin-2026")
ADMIN_SESSION_COOKIE = "aiad_admin_session"
ADMIN_SESSION_TOKEN = secrets.token_urlsafe(32)
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "0") or "0")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
SMTP_USE_SSL = os.environ.get("SMTP_USE_SSL", "1").lower() not in ("0", "false", "no")
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "0").lower() in ("1", "true", "yes")
VERIFICATION_TTL_MINUTES = int(os.environ.get("VERIFICATION_TTL_MINUTES", "10"))
EMAIL_SESSION_TTL_HOURS = int(os.environ.get("EMAIL_SESSION_TTL_HOURS", "12"))
MAX_VERIFICATION_ATTEMPTS = 5
TEST_LOCATION = "重庆大学A区校医院四楼阿尔兹海默症样机测试"


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                mentor TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, time)
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(bookings)").fetchall()}
        if "mentor" not in columns:
            conn.execute("ALTER TABLE bookings ADD COLUMN mentor TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_date_time ON bookings(date, time)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blocked_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, time)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_blocked_slots_date_time ON blocked_slots(date, time)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_verifications (
                email TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                verified_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                sent_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_sessions (
                token TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_sessions_email ON email_sessions(lower(email))")
        conn.execute("DROP INDEX IF EXISTS idx_bookings_email_unique")
        conn.execute(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_email_window_unique
            ON bookings(lower(email))
            WHERE date BETWEEN '{BOOKING_START_DATE.isoformat()}' AND '{BOOKING_END_DATE.isoformat()}'
            """
        )


def row_to_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "mentor": row["mentor"],
        "email": row["email"],
        "date": row["date"],
        "time": row["time"],
        "createdAt": row["created_at"],
    }


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def validate_booking(payload):
    name = str(payload.get("name", "")).strip()
    mentor = str(payload.get("mentor", "")).strip()
    email = normalize_email(payload.get("email", ""))
    booking_date = str(payload.get("date", "")).strip()
    booking_time = str(payload.get("time", "")).strip()
    parsed = parse_date(booking_date)

    if not name:
        return None, "请输入姓名"
    if not mentor:
        return None, "请输入导师"
    if not EMAIL_RE.match(email):
        return None, "请输入有效邮箱"
    if not parsed or parsed.weekday() not in VALID_WEEKDAYS:
        return None, f"只能预约{WEEKDAY_LABEL}"
    if parsed < BOOKING_START_DATE or parsed > BOOKING_END_DATE:
        return None, "只能预约 2026-05-20 至 2026-05-22 的时间段"
    if booking_time not in VALID_TIMES:
        return None, f"请选择有效的 {SLOT_MINUTES} 分钟时间窗"

    return {"name": name, "mentor": mentor, "email": email, "date": booking_date, "time": booking_time}, None


def validate_booking_slot(payload):
    email = normalize_email(payload.get("email", ""))
    booking_date = str(payload.get("date", "")).strip()
    booking_time = str(payload.get("time", "")).strip()
    parsed = parse_date(booking_date)

    if not EMAIL_RE.match(email):
        return None, "请输入有效邮箱"
    if not parsed or parsed.weekday() not in VALID_WEEKDAYS:
        return None, f"只能预约{WEEKDAY_LABEL}"
    if parsed < BOOKING_START_DATE or parsed > BOOKING_END_DATE:
        return None, "只能预约 2026-05-20 至 2026-05-22 的时间段"
    if booking_time not in VALID_TIMES:
        return None, f"请选择有效的 {SLOT_MINUTES} 分钟时间窗"

    return {"email": email, "date": booking_date, "time": booking_time}, None


def validate_slot(payload):
    booking_date = str(payload.get("date", "")).strip()
    booking_time = str(payload.get("time", "")).strip()
    parsed = parse_date(booking_date)

    if not parsed or parsed.weekday() not in VALID_WEEKDAYS:
        return None, f"只能选择{WEEKDAY_LABEL}"
    if parsed < BOOKING_START_DATE or parsed > BOOKING_END_DATE:
        return None, "只能选择 2026-05-20 至 2026-05-22 的时间段"
    if booking_time not in VALID_TIMES:
        return None, f"请选择有效的 {SLOT_MINUTES} 分钟时间窗"

    return {"date": booking_date, "time": booking_time}, None


def normalize_email(value):
    return str(value).strip().lower()


def format_dt(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def send_email_message(message):
    missing = [
        name
        for name, value in (
            ("SMTP_HOST", SMTP_HOST),
            ("SMTP_PORT", SMTP_PORT),
            ("SMTP_USER", SMTP_USER),
            ("SMTP_PASSWORD", SMTP_PASSWORD),
            ("SMTP_FROM", SMTP_FROM),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"邮箱服务器未配置 {', '.join(missing)}")

    smtp_class = smtplib.SMTP_SSL if SMTP_USE_SSL else smtplib.SMTP
    with smtp_class(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        if SMTP_STARTTLS and not SMTP_USE_SSL:
            smtp.starttls()
        if SMTP_USER:
            smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(message)


def send_verification_email(email, code):
    if not SMTP_PASSWORD:
        raise RuntimeError("邮箱服务器未配置 SMTP_PASSWORD")

    message = EmailMessage()
    message["Subject"] = "AIAD 样机测试预约验证码"
    message["From"] = SMTP_FROM
    message["To"] = email
    message.set_content(
        "\n".join(
            [
                "您好，",
                "",
                f"您的 AIAD 样机测试预约验证码是：{code}",
                f"验证码 {VERIFICATION_TTL_MINUTES} 分钟内有效。若不是您本人操作，请忽略本邮件。",
                "",
                "AIAD 预约系统",
            ]
        )
    )
    send_email_message(message)


def send_booking_confirmation_email(booking):
    message = EmailMessage()
    message["Subject"] = "AIAD 样机测试预约成功"
    message["From"] = SMTP_FROM
    message["To"] = booking["email"]
    message.set_content(
        "\n".join(
            [
                f"{booking['name']}，您好：",
                "",
                "您的 AIAD 样机测试预约已成功。",
                "",
                f"导师：{booking['mentor']}",
                f"预约日期：{booking['date']}",
                f"预约时间：{booking['time']} - {add_minutes(booking['time'], SLOT_MINUTES)}",
                f"测试地点：{TEST_LOCATION}",
                "",
                "如需调整预约，请回到预约页并通过邮箱验证码修改预约时间。",
                "",
                "AIAD 预约系统",
            ]
        )
    )
    send_email_message(message)


def send_booking_update_email(booking):
    message = EmailMessage()
    message["Subject"] = "AIAD 样机测试预约时间已修改"
    message["From"] = SMTP_FROM
    message["To"] = booking["email"]
    message.set_content(
        "\n".join(
            [
                f"{booking['name']}，您好：",
                "",
                "您的 AIAD 样机测试预约时间已修改。",
                "",
                f"导师：{booking['mentor']}",
                f"新的预约日期：{booking['date']}",
                f"新的预约时间：{booking['time']} - {add_minutes(booking['time'], SLOT_MINUTES)}",
                f"测试地点：{TEST_LOCATION}",
                "",
                "AIAD 预约系统",
            ]
        )
    )
    send_email_message(message)


def verification_is_valid(conn, email):
    row = conn.execute(
        """
        SELECT verified_at, expires_at
        FROM email_verifications
        WHERE lower(email) = lower(?)
        """,
        (email,),
    ).fetchone()
    if not row or not row["verified_at"]:
        return False
    expires_at = parse_dt(row["expires_at"])
    return bool(expires_at and expires_at >= datetime.now())


def create_email_session(conn, email):
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires_at = now + timedelta(hours=EMAIL_SESSION_TTL_HOURS)
    conn.execute("DELETE FROM email_sessions WHERE expires_at < ?", (format_dt(now),))
    conn.execute(
        """
        INSERT INTO email_sessions (token, email, expires_at, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (token, email, format_dt(expires_at), format_dt(now)),
    )
    return token, expires_at


def email_session_is_valid(conn, email, token):
    if not token:
        return False
    row = conn.execute(
        """
        SELECT expires_at
        FROM email_sessions
        WHERE token = ? AND lower(email) = lower(?)
        """,
        (token, email),
    ).fetchone()
    if not row:
        return False
    expires_at = parse_dt(row["expires_at"])
    return bool(expires_at and expires_at >= datetime.now())


def email_is_authorized(conn, email, token):
    return email_session_is_valid(conn, email, token) or verification_is_valid(conn, email)


def find_current_window_booking(conn, email):
    return conn.execute(
        """
        SELECT id, name, mentor, email, date, time, created_at
        FROM bookings
        WHERE lower(email) = lower(?)
          AND date BETWEEN ? AND ?
        ORDER BY date, time
        LIMIT 1
        """,
        (email, BOOKING_START_DATE.isoformat(), BOOKING_END_DATE.isoformat()),
    ).fetchone()


def slot_is_blocked(conn, booking_date, booking_time):
    return conn.execute(
        "SELECT 1 FROM blocked_slots WHERE date = ? AND time = ?",
        (booking_date, booking_time),
    ).fetchone() is not None


def slot_has_booking(conn, booking_date, booking_time):
    return conn.execute(
        "SELECT 1 FROM bookings WHERE date = ? AND time = ?",
        (booking_date, booking_time),
    ).fetchone() is not None


def booking_dates():
    current = BOOKING_START_DATE
    dates = []
    while current <= BOOKING_END_DATE:
        dates.append(current)
        current += timedelta(days=1)
    return dates


def query_dates(query):
    start = parse_date(query.get("startDate", [""])[0])
    end = parse_date(query.get("endDate", [""])[0])
    if not start or not end:
        return booking_dates()
    if end < start:
        start, end = end, start
    start = max(start, BOOKING_START_DATE)
    end = min(end, BOOKING_END_DATE)
    if (end - start).days > 62:
        end = start + timedelta(days=62)
    days = []
    current = start
    while current <= end:
        if current.weekday() in VALID_WEEKDAYS:
            days.append(current)
        current += timedelta(days=1)
    return days


def query_times(query):
    selected = query.get("time", ["all"])[0]
    if selected == "morning":
        return [item for item in VALID_TIMES if item < "12:00"]
    if selected == "afternoon":
        return [item for item in VALID_TIMES if item >= "13:00"]
    if selected in VALID_TIMES:
        return [selected]
    return VALID_TIMES


def build_schedule_rows(query):
    dates = query_dates(query)
    times = query_times(query)
    status = query.get("status", ["all"])[0]
    search = query.get("search", [""])[0].strip().lower()
    keys = [item.isoformat() for item in dates]

    bookings_by_slot = {}
    blocked_slots = set()
    if keys:
        placeholders = ",".join("?" for _ in keys)
        with connect() as conn:
            booking_rows = conn.execute(
                f"""
                SELECT id, name, mentor, email, date, time, created_at
                FROM bookings
                WHERE date IN ({placeholders})
                ORDER BY date, time
                """,
                keys,
            ).fetchall()
            blocked_rows = conn.execute(
                f"""
                SELECT date, time
                FROM blocked_slots
                WHERE date IN ({placeholders})
                ORDER BY date, time
                """,
                keys,
            ).fetchall()
        bookings_by_slot = {(row["date"], row["time"]): row for row in booking_rows}
        blocked_slots = {(row["date"], row["time"]) for row in blocked_rows}

    csv_rows = []
    for current in dates:
        for booking_time in times:
            slot_key = (current.isoformat(), booking_time)
            row = bookings_by_slot.get(slot_key)
            is_booked = row is not None
            is_blocked = slot_key in blocked_slots
            if status == "booked" and not is_booked:
                continue
            if status == "blocked" and (is_booked or not is_blocked):
                continue
            if status == "available" and (is_booked or is_blocked):
                continue
            if search and (not row or search not in f"{row['name']} {row['mentor']} {row['email']}".lower()):
                continue
            csv_rows.append(
                {
                    "日期": current.isoformat(),
                    "星期": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][current.weekday()],
                    "时间段": f"{booking_time}-{add_minutes(booking_time, SLOT_MINUTES)}",
                    "状态": "已预约" if row else ("已占用" if is_blocked else "可预约"),
                    "姓名": row["name"] if row else "",
                    "导师": row["mentor"] if row else "",
                    "邮箱": row["email"] if row else "",
                    "提交时间": row["created_at"] if row else "",
                }
            )
    return csv_rows


class BookingHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/admin/session":
            self.admin_session()
            return
        if parsed.path == "/api/availability":
            self.list_availability()
            return
        if parsed.path == "/api/bookings":
            if not self.require_admin():
                return
            self.list_bookings()
            return
        if parsed.path == "/api/blocked-slots":
            if not self.require_admin():
                return
            self.list_blocked_slots()
            return
        if parsed.path == "/api/export.csv":
            if not self.require_admin():
                return
            self.export_csv(parse_qs(parsed.query))
            return
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/admin/login":
            self.admin_login()
            return
        if path == "/api/admin/logout":
            self.admin_logout()
            return
        if path == "/api/verification/send":
            self.send_email_code()
            return
        if path == "/api/verification/verify":
            self.verify_email_code()
            return
        if path == "/api/bookings":
            self.create_booking()
            return
        if path == "/api/blocked-slots":
            if not self.require_admin():
                return
            self.create_blocked_slot()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PUT(self):
        path = urlparse(self.path).path
        if path == "/api/bookings/by-email":
            self.update_booking_by_email()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path == "/api/bookings/by-email":
            self.cancel_booking_by_email()
            return
        if path.startswith("/api/bookings/"):
            if not self.require_admin():
                return
            self.delete_booking(path.rsplit("/", 1)[-1])
            return
        if path == "/api/blocked-slots":
            if not self.require_admin():
                return
            self.delete_blocked_slot()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def is_admin_authenticated(self):
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return False
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get(ADMIN_SESSION_COOKIE)
        return bool(morsel and hmac.compare_digest(morsel.value, ADMIN_SESSION_TOKEN))

    def require_admin(self):
        if self.is_admin_authenticated():
            return True
        self.send_json({"error": "请先登录后台"}, HTTPStatus.UNAUTHORIZED)
        return False

    def set_admin_cookie(self):
        secure = " Secure;" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        self.send_header(
            "Set-Cookie",
            f"{ADMIN_SESSION_COOKIE}={ADMIN_SESSION_TOKEN}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800;{secure}",
        )

    def clear_admin_cookie(self):
        self.send_header(
            "Set-Cookie",
            f"{ADMIN_SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
        )

    def admin_session(self):
        self.send_json({"authenticated": self.is_admin_authenticated()})

    def admin_login(self):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        password = str(payload.get("password", ""))
        if not hmac.compare_digest(password, ADMIN_PASSWORD):
            self.send_json({"error": "后台密码错误"}, HTTPStatus.UNAUTHORIZED)
            return
        body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.set_admin_cookie()
        self.end_headers()
        self.wfile.write(body)

    def admin_logout(self):
        body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.clear_admin_cookie()
        self.end_headers()
        self.wfile.write(body)

    def list_availability(self):
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT date, time
                FROM bookings
                WHERE date BETWEEN ? AND ?
                UNION
                SELECT date, time
                FROM blocked_slots
                WHERE date BETWEEN ? AND ?
                ORDER BY date, time
                """,
                (
                    BOOKING_START_DATE.isoformat(),
                    BOOKING_END_DATE.isoformat(),
                    BOOKING_START_DATE.isoformat(),
                    BOOKING_END_DATE.isoformat(),
                ),
            ).fetchall()
        self.send_json([{"date": row["date"], "time": row["time"]} for row in rows])

    def list_bookings(self):
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, name, mentor, email, date, time, created_at FROM bookings ORDER BY date, time"
            ).fetchall()
        self.send_json([row_to_dict(row) for row in rows])

    def list_blocked_slots(self):
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT id, date, time, created_at
                FROM blocked_slots
                WHERE date BETWEEN ? AND ?
                ORDER BY date, time
                """,
                (BOOKING_START_DATE.isoformat(), BOOKING_END_DATE.isoformat()),
            ).fetchall()
        self.send_json(
            [
                {
                    "id": row["id"],
                    "date": row["date"],
                    "time": row["time"],
                    "createdAt": row["created_at"],
                }
                for row in rows
            ]
        )

    def send_email_code(self):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return

        email = normalize_email(payload.get("email", ""))
        if not EMAIL_RE.match(email):
            self.send_json({"error": "请输入有效邮箱"}, HTTPStatus.BAD_REQUEST)
            return

        now = datetime.now()
        code = f"{secrets.randbelow(1_000_000):06d}"
        try:
            send_verification_email(email, code)
        except Exception as exc:
            self.send_json({"error": f"验证码邮件发送失败：{exc}"}, HTTPStatus.BAD_GATEWAY)
            return

        expires_at = now + timedelta(minutes=VERIFICATION_TTL_MINUTES)
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO email_verifications (email, code, expires_at, verified_at, attempts, sent_at)
                VALUES (?, ?, ?, NULL, 0, ?)
                ON CONFLICT(email) DO UPDATE SET
                    code = excluded.code,
                    expires_at = excluded.expires_at,
                    verified_at = NULL,
                    attempts = 0,
                    sent_at = excluded.sent_at
                """,
                (email, code, format_dt(expires_at), format_dt(now)),
            )
        self.send_json({"ok": True, "message": "验证码已发送，请检查邮箱。"})

    def verify_email_code(self):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return

        email = normalize_email(payload.get("email", ""))
        code = str(payload.get("code", "")).strip()
        if not EMAIL_RE.match(email):
            self.send_json({"error": "请输入有效邮箱"}, HTTPStatus.BAD_REQUEST)
            return
        if not re.fullmatch(r"\d{6}", code):
            self.send_json({"error": "请输入 6 位验证码"}, HTTPStatus.BAD_REQUEST)
            return

        now = datetime.now()
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT code, expires_at, attempts
                FROM email_verifications
                WHERE lower(email) = lower(?)
                """,
                (email,),
            ).fetchone()
            if not row:
                self.send_json({"error": "请先发送验证码"}, HTTPStatus.BAD_REQUEST)
                return
            expires_at = parse_dt(row["expires_at"])
            if not expires_at or expires_at < now:
                self.send_json({"error": "验证码已过期，请重新发送。"}, HTTPStatus.BAD_REQUEST)
                return
            if row["attempts"] >= MAX_VERIFICATION_ATTEMPTS:
                self.send_json({"error": "验证码错误次数过多，请重新发送。"}, HTTPStatus.TOO_MANY_REQUESTS)
                return
            if not hmac.compare_digest(row["code"], code):
                conn.execute(
                    "UPDATE email_verifications SET attempts = attempts + 1 WHERE lower(email) = lower(?)",
                    (email,),
                )
                self.send_json({"error": "验证码不正确"}, HTTPStatus.BAD_REQUEST)
                return
            conn.execute(
                "UPDATE email_verifications SET verified_at = ? WHERE lower(email) = lower(?)",
                (format_dt(now), email),
            )
            verification_token, verification_expires_at = create_email_session(conn, email)
            current_booking = find_current_window_booking(conn, email)
        response = {
            "ok": True,
            "message": "邮箱验证通过。",
            "verificationToken": verification_token,
            "verificationExpiresAt": format_dt(verification_expires_at),
        }
        if current_booking:
            response["currentBooking"] = {
                "date": current_booking["date"],
                "time": current_booking["time"],
            }
        self.send_json(response)

    def create_booking(self):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return

        booking, error = validate_booking(payload)
        if error:
            self.send_json({"error": error}, HTTPStatus.BAD_REQUEST)
            return

        try:
            with connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing_booking = find_current_window_booking(conn, booking["email"])
                if existing_booking:
                    self.send_json(
                        {
                            "error": f"该邮箱已预约 {existing_booking['date']} {existing_booking['time']}，不能重复预约。"
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                if not email_is_authorized(conn, booking["email"], str(payload.get("verificationToken", ""))):
                    self.send_json({"error": "请先完成邮箱验证码验证。"}, HTTPStatus.FORBIDDEN)
                    return
                if slot_is_blocked(conn, booking["date"], booking["time"]):
                    self.send_json({"error": "该时间已被后台占用，请选择其他时间。"}, HTTPStatus.CONFLICT)
                    return
                cursor = conn.execute(
                    """
                    INSERT INTO bookings (name, mentor, email, date, time, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        booking["name"],
                        booking["mentor"],
                        booking["email"],
                        booking["date"],
                        booking["time"],
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                row = conn.execute(
                    "SELECT id, name, mentor, email, date, time, created_at FROM bookings WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
        except sqlite3.IntegrityError:
            self.send_json({"error": "该时间已被其他人预约，请选择其他时间。"}, HTTPStatus.CONFLICT)
            return

        response = row_to_dict(row)
        try:
            send_booking_confirmation_email(response)
        except Exception as exc:
            response["emailWarning"] = f"预约已保存，但确认邮件发送失败：{exc}"
        self.send_json(response, HTTPStatus.CREATED)

    def update_booking_by_email(self):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return

        slot, error = validate_booking_slot(payload)
        if error:
            self.send_json({"error": error}, HTTPStatus.BAD_REQUEST)
            return

        try:
            with connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if not email_is_authorized(conn, slot["email"], str(payload.get("verificationToken", ""))):
                    self.send_json({"error": "请先完成邮箱验证码验证。"}, HTTPStatus.FORBIDDEN)
                    return

                existing_booking = find_current_window_booking(conn, slot["email"])
                if not existing_booking:
                    self.send_json({"error": "该邮箱当前没有可修改的预约。"}, HTTPStatus.NOT_FOUND)
                    return

                if existing_booking["date"] == slot["date"] and existing_booking["time"] == slot["time"]:
                    self.send_json({"error": "请选择一个新的预约时间。"}, HTTPStatus.BAD_REQUEST)
                    return
                if slot_is_blocked(conn, slot["date"], slot["time"]):
                    self.send_json({"error": "该时间已被后台占用，请选择其他时间。"}, HTTPStatus.CONFLICT)
                    return

                conn.execute(
                    "UPDATE bookings SET date = ?, time = ? WHERE id = ?",
                    (slot["date"], slot["time"], existing_booking["id"]),
                )
                row = conn.execute(
                    "SELECT id, name, mentor, email, date, time, created_at FROM bookings WHERE id = ?",
                    (existing_booking["id"],),
                ).fetchone()
        except sqlite3.IntegrityError:
            self.send_json({"error": "该时间已被其他人预约，请选择其他时间。"}, HTTPStatus.CONFLICT)
            return

        response = row_to_dict(row)
        try:
            send_booking_update_email(response)
        except Exception as exc:
            response["emailWarning"] = f"预约时间已修改，但通知邮件发送失败：{exc}"
        self.send_json(response)

    def cancel_booking_by_email(self):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return

        email = normalize_email(payload.get("email", ""))
        if not EMAIL_RE.match(email):
            self.send_json({"error": "请输入有效邮箱"}, HTTPStatus.BAD_REQUEST)
            return

        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not email_is_authorized(conn, email, str(payload.get("verificationToken", ""))):
                self.send_json({"error": "请先完成邮箱验证码验证。"}, HTTPStatus.FORBIDDEN)
                return
            existing_booking = find_current_window_booking(conn, email)
            if not existing_booking:
                self.send_json({"error": "该邮箱当前没有可取消的预约。"}, HTTPStatus.NOT_FOUND)
                return
            conn.execute("DELETE FROM bookings WHERE id = ?", (existing_booking["id"],))
        self.send_json({"ok": True})

    def create_blocked_slot(self):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return

        slot, error = validate_slot(payload)
        if error:
            self.send_json({"error": error}, HTTPStatus.BAD_REQUEST)
            return

        try:
            with connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if slot_has_booking(conn, slot["date"], slot["time"]):
                    self.send_json({"error": "该时间已有预约，不能占用。"}, HTTPStatus.CONFLICT)
                    return
                cursor = conn.execute(
                    """
                    INSERT INTO blocked_slots (date, time, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (slot["date"], slot["time"], format_dt(datetime.now())),
                )
                row = conn.execute(
                    "SELECT id, date, time, created_at FROM blocked_slots WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
        except sqlite3.IntegrityError:
            self.send_json({"error": "该时间已被占用。"}, HTTPStatus.CONFLICT)
            return

        self.send_json(
            {
                "id": row["id"],
                "date": row["date"],
                "time": row["time"],
                "createdAt": row["created_at"],
            },
            HTTPStatus.CREATED,
        )

    def delete_blocked_slot(self):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return

        slot, error = validate_slot(payload)
        if error:
            self.send_json({"error": error}, HTTPStatus.BAD_REQUEST)
            return

        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if slot_has_booking(conn, slot["date"], slot["time"]):
                self.send_json({"error": "该时间已有预约，不能释放。"}, HTTPStatus.CONFLICT)
                return
            cursor = conn.execute(
                "DELETE FROM blocked_slots WHERE date = ? AND time = ?",
                (slot["date"], slot["time"]),
            )
        if cursor.rowcount == 0:
            self.send_json({"error": "该时间没有后台占用记录。"}, HTTPStatus.NOT_FOUND)
            return
        self.send_json({"ok": True})

    def delete_booking(self, booking_id):
        if not booking_id.isdigit():
            self.send_json({"error": "预约编号无效"}, HTTPStatus.BAD_REQUEST)
            return
        with connect() as conn:
            cursor = conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        if cursor.rowcount == 0:
            self.send_json({"error": "预约不存在"}, HTTPStatus.NOT_FOUND)
            return
        self.send_json({"ok": True})

    def export_csv(self, query):
        csv_rows = build_schedule_rows(query)
        body = make_csv(csv_rows).encode("utf-8-sig")
        start = query.get("startDate", [""])[0] or "schedule"
        end = query.get("endDate", [""])[0]
        filename = f"aiad-bookings-{start}{('-' + end) if end and end != start else ''}.csv"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def add_minutes(time_value, minutes):
    hour, minute = [int(part) for part in time_value.split(":")]
    total = hour * 60 + minute + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


def make_csv(rows):
    from io import StringIO

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=["日期", "星期", "时间段", "状态", "姓名", "导师", "邮箱", "提交时间"])
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def main():
    init_db()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), BookingHandler)
    print(f"AIAD booking server running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
