#!/usr/bin/env python3
import csv
import hmac
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent


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
ADMIN_SESSION_COOKIE = "booking_admin_session"
ADMIN_SESSION_TOKEN = secrets.token_urlsafe(32)
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


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def table_columns(conn, name):
    if not table_exists(conn, name):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({name})")}


def generate_public_id(conn=None):
    while True:
        value = "".join(secrets.choice(PUBLIC_ID_ALPHABET) for _ in range(8))
        if conn is None or conn.execute("SELECT 1 FROM tasks WHERE public_id=?", (value,)).fetchone() is None:
            return value


def create_task_tables(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL COLLATE NOCASE UNIQUE,
            name TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            contact TEXT NOT NULL DEFAULT '',
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            slot_minutes INTEGER NOT NULL DEFAULT 10,
            opening_strategy TEXT NOT NULL DEFAULT 'sequential',
            status TEXT NOT NULL DEFAULT 'draft',
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_dates (
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            PRIMARY KEY (task_id, date)
        );
        CREATE TABLE IF NOT EXISTS task_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS task_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            field_key TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL,
            field_type TEXT NOT NULL DEFAULT 'text',
            options_json TEXT NOT NULL DEFAULT '[]',
            required INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        """
    )


def create_default_task(conn):
    existing = conn.execute("SELECT id FROM tasks ORDER BY id LIMIT 1").fetchone()
    if existing:
        return existing["id"]
    timestamp = now_text()
    cursor = conn.execute(
        """
        INSERT INTO tasks (
            public_id, name, title, description, location, contact, timezone,
            slot_minutes, opening_strategy, status, is_default, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, '', 'Asia/Shanghai', 10, 'sequential', 'published', 1, ?, ?)
        """,
        (
            generate_public_id(conn),
            "AIAD 样机测试预约",
            "AIAD 样机测试时间预约",
            "请选择合适的测试时间，并填写真实联系信息。",
            "重庆大学A区校医院四楼阿尔兹海默症样机测试",
            timestamp,
            timestamp,
        ),
    )
    task_id = cursor.lastrowid
    conn.execute("INSERT INTO task_dates (task_id, date) VALUES (?, '2026-07-04')", (task_id,))
    conn.executemany(
        "INSERT INTO task_periods (task_id, label, start_time, end_time, sort_order) VALUES (?, ?, ?, ?, ?)",
        [(task_id, "上午", "09:30", "11:30", 0), (task_id, "下午", "13:00", "18:00", 1)],
    )
    conn.execute(
        "INSERT INTO task_fields (task_id, field_key, label, field_type, options_json, required, sort_order) VALUES (?, 'mentor', '导师', 'text', '[]', 1, 0)",
        (task_id,),
    )
    return task_id


def create_booking_table(conn):
    conn.executescript(
        """
        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
            name TEXT NOT NULL,
            email TEXT NOT NULL COLLATE NOCASE,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            answers_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'confirmed',
            cancelled_at TEXT,
            cancellation_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_bookings_task_date_time ON bookings(task_id, date, time);
        CREATE UNIQUE INDEX idx_bookings_active_slot_unique
            ON bookings(task_id, date, time) WHERE status='confirmed';
        CREATE UNIQUE INDEX idx_bookings_active_email_unique
            ON bookings(task_id, email) WHERE status='confirmed';
        """
    )


def create_blocked_table(conn):
    conn.executescript(
        """
        CREATE TABLE blocked_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(task_id, date, time)
        );
        CREATE INDEX idx_blocked_slots_task_date_time ON blocked_slots(task_id, date, time);
        """
    )


def create_legacy_task(conn, legacy_dates, legacy_times):
    timestamp = now_text()
    cursor = conn.execute(
        """
        INSERT INTO tasks (
            public_id, name, title, description, location, contact, timezone,
            slot_minutes, opening_strategy, status, is_default, created_at, updated_at
        ) VALUES (?, '历史预约（迁移）', '历史预约记录', '由旧版预约系统自动迁移。', '', '',
            'Asia/Shanghai', 10, 'any', 'archived', 0, ?, ?)
        """,
        (generate_public_id(conn), timestamp, timestamp),
    )
    task_id = cursor.lastrowid
    conn.executemany(
        "INSERT INTO task_dates (task_id, date) VALUES (?, ?)",
        [(task_id, value) for value in sorted(legacy_dates)],
    )
    valid_times = sorted(value for value in legacy_times if time_minutes(value) is not None)
    start = valid_times[0] if valid_times else "00:00"
    end = add_minutes(valid_times[-1], 10) if valid_times else "00:10"
    conn.execute(
        "INSERT INTO task_periods (task_id, label, start_time, end_time, sort_order) VALUES (?, '历史时段', ?, ?, 0)",
        (task_id, start, end),
    )
    conn.execute(
        "INSERT INTO task_fields (task_id, field_key, label, field_type, options_json, required, sort_order) VALUES (?, 'mentor', '导师', 'text', '[]', 0, 0)",
        (task_id,),
    )
    return task_id


def migrate_booking_tables(conn, default_task_id):
    booking_columns = table_columns(conn, "bookings")
    default_dates = {
        row["date"] for row in conn.execute("SELECT date FROM task_dates WHERE task_id=?", (default_task_id,)).fetchall()
    }
    legacy_task_id = None
    if booking_columns and "task_id" not in booking_columns:
        legacy_booking_rows = conn.execute("SELECT date, time FROM bookings").fetchall()
        legacy_blocked_rows = conn.execute("SELECT date, time FROM blocked_slots").fetchall() if table_exists(conn, "blocked_slots") else []
        outside_rows = [row for row in [*legacy_booking_rows, *legacy_blocked_rows] if row["date"] not in default_dates]
        if outside_rows:
            legacy_task_id = create_legacy_task(
                conn,
                {row["date"] for row in outside_rows},
                {row["time"] for row in outside_rows},
            )
    if not booking_columns:
        create_booking_table(conn)
    elif "task_id" not in booking_columns or "end_time" not in booking_columns or "status" not in booking_columns:
        conn.execute("DROP INDEX IF EXISTS idx_bookings_task_date_time")
        conn.execute("DROP INDEX IF EXISTS idx_bookings_active_slot_unique")
        conn.execute("DROP INDEX IF EXISTS idx_bookings_active_email_unique")
        conn.execute("ALTER TABLE bookings RENAME TO bookings_legacy")
        create_booking_table(conn)
        legacy_columns = table_columns(conn, "bookings_legacy")
        rows = conn.execute("SELECT * FROM bookings_legacy ORDER BY id").fetchall()
        for row in rows:
            mentor = row["mentor"] if "mentor" in legacy_columns else ""
            conn.execute(
                """
                INSERT INTO bookings (
                    id, task_id, name, email, date, time, end_time, answers_json,
                    status, cancelled_at, cancellation_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["task_id"] if "task_id" in legacy_columns else (default_task_id if row["date"] in default_dates else (legacy_task_id or default_task_id)),
                    row["name"],
                    row["email"],
                    row["date"],
                    row["time"],
                    row["end_time"] if "end_time" in legacy_columns else add_minutes(row["time"], 10),
                    row["answers_json"] if "answers_json" in legacy_columns else (json.dumps({"导师": mentor}, ensure_ascii=False) if mentor else "{}"),
                    row["status"] if "status" in legacy_columns else "confirmed",
                    row["cancelled_at"] if "cancelled_at" in legacy_columns else None,
                    row["cancellation_reason"] if "cancellation_reason" in legacy_columns else "",
                    row["created_at"],
                ),
            )
        conn.execute("DROP TABLE bookings_legacy")
    blocked_columns = table_columns(conn, "blocked_slots")
    if not blocked_columns:
        create_blocked_table(conn)
    elif "task_id" not in blocked_columns:
        conn.execute("ALTER TABLE blocked_slots RENAME TO blocked_slots_legacy")
        create_blocked_table(conn)
        for row in conn.execute("SELECT * FROM blocked_slots_legacy ORDER BY id").fetchall():
            conn.execute(
                "INSERT INTO blocked_slots (id, task_id, date, time, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    row["id"],
                    default_task_id if row["date"] in default_dates else (legacy_task_id or default_task_id),
                    row["date"], row["time"], row["created_at"],
                ),
            )
        conn.execute("DROP TABLE blocked_slots_legacy")


