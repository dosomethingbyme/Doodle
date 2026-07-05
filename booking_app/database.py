"""SQLite connection, schema creation, and forward-only migrations."""

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config
from .config import PUBLIC_ID_ALPHABET
from .security import hash_password, new_token
from .utils import add_minutes, time_minutes

@contextmanager
def connect():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


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
            min_advance_minutes INTEGER NOT NULL DEFAULT 0,
            max_advance_days INTEGER NOT NULL DEFAULT 365,
            status TEXT NOT NULL DEFAULT 'draft',
            published_version INTEGER NOT NULL DEFAULT 0,
            has_unpublished_changes INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS task_date_overrides (
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            is_closed INTEGER NOT NULL DEFAULT 0,
            periods_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (task_id, date)
        );
        CREATE TABLE IF NOT EXISTS task_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            config_json TEXT NOT NULL,
            published_at TEXT NOT NULL,
            UNIQUE(task_id, version)
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


def task_config_snapshot(conn, task_id):
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        return None
    return {
        "name": task["name"],
        "title": task["title"],
        "description": task["description"],
        "location": task["location"],
        "contact": task["contact"],
        "timezone": task["timezone"],
        "slot_minutes": task["slot_minutes"],
        "opening_strategy": task["opening_strategy"],
        "min_advance_minutes": task["min_advance_minutes"],
        "max_advance_days": task["max_advance_days"],
        "cancel_cutoff_minutes": task["cancel_cutoff_minutes"] if "cancel_cutoff_minutes" in task.keys() else 0,
        "reschedule_cutoff_minutes": task["reschedule_cutoff_minutes"] if "reschedule_cutoff_minutes" in task.keys() else 0,
        "max_reschedules": task["max_reschedules"] if "max_reschedules" in task.keys() else 10,
        "policy_text": task["policy_text"] if "policy_text" in task.keys() else "",
        "privacy_text": task["privacy_text"] if "privacy_text" in task.keys() else "",
        "retention_days": task["retention_days"] if "retention_days" in task.keys() else 365,
        "dates": [
            row["date"]
            for row in conn.execute("SELECT date FROM task_dates WHERE task_id=? ORDER BY date", (task_id,))
        ],
        "periods": [
            dict(row)
            for row in conn.execute(
                "SELECT id,label,start_time,end_time,sort_order FROM task_periods WHERE task_id=? ORDER BY sort_order,start_time",
                (task_id,),
            )
        ],
        "fields": [
            dict(row)
            for row in conn.execute(
                "SELECT id,field_key,label,field_type,options_json,required,sort_order FROM task_fields WHERE task_id=? ORDER BY sort_order,id",
                (task_id,),
            )
        ],
        "date_overrides": {
            row["date"]: {
                "closed": bool(row["is_closed"]),
                "periods": json.loads(row["periods_json"] or "[]"),
            }
            for row in conn.execute(
                "SELECT date,is_closed,periods_json FROM task_date_overrides WHERE task_id=? ORDER BY date",
                (task_id,),
            )
        },
    }


