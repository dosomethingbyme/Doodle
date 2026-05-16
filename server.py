#!/usr/bin/env python3
import csv
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
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
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


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
    email = str(payload.get("email", "")).strip()
    booking_date = str(payload.get("date", "")).strip()
    booking_time = str(payload.get("time", "")).strip()
    parsed = parse_date(booking_date)

    if not name:
        return None, "请输入姓名"
    if not EMAIL_RE.match(email):
        return None, "请输入有效邮箱"
    if not parsed or parsed.weekday() not in (1, 2, 3):
        return None, "只能预约周二、周三、周四"
    if booking_time not in VALID_TIMES:
        return None, "请选择有效的 20 分钟时间窗"

    return {"name": name, "email": email, "date": booking_date, "time": booking_time}, None


def first_tuesday(offset):
    today = date.today()
    days_to_tuesday = (1 - today.weekday()) % 7
    return today + timedelta(days=days_to_tuesday + offset * 7)


def week_dates(offset):
    start = first_tuesday(offset)
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
        if current.weekday() in (1, 2, 3):
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
        if parsed.path == "/api/bookings":
            self.list_bookings()
            return
        if parsed.path == "/api/export.csv":
            self.export_csv(parse_qs(parsed.query))
            return
        super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path == "/api/bookings":
            self.create_booking()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/bookings/"):
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

    def list_bookings(self):
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, name, email, date, time, created_at FROM bookings ORDER BY date, time"
            ).fetchall()
        self.send_json([row_to_dict(row) for row in rows])

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