def migrate_task_fields(conn):
    columns = table_columns(conn, "task_fields")
    if "field_key" not in columns:
        conn.execute("ALTER TABLE task_fields ADD COLUMN field_key TEXT NOT NULL DEFAULT ''")
    if "field_type" not in columns:
        conn.execute("ALTER TABLE task_fields ADD COLUMN field_type TEXT NOT NULL DEFAULT 'text'")
    if "options_json" not in columns:
        conn.execute("ALTER TABLE task_fields ADD COLUMN options_json TEXT NOT NULL DEFAULT '[]'")
    fields = conn.execute("SELECT id, task_id, field_key, label FROM task_fields ORDER BY id").fetchall()
    for field in fields:
        if not field["field_key"]:
            conn.execute("UPDATE task_fields SET field_key=? WHERE id=?", (f"field_{field['id']}", field["id"]))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_task_fields_key_unique ON task_fields(task_id, field_key)")

    for booking in conn.execute("SELECT id, task_id, answers_json FROM bookings").fetchall():
        try:
            answers = json.loads(booking["answers_json"] or "{}")
        except json.JSONDecodeError:
            answers = {}
        changed = False
        task_fields = conn.execute(
            "SELECT field_key, label FROM task_fields WHERE task_id=?", (booking["task_id"],)
        ).fetchall()
        for field in task_fields:
            if field["field_key"] not in answers and field["label"] in answers:
                answers[field["field_key"]] = answers[field["label"]]
                changed = True
        if changed:
            conn.execute(
                "UPDATE bookings SET answers_json=? WHERE id=?",
                (json.dumps(answers, ensure_ascii=False), booking["id"]),
            )