def migrate_product_capabilities(conn):
    columns = table_columns(conn, "tasks")
    additions = {
        "min_advance_minutes": "INTEGER NOT NULL DEFAULT 0",
        "max_advance_days": "INTEGER NOT NULL DEFAULT 365",
        "published_version": "INTEGER NOT NULL DEFAULT 0",
        "has_unpublished_changes": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_date_overrides (
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            is_closed INTEGER NOT NULL DEFAULT 0,
            periods_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (task_id, date)
        );
        CREATE TABLE IF NOT EXISTS task_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            config_json TEXT NOT NULL,
            published_at TEXT NOT NULL,
            UNIQUE(task_id, version)
        );
        CREATE TABLE IF NOT EXISTS booking_reschedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            old_date TEXT NOT NULL,
            old_time TEXT NOT NULL,
            old_end_time TEXT NOT NULL,
            new_date TEXT NOT NULL,
            new_time TEXT NOT NULL,
            new_end_time TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'user',
            changed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_booking_reschedules_booking ON booking_reschedules(booking_id, changed_at);
        """
    )
    for task in conn.execute(
        "SELECT id,status,published_version FROM tasks WHERE status!='draft' ORDER BY id"
    ).fetchall():
        if task["published_version"]:
            continue
        snapshot = task_config_snapshot(conn, task["id"])
        if snapshot is None:
            continue
        conn.execute(
            "INSERT INTO task_versions (task_id,version,config_json,published_at) VALUES (?,1,?,?)",
            (task["id"], json.dumps(snapshot, ensure_ascii=False), now_text()),
        )
        conn.execute(
            "UPDATE tasks SET published_version=1,has_unpublished_changes=0 WHERE id=?",
            (task["id"],),
        )


def migrate_v3_trusted_delivery(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('owner','operator')),
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT
        );
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
            csrf_token TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_admin_sessions_user ON admin_sessions(user_id, expires_at);
        CREATE TABLE IF NOT EXISTS system_settings (
            id INTEGER PRIMARY KEY CHECK(id=1),
            app_secret TEXT NOT NULL,
            smtp_host TEXT,
            smtp_port INTEGER,
            smtp_user TEXT,
            smtp_password TEXT,
            smtp_from TEXT,
            smtp_use_ssl INTEGER,
            smtp_starttls INTEGER,
            reminder_minutes INTEGER NOT NULL DEFAULT 1440,
            updated_by INTEGER REFERENCES admin_users(id),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER REFERENCES admin_users(id),
            actor_label TEXT NOT NULL,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            ip_address TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_events_created ON audit_events(created_at DESC);
        CREATE TABLE IF NOT EXISTS booking_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id TEXT NOT NULL DEFAULT '',
            request_key TEXT,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_booking_events_request_key
            ON booking_events(request_key) WHERE request_key IS NOT NULL AND request_key!='';
        CREATE INDEX IF NOT EXISTS idx_booking_events_booking ON booking_events(booking_id, id);
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            booking_id INTEGER REFERENCES bookings(id) ON DELETE CASCADE,
            task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
            recipient TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            sent_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
            ON notification_outbox(status, next_attempt_at);
        """
    )
    task_columns = table_columns(conn, "tasks")
    task_additions = {
        "cancel_cutoff_minutes": "INTEGER NOT NULL DEFAULT 0",
        "reschedule_cutoff_minutes": "INTEGER NOT NULL DEFAULT 0",
        "max_reschedules": "INTEGER NOT NULL DEFAULT 10",
        "policy_text": "TEXT NOT NULL DEFAULT ''",
        "privacy_text": "TEXT NOT NULL DEFAULT ''",
        "retention_days": "INTEGER NOT NULL DEFAULT 365",
        "revision": "INTEGER NOT NULL DEFAULT 1",
    }
    for name, definition in task_additions.items():
        if name not in task_columns:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
    booking_columns = table_columns(conn, "bookings")
    booking_additions = {
        "booking_ref": "TEXT NOT NULL DEFAULT ''",
        "policy_json": "TEXT NOT NULL DEFAULT '{}'",
        "idempotency_key": "TEXT NOT NULL DEFAULT ''",
        "internal_notes": "TEXT NOT NULL DEFAULT ''",
        "created_by_user_id": "INTEGER",
    }
    for name, definition in booking_additions.items():
        if name not in booking_columns:
            conn.execute(f"ALTER TABLE bookings ADD COLUMN {name} {definition}")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_ref_unique ON bookings(booking_ref) WHERE booking_ref!=''"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_idempotency_unique ON bookings(task_id,idempotency_key) WHERE idempotency_key!=''"
    )
    for booking in conn.execute("SELECT id FROM bookings WHERE booking_ref='' ORDER BY id").fetchall():
        while True:
            reference = "APT-" + "".join(secrets.choice(PUBLIC_ID_ALPHABET) for _ in range(10))
            if not conn.execute("SELECT 1 FROM bookings WHERE booking_ref=?", (reference,)).fetchone():
                break
        conn.execute("UPDATE bookings SET booking_ref=? WHERE id=?", (reference, booking["id"]))
    timestamp = now_text()
    if not conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone():
        conn.execute(
            """INSERT INTO admin_users (username,password_hash,role,is_active,created_at,updated_at)
            VALUES (?,?, 'owner',1,?,?)""",
            (config.ADMIN_USERNAME, hash_password(config.ADMIN_PASSWORD), timestamp, timestamp),
        )
    if not conn.execute("SELECT 1 FROM system_settings WHERE id=1").fetchone():
        conn.execute(
            "INSERT INTO system_settings (id,app_secret,updated_at) VALUES (1,?,?)",
            (new_token(48), timestamp),
        )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version,applied_at) VALUES (3,?)",
        (timestamp,),
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
        migrate_product_capabilities(conn)
        migrate_v3_trusted_delivery(conn)
