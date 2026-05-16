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
DB_PATH = Path(os.environ.get("BOOKING_DB_PATH", ROOT / "bookings.sqlite3"))
VALID_TIMES = [
    *(f"{hour:02d}:{minute:02d}" for hour in range(9, 12) for minute in (0, 20, 40)),
    *(f"{hour:02d}:{minute:02d}" for hour in range(14, 17) for minute in (0, 20, 40)),
]
VALID_WEEKDAYS = (2, 3, 4)
WEEKDAY_LABEL = "周三、周四、周五"
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "aiad-admin-2026")
ADMIN_SESSION_COOKIE = "aiad_admin_session"
ADMIN_SESSION_TOKEN = secrets.token_urlsafe(32)
SMTP_HOST = os.environ.get("SMTP_HOST", "your-smtp-host")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "your-smtp-login")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
SMTP_USE_SSL = os.environ.get("SMTP_USE_SSL", "1").lower() not in ("0", "false", "no")
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "0").lower() in ("1", "true", "yes")
VERIFICATION_TTL_MINUTES = int(os.environ.get("VERIFICATION_TTL_MINUTES", "10"))
VERIFICATION_RESEND_SECONDS = int(os.environ.get("VERIFICATION_RESEND_SECONDS", "60"))
MAX_VERIFICATION_ATTEMPTS = 5


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
                email TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, time)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_date_time ON bookings(date, time)")
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
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_email_unique ON bookings(lower(email))")
        except sqlite3.IntegrityError:
            pass


def row_to_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
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
    email = normalize_email(payload.get("email", ""))
    booking_date = str(payload.get("date", "")).strip()
    booking_time = str(payload.get("time", "")).strip()
    parsed = parse_date(booking_date)

    if not name:
        return None, "请输入姓名"
    if not EMAIL_RE.match(email):
        return None, "请输入有效邮箱"
    if not parsed or parsed.weekday() not in VALID_WEEKDAYS:
        return None, f"只能预约{WEEKDAY_LABEL}"
    if booking_time not in VALID_TIMES:
        return None, "请选择有效的 20 分钟时间窗"

    return {"name": name, "email": email, "date": booking_date, "time": booking_time}, None


def normalize_email(value):
    return str(value).strip().lower()


def format_dt(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


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

    smtp_class = smtplib.SMTP_SSL if SMTP_USE_SSL else smtplib.SMTP
    with smtp_class(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        if SMTP_STARTTLS and not SMTP_USE_SSL:
            smtp.starttls()
        if SMTP_USER:
            smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(message)


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


def first_bookable_day(offset):
    today = date.today()
    days_to_wednesday = (2 - today.weekday()) % 7
    return today + timedelta(days=days_to_wednesday + offset * 7)


def week_dates(offset):
    start = first_bookable_day(offset)
    return [start + timedelta(days=i) for i in range(3)]


def query_dates(query):
    start = parse_date(query.get("startDate", [""])[0])
    end = parse_date(query.get("endDate", [""])[0])
    if not start or not end:
        try:
            offset = int(query.get("weekOffset", ["0"])[0] or 0)
        except ValueError:
            offset = 0
        return week_dates(offset)
    if end < start:
        start, end = end, start
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
        return [item for item in VALID_TIMES if item >= "14:00"]
    if selected in VALID_TIMES:
        return [selected]
    return VALID_TIMES


def build_schedule_rows(query):
    dates = query_dates(query)
    times = query_times(query)
    status = query.get("status", ["all"])[0]
    search = query.get("search", [""])[0].strip().lower()
    keys = [item.isoformat() for item in dates]

    by_slot = {}
    if keys:
        placeholders = ",".join("?" for _ in keys)
        with connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, name, email, date, time, created_at
                FROM bookings
                WHERE date IN ({placeholders})
                ORDER BY date, time
                """,
                keys,
            ).fetchall()
        by_slot = {(row["date"], row["time"]): row for row in rows}

    csv_rows = []
    for current in dates:
        for booking_time in times:
            row = by_slot.get((current.isoformat(), booking_time))
            is_booked = row is not None
            if status == "booked" and not is_booked:
                continue
            if status == "available" and is_booked:
                continue
            if search and (not row or search not in f"{row['name']} {row['email']}".lower()):
                continue
            csv_rows.append(
                {
                    "日期": current.isoformat(),
                    "星期": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][current.weekday()],
                    "时间段": f"{booking_time}-{add_minutes(booking_time, 20)}",
                    "状态": "已预约" if row else "可预约",
                    "姓名": row["name"] if row else "",
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
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/bookings/"):
            if not self.require_admin():
                return
            self.delete_booking(path.rsplit("/", 1)[-1])
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
            rows = conn.execute("SELECT date, time FROM bookings ORDER BY date, time").fetchall()
        self.send_json([{"date": row["date"], "time": row["time"]} for row in rows])

    def list_bookings(self):
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, name, email, date, time, created_at FROM bookings ORDER BY date, time"
            ).fetchall()
        self.send_json([row_to_dict(row) for row in rows])

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
        with connect() as conn:
            existing_booking = conn.execute(
                "SELECT date, time FROM bookings WHERE lower(email) = lower(?)",
                (email,),
            ).fetchone()
            if existing_booking:
                self.send_json(
                    {
                        "error": f"该邮箱已预约 {existing_booking['date']} {existing_booking['time']}，不能重复预约。"
                    },
                    HTTPStatus.CONFLICT,
                )
                return

            row = conn.execute(
                "SELECT sent_at FROM email_verifications WHERE lower(email) = lower(?)",
                (email,),
            ).fetchone()
            sent_at = parse_dt(row["sent_at"]) if row else None
            if sent_at and (now - sent_at).total_seconds() < VERIFICATION_RESEND_SECONDS:
                remaining = int(VERIFICATION_RESEND_SECONDS - (now - sent_at).total_seconds())
                self.send_json({"error": f"验证码已发送，请 {remaining} 秒后再试。"}, HTTPStatus.TOO_MANY_REQUESTS)
                return

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
        self.send_json({"ok": True, "message": "邮箱验证通过。"})

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
                existing_booking = conn.execute(
                    "SELECT date, time FROM bookings WHERE lower(email) = lower(?)",
                    (booking["email"],),
                ).fetchone()
                if existing_booking:
                    self.send_json(
                        {
                            "error": f"该邮箱已预约 {existing_booking['date']} {existing_booking['time']}，不能重复预约。"
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                if not verification_is_valid(conn, booking["email"]):
                    self.send_json({"error": "请先完成邮箱验证码验证。"}, HTTPStatus.FORBIDDEN)
                    return
                cursor = conn.execute(
                    """
                    INSERT INTO bookings (name, email, date, time, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        booking["name"],
                        booking["email"],
                        booking["date"],
                        booking["time"],
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                row = conn.execute(
                    "SELECT id, name, email, date, time, created_at FROM bookings WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
                conn.execute("DELETE FROM email_verifications WHERE lower(email) = lower(?)", (booking["email"],))
        except sqlite3.IntegrityError:
            self.send_json({"error": "该时间已被其他人预约，请选择其他时间。"}, HTTPStatus.CONFLICT)
            return

        self.send_json(row_to_dict(row), HTTPStatus.CREATED)

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
    writer = csv.DictWriter(output, fieldnames=["日期", "星期", "时间段", "状态", "姓名", "邮箱", "提交时间"])
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