def init_db():
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        legacy_schema = table_exists(conn, "bookings") and "task_id" not in table_columns(conn, "bookings")
        create_task_tables(conn)
        existing_task = conn.execute("SELECT id FROM tasks ORDER BY is_default DESC, id LIMIT 1").fetchone()
        default_task_id = existing_task["id"] if existing_task else (create_default_task(conn) if legacy_schema else None)
        migrate_booking_tables(conn, default_task_id)
        migrate_task_fields(conn)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS email_verifications (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                code TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                verified_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                sent_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS email_sessions (
                token TEXT PRIMARY KEY,
                email TEXT NOT NULL COLLATE NOCASE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_email_sessions_email ON email_sessions(email);
            """
        )


def parse_date(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def time_minutes(value):
    if not TIME_RE.match(str(value)):
        return None
    hour, minute = map(int, str(value).split(":"))
    return hour * 60 + minute


def add_minutes(value, amount):
    total = time_minutes(value)
    if total is None:
        return ""
    total += amount
    return f"{total // 60:02d}:{total % 60:02d}"


def normalize_email(value):
    return str(value or "").strip().lower()


def load_task(conn, task_id=None, public_id=None):
    if public_id is not None:
        task = conn.execute("SELECT * FROM tasks WHERE public_id=? COLLATE NOCASE", (public_id,)).fetchone()
    else:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        return None
    result = dict(task)
    result["dates"] = [
        row["date"] for row in conn.execute("SELECT date FROM task_dates WHERE task_id=? ORDER BY date", (task["id"],))
    ]
    result["periods"] = [
        dict(row)
        for row in conn.execute(
            "SELECT id, label, start_time, end_time, sort_order FROM task_periods WHERE task_id=? ORDER BY sort_order, start_time",
            (task["id"],),
        )
    ]
    result["fields"] = [
        dict(row)
        for row in conn.execute(
            "SELECT id, field_key, label, field_type, options_json, required, sort_order FROM task_fields WHERE task_id=? ORDER BY sort_order, id",
            (task["id"],),
        )
    ]
    return result


def task_payload(task, include_private=False):
    result = {
        "publicId": task["public_id"],
        "title": task["title"],
        "description": task["description"],
        "location": task["location"],
        "contact": task["contact"],
        "timezone": task["timezone"],
        "slotMinutes": task["slot_minutes"],
        "openingStrategy": task["opening_strategy"],
        "status": task["status"],
        "dates": task["dates"],
        "periods": [
            {
                "id": period["id"],
                "label": period["label"],
                "start": period["start_time"],
                "end": period["end_time"],
            }
            for period in task["periods"]
        ],
        "fields": [
            {
                "id": field["id"],
                "key": field["field_key"],
                "label": field["label"],
                "type": field["field_type"],
                "options": json.loads(field["options_json"] or "[]"),
                "required": bool(field["required"]),
            }
            for field in task["fields"]
        ],
    }
    if include_private:
        result.update(
            {
                "id": task["id"],
                "name": task["name"],
                "isDefault": bool(task["is_default"]),
                "createdAt": task["created_at"],
                "updatedAt": task["updated_at"],
            }
        )
    return result


def generate_slots(task):
    slots = []
    duration = int(task["slot_minutes"])
    for date_value in task["dates"]:
        for period in task["periods"]:
            start = time_minutes(period["start_time"])
            end = time_minutes(period["end_time"])
            if start is None or end is None:
                continue
            current = start
            while current + duration <= end:
                start_text = f"{current // 60:02d}:{current % 60:02d}"
                end_value = current + duration
                slots.append(
                    {
                        "date": date_value,
                        "time": start_text,
                        "endTime": f"{end_value // 60:02d}:{end_value % 60:02d}",
                        "periodId": period["id"],
                        "periodLabel": period["label"],
                    }
                )
                current += duration
    return slots


def validate_task_input(payload):
    name = str(payload.get("name", "")).strip()
    title = str(payload.get("title", "")).strip()
    description = str(payload.get("description", "")).strip()
    location = str(payload.get("location", "")).strip()
    contact = str(payload.get("contact", "")).strip()
    timezone = str(payload.get("timezone", "Asia/Shanghai")).strip() or "Asia/Shanghai"
    strategy = str(payload.get("openingStrategy", "any"))
    try:
        duration = int(payload.get("slotMinutes", 10))
    except (TypeError, ValueError):
        duration = 0
    if not name:
        return None, "请输入后台任务名称"
    if not title:
        return None, "请输入公开页面标题"
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        return None, "时区名称无效"
    if duration < 5 or duration > 240:
        return None, "单次预约时长必须在 5–240 分钟之间"
    if strategy not in ("any", "sequential"):
        return None, "开放方式无效"

    raw_dates = payload.get("dates", [])
    dates = sorted({str(value) for value in raw_dates if parse_date(value)})
    if not dates:
        return None, "请至少选择一个可预约日期"
    if len(dates) > 366:
        return None, "一个任务最多设置 366 个日期"

    periods = []
    for index, raw in enumerate(payload.get("periods", [])):
        label = str(raw.get("label", "")).strip() or f"时段 {index + 1}"
        start = str(raw.get("start", "")).strip()
        end = str(raw.get("end", "")).strip()
        start_value = time_minutes(start)
        end_value = time_minutes(end)
        if start_value is None or end_value is None or end_value <= start_value:
            return None, f"{label}的结束时间必须晚于开始时间"
        if (end_value - start_value) % duration:
            return None, f"{label}的长度必须能被 {duration} 分钟整除"
        periods.append({"label": label, "start": start, "end": end, "startValue": start_value, "endValue": end_value})
    if not periods:
        return None, "请至少添加一个开放时段"
    periods.sort(key=lambda item: item["startValue"])
    for previous, current in zip(periods, periods[1:]):
        if current["startValue"] < previous["endValue"]:
            return None, f"{current['label']}与{previous['label']}存在重叠"

    fields = []
    seen_labels = set()
    seen_keys = set()
    for raw in payload.get("fields", []):
        label = str(raw.get("label", "")).strip()
        if not label:
            continue
        label_key = label.lower()
        if label_key in ("姓名", "邮箱", "name", "email") or label_key in seen_labels:
            return None, f"附加字段“{label}”重复或属于系统字段"
        seen_labels.add(label_key)
        field_key = str(raw.get("key", "")).strip()
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{2,63}", field_key):
            field_key = f"field_{secrets.token_hex(4)}"
        while field_key in seen_keys:
            field_key = f"field_{secrets.token_hex(4)}"
        seen_keys.add(field_key)
        field_type = str(raw.get("type", "text"))
        if field_type not in ("text", "textarea", "phone", "select"):
            return None, f"附加字段“{label}”类型无效"
        options = [str(value).strip() for value in raw.get("options", []) if str(value).strip()]
        if field_type == "select" and len(options) < 2:
            return None, f"选择字段“{label}”至少需要两个选项"
        fields.append(
            {"key": field_key, "label": label, "type": field_type, "options": options, "required": bool(raw.get("required"))}
        )
    if len(fields) > 10:
        return None, "附加字段最多 10 个"

    return {
        "name": name,
        "title": title,
        "description": description,
        "location": location,
        "contact": contact,
        "timezone": timezone,
        "slotMinutes": duration,
        "openingStrategy": strategy,
        "dates": dates,
        "periods": periods,
        "fields": fields,
    }, None


def save_task_config(conn, task_id, data):
    timestamp = now_text()
    conn.execute(
        """
        UPDATE tasks SET name=?, title=?, description=?, location=?, contact=?, timezone=?,
            slot_minutes=?, opening_strategy=?, updated_at=? WHERE id=?
        """,
        (
            data["name"], data["title"], data["description"], data["location"], data["contact"],
            data["timezone"], data["slotMinutes"], data["openingStrategy"], timestamp, task_id,
        ),
    )
    conn.execute("DELETE FROM task_dates WHERE task_id=?", (task_id,))
    conn.executemany("INSERT INTO task_dates (task_id, date) VALUES (?, ?)", [(task_id, item) for item in data["dates"]])
    conn.execute("DELETE FROM task_periods WHERE task_id=?", (task_id,))
    conn.executemany(
        "INSERT INTO task_periods (task_id, label, start_time, end_time, sort_order) VALUES (?, ?, ?, ?, ?)",
        [(task_id, item["label"], item["start"], item["end"], index) for index, item in enumerate(data["periods"])],
    )
    conn.execute("DELETE FROM task_fields WHERE task_id=?", (task_id,))
    conn.executemany(
        """
        INSERT INTO task_fields (task_id, field_key, label, field_type, options_json, required, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (task_id, item["key"], item["label"], item["type"], json.dumps(item["options"], ensure_ascii=False), int(item["required"]), index)
            for index, item in enumerate(data["fields"])
        ],
    )


def config_conflicts(conn, task_id, data):
    pseudo_task = {
        "dates": data["dates"],
        "periods": [
            {"id": index, "label": item["label"], "start_time": item["start"], "end_time": item["end"]}
            for index, item in enumerate(data["periods"])
        ],
        "slot_minutes": data["slotMinutes"],
    }
    allowed = {(slot["date"], slot["time"], slot["endTime"]) for slot in generate_slots(pseudo_task)}
    conflicts = []
    for row in conn.execute(
        "SELECT id, name, email, date, time, end_time FROM bookings WHERE task_id=? AND status='confirmed' ORDER BY date, time", (task_id,)
    ):
        if (row["date"], row["time"], row["end_time"]) not in allowed:
            conflicts.append({"id": row["id"], "name": row["name"], "email": row["email"], "date": row["date"], "time": row["time"]})
    return conflicts


def slot_map_for_task(conn, task):
    bookings = {
        (row["date"], row["time"]): row
        for row in conn.execute("SELECT * FROM bookings WHERE task_id=? AND status='confirmed'", (task["id"],)).fetchall()
    }
    blocked = {
        (row["date"], row["time"]): row
        for row in conn.execute("SELECT * FROM blocked_slots WHERE task_id=?", (task["id"],)).fetchall()
    }
    result = []
    chain_open_by_period = {}
    try:
        local_now = datetime.now(ZoneInfo(task["timezone"]))
    except ZoneInfoNotFoundError:
        local_now = datetime.now().astimezone()
    for slot in generate_slots(task):
        key = (slot["date"], slot["time"])
        period_key = (slot["date"], slot["periodId"])
        chain_open = chain_open_by_period.get(period_key, True)
        booking = bookings.get(key)
        block = blocked.get(key)
        slot_start = datetime.combine(parse_date(slot["date"]), datetime.strptime(slot["time"], "%H:%M").time())
        if local_now.tzinfo is not None:
            slot_start = slot_start.replace(tzinfo=local_now.tzinfo)
        if booking:
            status = "booked"
        elif block:
            status = "blocked"
        elif slot_start <= local_now or task["status"] != "published":
            status = "closed"
        elif task["opening_strategy"] == "sequential" and not chain_open:
            status = "locked"
        else:
            status = "available"
        if not booking and not block:
            chain_open_by_period[period_key] = False
        else:
            chain_open_by_period[period_key] = chain_open
        result.append({**slot, "status": status, "booking": booking, "blockedRow": block})
    return result


def format_booking(row):
    answers = json.loads(row["answers_json"] or "{}")
    return {
        "id": row["id"], "name": row["name"], "email": row["email"], "date": row["date"],
        "time": row["time"], "endTime": row["end_time"], "answers": answers,
        "status": row["status"], "cancelledAt": row["cancelled_at"],
        "cancellationReason": row["cancellation_reason"], "createdAt": row["created_at"],
    }


def parse_datetime(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def email_session_is_valid(conn, email, token):
    if not token:
        return False
    row = conn.execute("SELECT expires_at FROM email_sessions WHERE token=? AND email=? COLLATE NOCASE", (token, email)).fetchone()
    expires = parse_datetime(row["expires_at"]) if row else None
    return bool(expires and expires >= datetime.now())


def create_email_session(conn, email):
    token = secrets.token_urlsafe(32)
    expires = datetime.now() + timedelta(hours=EMAIL_SESSION_TTL_HOURS)
    conn.execute(
        "INSERT INTO email_sessions (token, email, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (token, email, expires.strftime("%Y-%m-%d %H:%M:%S"), now_text()),
    )
    return token, expires


def send_email_message(message):
    missing = [name for name, value in (("SMTP_HOST", SMTP_HOST), ("SMTP_PORT", SMTP_PORT), ("SMTP_FROM", SMTP_FROM)) if not value]
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
    message = EmailMessage()
    message["Subject"] = "预约邮箱验证码"
    message["From"] = SMTP_FROM
    message["To"] = email
    message.set_content(f"您的预约验证码是：{code}\n\n验证码 {VERIFICATION_TTL_MINUTES} 分钟内有效。")
    send_email_message(message)


def send_booking_email(task, booking):
    message = EmailMessage()
    message["Subject"] = f"{task['title']}预约成功"
    message["From"] = SMTP_FROM
    message["To"] = booking["email"]
    lines = [
        f"{booking['name']}，您好：", "", f"您已成功预约：{task['title']}",
        f"日期：{booking['date']}", f"时间：{booking['time']} - {booking['endTime']}",
    ]
    if task["location"]:
        lines.append(f"地点：{task['location']}")
    message.set_content("\n".join(lines))
    send_email_message(message)


class BookingHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def serve_html(self, filename):
        body = (ROOT / filename).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location):
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def is_admin(self):
        raw = self.headers.get("Cookie", "")
        cookie = SimpleCookie()
        cookie.load(raw)
        value = cookie.get(ADMIN_SESSION_COOKIE)
        return bool(value and hmac.compare_digest(value.value, ADMIN_SESSION_TOKEN))

    def require_admin(self):
        if self.is_admin():
            return True
        self.send_json({"error": "请先登录后台"}, HTTPStatus.UNAUTHORIZED)
        return False

    def path_parts(self):
        return [part for part in urlparse(self.path).path.split("/") if part]

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/admin", "/admin.html") or path.startswith("/admin/"):
            self.serve_html("admin.html")
            return
        if path.startswith("/b/") and len(self.path_parts()) == 2:
            self.serve_html("index.html")
            return
        if path in ("/", "/index.html"):
            with connect() as conn:
                task = conn.execute("SELECT public_id FROM tasks WHERE is_default=1 ORDER BY id LIMIT 1").fetchone()
                if not task:
                    task = conn.execute("SELECT public_id FROM tasks WHERE status='published' ORDER BY id LIMIT 1").fetchone()
            self.redirect(f"/b/{task['public_id']}" if task else "/admin")
            return
        if path == "/api/admin/session":
            self.send_json({"authenticated": self.is_admin(), "defaultPassword": ADMIN_PASSWORD_IS_DEFAULT})
            return
        if path == "/api/admin/tasks":
            if self.require_admin():
                self.admin_list_tasks()
            return
        parts = self.path_parts()
        if len(parts) >= 4 and parts[:3] == ["api", "public", "tasks"]:
            self.public_get_task(parts[3])
            return
        if len(parts) >= 4 and parts[:3] == ["api", "admin", "tasks"]:
            if not self.require_admin():
                return
            self.admin_get_route(parts, parse_qs(parsed.query))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path
        parts = self.path_parts()
        if path == "/api/admin/login":
            self.admin_login()
            return
        if path == "/api/admin/logout":
            self.admin_logout()
            return
        if path == "/api/verification/send":
            self.verification_send()
            return
        if path == "/api/verification/verify":
            self.verification_verify()
            return
        if path == "/api/admin/tasks":
            if self.require_admin():
                self.admin_create_task()
            return
        if len(parts) >= 5 and parts[:3] == ["api", "public", "tasks"] and parts[4] == "bookings":
            if len(parts) == 6 and parts[5] == "lookup":
                self.public_lookup_booking(parts[3])
            else:
                self.public_create_booking(parts[3])
            return
        if len(parts) >= 5 and parts[:3] == ["api", "admin", "tasks"]:
            if not self.require_admin():
                return
            self.admin_post_route(parts)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PUT(self):
        parts = self.path_parts()
        if len(parts) == 4 and parts[:3] == ["api", "admin", "tasks"]:
            if self.require_admin():
                self.admin_update_task(parts[3])
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self):
        parts = self.path_parts()
        if len(parts) == 6 and parts[:3] == ["api", "public", "tasks"] and parts[4:] == ["bookings", "by-email"]:
            self.public_cancel_booking(parts[3])
            return
        if len(parts) >= 5 and parts[:3] == ["api", "admin", "tasks"]:
            if not self.require_admin():
                return
            self.admin_delete_route(parts)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def admin_login(self):
        client_ip = self.client_address[0]
        cutoff = datetime.now() - timedelta(minutes=5)
        failures = [value for value in ADMIN_LOGIN_FAILURES.get(client_ip, []) if value >= cutoff]
        ADMIN_LOGIN_FAILURES[client_ip] = failures
        if len(failures) >= 5:
            self.send_json({"error": "登录失败次数过多，请 5 分钟后重试"}, HTTPStatus.TOO_MANY_REQUESTS)
            return
        try:
            password = str(self.read_json().get("password", ""))
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        if not hmac.compare_digest(password, ADMIN_PASSWORD):
            failures.append(datetime.now())
            ADMIN_LOGIN_FAILURES[client_ip] = failures
            self.send_json({"error": "后台密码错误"}, HTTPStatus.UNAUTHORIZED)
            return
        ADMIN_LOGIN_FAILURES.pop(client_ip, None)
        body = json.dumps({"ok": True}).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
        self.send_header("Set-Cookie", f"{ADMIN_SESSION_COOKIE}={ADMIN_SESSION_TOKEN}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800{secure}")
        self.end_headers()
        self.wfile.write(body)

    def admin_logout(self):
        body = b'{"ok":true}'
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", f"{ADMIN_SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

    def public_get_task(self, public_id):
        with connect() as conn:
            task = load_task(conn, public_id=public_id)
            if not task:
                self.send_json({"error": "预约任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            slots = slot_map_for_task(conn, task)
            current_booking = None
        result = task_payload(task)
        result["slots"] = [
            {key: slot[key] for key in ("date", "time", "endTime", "periodId", "periodLabel", "status")}
            | {"status": "occupied" if slot["status"] in ("booked", "blocked") else slot["status"]}
            for slot in slots
        ]
        self.send_json(result)

    def admin_list_tasks(self):
        with connect() as conn:
            tasks = conn.execute(
                """
                SELECT t.*, COUNT(DISTINCT b.id) booking_count
                FROM tasks t LEFT JOIN bookings b ON b.task_id=t.id AND b.status='confirmed'
                GROUP BY t.id ORDER BY CASE t.status WHEN 'published' THEN 0 WHEN 'draft' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END, t.updated_at DESC
                """
            ).fetchall()
            result = []
            for row in tasks:
                task = load_task(conn, task_id=row["id"])
                item = task_payload(task, include_private=True)
                item["bookingCount"] = row["booking_count"]
                item["slotCount"] = len(generate_slots(task))
                result.append(item)
        self.send_json(result)

    def admin_get_route(self, parts, query):
        try:
            task_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "任务编号无效"}, HTTPStatus.BAD_REQUEST)
            return
        with connect() as conn:
            task = load_task(conn, task_id=task_id)
            if not task:
                self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            if len(parts) == 4:
                result = task_payload(task, include_private=True)
                slots = slot_map_for_task(conn, task)
                result["slots"] = [
                    {key: slot[key] for key in ("date", "time", "endTime", "periodId", "periodLabel", "status")}
                    for slot in slots
                ]
                self.send_json(result)
                return
            if parts[4] == "bookings":
                status = query.get("status", ["all"])[0]
                search = query.get("search", [""])[0].strip().lower()
                start_date = query.get("startDate", [""])[0]
                end_date = query.get("endDate", [""])[0]
                clauses = ["task_id=?"]
                params = [task_id]
                if status in ("confirmed", "cancelled"):
                    clauses.append("status=?")
                    params.append(status)
                if start_date:
                    clauses.append("date>=?")
                    params.append(start_date)
                if end_date:
                    clauses.append("date<=?")
                    params.append(end_date)
                if search:
                    clauses.append("(lower(name) LIKE ? OR lower(email) LIKE ? OR lower(answers_json) LIKE ?)")
                    pattern = f"%{search}%"
                    params.extend([pattern, pattern, pattern])
                rows = conn.execute(
                    f"SELECT * FROM bookings WHERE {' AND '.join(clauses)} ORDER BY date, time, created_at",
                    params,
                ).fetchall()
                self.send_json([format_booking(row) for row in rows])
                return
            if parts[4] == "blocked-slots":
                rows = conn.execute("SELECT * FROM blocked_slots WHERE task_id=? ORDER BY date, time", (task_id,)).fetchall()
                self.send_json([dict(row) for row in rows])
                return
            if parts[4] == "export.csv":
                self.export_csv(conn, task, query)
                return
        self.send_error(HTTPStatus.NOT_FOUND)

    def admin_create_task(self):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        data, error = validate_task_input(payload)
        if error:
            self.send_json({"error": error}, HTTPStatus.BAD_REQUEST)
            return
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            timestamp = now_text()
            cursor = conn.execute(
                """
                INSERT INTO tasks (public_id, name, title, description, location, contact, timezone, slot_minutes,
                    opening_strategy, status, is_default, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 0, ?, ?)
                """,
                (
                    generate_public_id(conn), data["name"], data["title"], data["description"], data["location"],
                    data["contact"], data["timezone"], data["slotMinutes"], data["openingStrategy"], timestamp, timestamp,
                ),
            )
            save_task_config(conn, cursor.lastrowid, data)
            task = load_task(conn, task_id=cursor.lastrowid)
        self.send_json(task_payload(task, include_private=True), HTTPStatus.CREATED)

    def admin_update_task(self, raw_id):
        try:
            task_id = int(raw_id)
            payload = self.read_json()
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        data, error = validate_task_input(payload)
        if error:
            self.send_json({"error": error}, HTTPStatus.BAD_REQUEST)
            return
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            conflicts = config_conflicts(conn, task_id, data)
            if conflicts:
                self.send_json({"error": f"新设置与 {len(conflicts)} 个已有预约冲突，请先处理这些预约。", "conflicts": conflicts}, HTTPStatus.CONFLICT)
                return
            pseudo_task = {
                "dates": data["dates"],
                "periods": [
                    {"id": index, "label": item["label"], "start_time": item["start"], "end_time": item["end"]}
                    for index, item in enumerate(data["periods"])
                ],
                "slot_minutes": data["slotMinutes"],
            }
            valid_keys = {(slot["date"], slot["time"]) for slot in generate_slots(pseudo_task)}
            blocked_conflicts = [
                dict(row)
                for row in conn.execute("SELECT id, date, time FROM blocked_slots WHERE task_id=?", (task_id,)).fetchall()
                if (row["date"], row["time"]) not in valid_keys
            ]
            if blocked_conflicts and not payload.get("forceRemoveBlocks"):
                self.send_json(
                    {
                        "error": f"新设置会移除 {len(blocked_conflicts)} 个后台占用，请确认后再保存。",
                        "blockedConflicts": blocked_conflicts,
                        "requiresBlockCleanup": True,
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            save_task_config(conn, task_id, data)
            for row in blocked_conflicts:
                conn.execute("DELETE FROM blocked_slots WHERE id=?", (row["id"],))
            task = load_task(conn, task_id=task_id)
        self.send_json(task_payload(task, include_private=True))

    def admin_post_route(self, parts):
        try:
            task_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "任务编号无效"}, HTTPStatus.BAD_REQUEST)
            return
        action = parts[4]
        if action == "status":
            try:
                status = str(self.read_json().get("status", ""))
            except json.JSONDecodeError:
                status = ""
            if status not in VALID_STATUSES:
                self.send_json({"error": "任务状态无效"}, HTTPStatus.BAD_REQUEST)
                return
            with connect() as conn:
                task = load_task(conn, task_id=task_id)
                if not task:
                    self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                    return
                if status not in STATUS_TRANSITIONS.get(task["status"], set()):
                    self.send_json(
                        {"error": f"任务不能从“{task['status']}”直接切换到“{status}”"},
                        HTTPStatus.CONFLICT,
                    )
                    return
                try:
                    task_today = datetime.now(ZoneInfo(task["timezone"])).date().isoformat()
                except ZoneInfoNotFoundError:
                    task_today = date.today().isoformat()
                if status == "published" and not any(value >= task_today for value in task["dates"]):
                    self.send_json({"error": "任务没有今天或未来的开放日期，请先修改日期再发布。"}, HTTPStatus.CONFLICT)
                    return
                conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (status, now_text(), task_id))
            self.send_json({"ok": True, "status": status})
            return
        if action == "copy":
            self.admin_copy_task(task_id)
            return
        if action == "blocked-slots":
            self.admin_block_slot(task_id)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def admin_copy_task(self, task_id):
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = load_task(conn, task_id=task_id)
            if not source:
                self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            timestamp = now_text()
            cursor = conn.execute(
                """
                INSERT INTO tasks (public_id, name, title, description, location, contact, timezone, slot_minutes,
                    opening_strategy, status, is_default, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 0, ?, ?)
                """,
                (
                    generate_public_id(conn), f"{source['name']}（副本）", source["title"], source["description"], source["location"],
                    source["contact"], source["timezone"], source["slot_minutes"], source["opening_strategy"], timestamp, timestamp,
                ),
            )
            new_id = cursor.lastrowid
            conn.executemany("INSERT INTO task_dates (task_id, date) VALUES (?, ?)", [(new_id, value) for value in source["dates"]])
            conn.executemany(
                "INSERT INTO task_periods (task_id, label, start_time, end_time, sort_order) VALUES (?, ?, ?, ?, ?)",
                [(new_id, row["label"], row["start_time"], row["end_time"], row["sort_order"]) for row in source["periods"]],
            )
            conn.executemany(
                """
                INSERT INTO task_fields (task_id, field_key, label, field_type, options_json, required, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (new_id, row["field_key"], row["label"], row["field_type"], row["options_json"], row["required"], row["sort_order"])
                    for row in source["fields"]
                ],
            )
            task = load_task(conn, task_id=new_id)
        self.send_json(task_payload(task, include_private=True), HTTPStatus.CREATED)

    def admin_block_slot(self, task_id):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        date_value = str(payload.get("date", ""))
        time_value = str(payload.get("time", ""))
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = load_task(conn, task_id=task_id)
            if not task:
                self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            valid = {(slot["date"], slot["time"]) for slot in generate_slots(task)}
            if (date_value, time_value) not in valid:
                self.send_json({"error": "该时间不属于此任务"}, HTTPStatus.BAD_REQUEST)
                return
            if conn.execute(
                "SELECT 1 FROM bookings WHERE task_id=? AND date=? AND time=? AND status='confirmed'",
                (task_id, date_value, time_value),
            ).fetchone():
                self.send_json({"error": "该时间已有预约"}, HTTPStatus.CONFLICT)
                return
            try:
                conn.execute("INSERT INTO blocked_slots (task_id, date, time, created_at) VALUES (?, ?, ?, ?)", (task_id, date_value, time_value, now_text()))
            except sqlite3.IntegrityError:
                self.send_json({"error": "该时间已经占用"}, HTTPStatus.CONFLICT)
                return
        self.send_json({"ok": True}, HTTPStatus.CREATED)

    def admin_delete_route(self, parts):
        try:
            task_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "任务编号无效"}, HTTPStatus.BAD_REQUEST)
            return
        if parts[4] == "blocked-slots":
            try:
                payload = self.read_json()
            except json.JSONDecodeError:
                payload = {}
            with connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM blocked_slots WHERE task_id=? AND date=? AND time=?",
                    (task_id, str(payload.get("date", "")), str(payload.get("time", ""))),
                )
            if not cursor.rowcount:
                self.send_json({"error": "未找到后台占用"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json({"ok": True})
            return
        if parts[4] == "bookings" and len(parts) == 6:
            try:
                booking_id = int(parts[5])
            except ValueError:
                self.send_json({"error": "预约编号无效"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                payload = self.read_json()
            except json.JSONDecodeError:
                payload = {}
            reason = str(payload.get("reason", "管理员取消")).strip() or "管理员取消"
            with connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE bookings SET status='cancelled', cancelled_at=?, cancellation_reason=?
                    WHERE task_id=? AND id=? AND status='confirmed'
                    """,
                    (now_text(), reason, task_id, booking_id),
                )
            if not cursor.rowcount:
                self.send_json({"error": "预约不存在或已取消"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json({"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def verification_send(self):
        try:
            email = normalize_email(self.read_json().get("email"))
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        if not EMAIL_RE.match(email):
            self.send_json({"error": "请输入有效邮箱"}, HTTPStatus.BAD_REQUEST)
            return
        with connect() as conn:
            existing = conn.execute("SELECT sent_at FROM email_verifications WHERE email=?", (email,)).fetchone()
            sent_at = parse_datetime(existing["sent_at"]) if existing else None
            if sent_at and (datetime.now() - sent_at).total_seconds() < 60:
                self.send_json({"error": "验证码发送过于频繁，请稍后重试"}, HTTPStatus.TOO_MANY_REQUESTS)
                return
        code = DEV_VERIFICATION_CODE or f"{secrets.randbelow(1_000_000):06d}"
        if not DEV_VERIFICATION_CODE:
            try:
                send_verification_email(email, code)
            except Exception as exc:
                self.send_json({"error": f"验证码邮件发送失败：{exc}"}, HTTPStatus.BAD_GATEWAY)
                return
        timestamp = datetime.now()
        expires = timestamp + timedelta(minutes=VERIFICATION_TTL_MINUTES)
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO email_verifications (email, code, expires_at, verified_at, attempts, sent_at)
                VALUES (?, ?, ?, NULL, 0, ?)
                ON CONFLICT(email) DO UPDATE SET code=excluded.code, expires_at=excluded.expires_at,
                    verified_at=NULL, attempts=0, sent_at=excluded.sent_at
                """,
                (email, code, expires.strftime("%Y-%m-%d %H:%M:%S"), timestamp.strftime("%Y-%m-%d %H:%M:%S")),
            )
        response = {"ok": True, "message": "验证码已发送"}
        if DEV_VERIFICATION_CODE:
            response["devCode"] = code
        self.send_json(response)

    def verification_verify(self):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        email = normalize_email(payload.get("email"))
        code = str(payload.get("code", "")).strip()
        if not EMAIL_RE.match(email) or not re.fullmatch(r"\d{6}", code):
            self.send_json({"error": "请输入有效邮箱和 6 位验证码"}, HTTPStatus.BAD_REQUEST)
            return
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM email_verifications WHERE email=?", (email,)).fetchone()
            expires = parse_datetime(row["expires_at"]) if row else None
            if not row or not expires or expires < datetime.now():
                self.send_json({"error": "验证码不存在或已过期"}, HTTPStatus.BAD_REQUEST)
                return
            if row["attempts"] >= MAX_VERIFICATION_ATTEMPTS:
                self.send_json({"error": "错误次数过多，请重新发送"}, HTTPStatus.TOO_MANY_REQUESTS)
                return
            if not hmac.compare_digest(row["code"], code):
                conn.execute("UPDATE email_verifications SET attempts=attempts+1 WHERE email=?", (email,))
                self.send_json({"error": "验证码不正确"}, HTTPStatus.BAD_REQUEST)
                return
            conn.execute("UPDATE email_verifications SET verified_at=? WHERE email=?", (now_text(), email))
            token, expires_at = create_email_session(conn, email)
        self.send_json({"ok": True, "verificationToken": token, "verificationExpiresAt": expires_at.strftime("%Y-%m-%d %H:%M:%S")})

    def public_create_booking(self, public_id):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        name = str(payload.get("name", "")).strip()
        email = normalize_email(payload.get("email"))
        date_value = str(payload.get("date", ""))
        time_value = str(payload.get("time", ""))
        answers = payload.get("answers", {}) if isinstance(payload.get("answers", {}), dict) else {}
        if not name or not EMAIL_RE.match(email):
            self.send_json({"error": "请输入姓名和有效邮箱"}, HTTPStatus.BAD_REQUEST)
            return
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = load_task(conn, public_id=public_id)
            if not task:
                self.send_json({"error": "预约任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            if task["status"] != "published":
                self.send_json({"error": "该任务当前不可预约"}, HTTPStatus.CONFLICT)
                return
            if not email_session_is_valid(conn, email, str(payload.get("verificationToken", ""))):
                self.send_json({"error": "请先完成邮箱验证"}, HTTPStatus.FORBIDDEN)
                return
            normalized_answers = {"__labels": {}}
            for field in task["fields"]:
                value = str(answers.get(field["field_key"], answers.get(field["label"], ""))).strip()
                if field["required"] and not value:
                    self.send_json({"error": f"请填写{field['label']}"}, HTTPStatus.BAD_REQUEST)
                    return
                if field["field_type"] == "select" and value and value not in json.loads(field["options_json"] or "[]"):
                    self.send_json({"error": f"{field['label']}选项无效"}, HTTPStatus.BAD_REQUEST)
                    return
                normalized_answers[field["field_key"]] = value
                normalized_answers["__labels"][field["field_key"]] = field["label"]
            slots = slot_map_for_task(conn, task)
            selected = next((slot for slot in slots if slot["date"] == date_value and slot["time"] == time_value), None)
            if not selected:
                self.send_json({"error": "所选时间不属于此任务"}, HTTPStatus.BAD_REQUEST)
                return
            if selected["status"] != "available":
                self.send_json({"error": "该时间当前不可预约，请刷新后重试"}, HTTPStatus.CONFLICT)
                return
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO bookings (task_id, name, email, date, time, end_time, answers_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (task["id"], name, email, date_value, time_value, selected["endTime"], json.dumps(normalized_answers, ensure_ascii=False), now_text()),
                )
            except sqlite3.IntegrityError:
                self.send_json({"error": "该邮箱已预约此任务，或该时间刚被占用"}, HTTPStatus.CONFLICT)
                return
            row = conn.execute("SELECT * FROM bookings WHERE id=?", (cursor.lastrowid,)).fetchone()
        result = format_booking(row)
        try:
            send_booking_email(task, result)
        except Exception as exc:
            result["emailWarning"] = f"预约已保存，但确认邮件发送失败：{exc}"
        self.send_json(result, HTTPStatus.CREATED)

    def public_lookup_booking(self, public_id):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        email = normalize_email(payload.get("email"))
        with connect() as conn:
            task = load_task(conn, public_id=public_id)
            if not task:
                self.send_json({"error": "预约任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            if not email_session_is_valid(conn, email, str(payload.get("verificationToken", ""))):
                self.send_json({"error": "请先完成邮箱验证"}, HTTPStatus.FORBIDDEN)
                return
            row = conn.execute(
                """
                SELECT * FROM bookings
                WHERE task_id=? AND email=? COLLATE NOCASE AND status='confirmed'
                ORDER BY created_at DESC LIMIT 1
                """,
                (task["id"], email),
            ).fetchone()
        self.send_json({"booking": format_booking(row) if row else None})

    def public_cancel_booking(self, public_id):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        email = normalize_email(payload.get("email"))
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = load_task(conn, public_id=public_id)
            if not task:
                self.send_json({"error": "预约任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            if not email_session_is_valid(conn, email, str(payload.get("verificationToken", ""))):
                self.send_json({"error": "请先完成邮箱验证"}, HTTPStatus.FORBIDDEN)
                return
            reason = str(payload.get("reason", "用户取消")).strip() or "用户取消"
            cursor = conn.execute(
                """
                UPDATE bookings SET status='cancelled', cancelled_at=?, cancellation_reason=?
                WHERE task_id=? AND email=? COLLATE NOCASE AND status='confirmed'
                """,
                (now_text(), reason, task["id"], email),
            )
            if not cursor.rowcount:
                self.send_json({"error": "该邮箱在此任务中没有预约"}, HTTPStatus.NOT_FOUND)
                return
        self.send_json({"ok": True})

    def export_csv(self, conn, task, query):
        output = io.StringIO()
        status = query.get("status", ["all"])[0]
        search = query.get("search", [""])[0].strip().lower()
        start_date = query.get("startDate", [""])[0]
        end_date = query.get("endDate", [""])[0]
        clauses = ["task_id=?"]
        params = [task["id"]]
        if status in ("confirmed", "cancelled"):
            clauses.append("status=?")
            params.append(status)
        if start_date:
            clauses.append("date>=?")
            params.append(start_date)
        if end_date:
            clauses.append("date<=?")
            params.append(end_date)
        if search:
            clauses.append("(lower(name) LIKE ? OR lower(email) LIKE ? OR lower(answers_json) LIKE ?)")
            pattern = f"%{search}%"
            params.extend([pattern, pattern, pattern])
        rows = conn.execute(
            f"SELECT * FROM bookings WHERE {' AND '.join(clauses)} ORDER BY date, time, created_at",
            params,
        ).fetchall()
        label_by_key = {field["field_key"]: field["label"] for field in task["fields"]}
        for row in rows:
            answers = json.loads(row["answers_json"] or "{}")
            label_by_key.update(answers.get("__labels", {}))
        fieldnames = ["状态", "日期", "时间", "姓名", "邮箱", *label_by_key.values(), "提交时间", "取消时间", "取消原因"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            booking = format_booking(row)
            item = {
                "状态": "已预约" if booking["status"] == "confirmed" else "已取消",
                "日期": booking["date"], "时间": f"{booking['time']}-{booking['endTime']}",
                "姓名": booking["name"], "邮箱": booking["email"], "提交时间": booking["createdAt"],
                "取消时间": booking["cancelledAt"] or "", "取消原因": booking["cancellationReason"],
            }
            for key, label in label_by_key.items():
                item[label] = booking["answers"].get(key, "")
            writer.writerow(item)
        body = ("\ufeff" + output.getvalue()).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename=task-{task['public_id']}-bookings.csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    init_db()
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), BookingHandler)
    print(f"Booking server running at http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
