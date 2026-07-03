import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import server


class BookingServerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.tempdir.name) / "test.sqlite3"
        server.init_db()

    def tearDown(self):
        server.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def create_task(self, name="测试任务", opening_strategy="any"):
        booking_date = (date.today() + timedelta(days=1)).isoformat()
        data = {
            "name": name,
            "title": name,
            "description": "",
            "location": "",
            "contact": "",
            "timezone": "Asia/Shanghai",
            "slotMinutes": 15,
            "openingStrategy": opening_strategy,
            "dates": [booking_date],
            "periods": [{"label": "上午", "start": "09:00", "end": "10:00", "startValue": 540, "endValue": 600}],
            "fields": [{"key": "department", "label": "部门", "type": "text", "options": [], "required": True}],
        }
        with server.connect() as conn:
            timestamp = server.now_text()
            cursor = conn.execute(
                """
                INSERT INTO tasks (public_id, name, title, description, location, contact, timezone,
                    slot_minutes, opening_strategy, status, is_default, created_at, updated_at)
                VALUES (?, ?, ?, '', '', '', 'Asia/Shanghai', 15, ?, 'published', 0, ?, ?)
                """,
                (server.generate_public_id(conn), name, name, opening_strategy, timestamp, timestamp),
            )
            server.save_task_config(conn, cursor.lastrowid, data)
            return cursor.lastrowid, booking_date

    def test_fresh_database_starts_without_business_specific_task(self):
        with server.connect() as conn:
            count = conn.execute("SELECT COUNT(*) count FROM tasks").fetchone()["count"]
        self.assertEqual(count, 0)

    def test_legacy_database_is_wrapped_in_default_task(self):
        legacy_path = Path(self.tempdir.name) / "legacy.sqlite3"
        server.DB_PATH = legacy_path
        conn = sqlite3.connect(legacy_path)
        conn.executescript(
            """
            CREATE TABLE bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                mentor TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE blocked_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO bookings (name, mentor, email, date, time, created_at)
            VALUES ('旧用户', '旧导师', 'legacy@example.com', '2026-07-04', '09:30', '2026-07-01 10:00:00');
            """
        )
        conn.close()
        server.init_db()
        with server.connect() as conn:
            task = server.load_task(conn, task_id=1)
            booking = conn.execute("SELECT task_id, email FROM bookings").fetchone()
        self.assertEqual(task["title"], "AIAD 样机测试时间预约")
        self.assertEqual(booking["task_id"], task["id"])
        self.assertEqual(booking["email"], "legacy@example.com")

    def test_validation_rejects_overlapping_periods(self):
        payload = {
            "name": "冲突测试",
            "title": "冲突测试",
            "slotMinutes": 10,
            "openingStrategy": "any",
            "dates": [(date.today() + timedelta(days=1)).isoformat()],
            "periods": [
                {"label": "A", "start": "09:00", "end": "10:00"},
                {"label": "B", "start": "09:30", "end": "10:30"},
            ],
            "fields": [],
        }
        data, error = server.validate_task_input(payload)
        self.assertIsNone(data)
        self.assertIn("重叠", error)

    def test_same_email_and_time_are_isolated_by_task(self):
        first_id, booking_date = self.create_task("任务 A")
        second_id, _ = self.create_task("任务 B")
        with server.connect() as conn:
            values = ("测试用户", "same@example.com", booking_date, "09:00", "09:15", json.dumps({"department": "研发"}), server.now_text())
            conn.execute(
                "INSERT INTO bookings (task_id,name,email,date,time,end_time,answers_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (first_id, *values),
            )
            conn.execute(
                "INSERT INTO bookings (task_id,name,email,date,time,end_time,answers_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (second_id, *values),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO bookings (task_id,name,email,date,time,end_time,answers_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (first_id, "另一用户", "same@example.com", booking_date, "09:15", "09:30", "{}", server.now_text()),
                )

    def test_sequential_strategy_opens_only_frontier_slot(self):
        task_id, booking_date = self.create_task("顺序开放", opening_strategy="sequential")
        with server.connect() as conn:
            task = server.load_task(conn, task_id=task_id)
            statuses = [slot["status"] for slot in server.slot_map_for_task(conn, task)]
            self.assertEqual(statuses, ["available", "locked", "locked", "locked"])
            conn.execute(
                """
                INSERT INTO bookings (task_id,name,email,date,time,end_time,answers_json,created_at)
                VALUES (?, '用户', 'one@example.com', ?, '09:00', '09:15', '{}', ?)
                """,
                (task_id, booking_date, server.now_text()),
            )
            task = server.load_task(conn, task_id=task_id)
            statuses = [slot["status"] for slot in server.slot_map_for_task(conn, task)]
        self.assertEqual(statuses, ["booked", "available", "locked", "locked"])

    def test_soft_cancel_preserves_history_and_allows_rebooking(self):
        task_id, booking_date = self.create_task("取消历史")
        with server.connect() as conn:
            conn.execute(
                """
                INSERT INTO bookings (task_id,name,email,date,time,end_time,answers_json,created_at)
                VALUES (?, '用户', 'again@example.com', ?, '09:00', '09:15', '{}', ?)
                """,
                (task_id, booking_date, server.now_text()),
            )
            conn.execute(
                """
                UPDATE bookings SET status='cancelled', cancelled_at=?, cancellation_reason='计划变化'
                WHERE task_id=? AND email='again@example.com'
                """,
                (server.now_text(), task_id),
            )
            conn.execute(
                """
                INSERT INTO bookings (task_id,name,email,date,time,end_time,answers_json,created_at)
                VALUES (?, '用户', 'again@example.com', ?, '09:00', '09:15', '{}', ?)
                """,
                (task_id, booking_date, server.now_text()),
            )
            rows = conn.execute(
                "SELECT status, cancellation_reason FROM bookings WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        self.assertEqual([row["status"] for row in rows], ["cancelled", "confirmed"])
        self.assertEqual(rows[0]["cancellation_reason"], "计划变化")

    def test_field_key_survives_label_change(self):
        payload = {
            "name": "字段测试",
            "title": "字段测试",
            "slotMinutes": 15,
            "openingStrategy": "any",
            "dates": [(date.today() + timedelta(days=1)).isoformat()],
            "periods": [{"label": "上午", "start": "09:00", "end": "10:00"}],
            "fields": [{"key": "department", "label": "部门", "type": "select", "options": ["研发", "测试"], "required": True}],
        }
        data, error = server.validate_task_input(payload)
        self.assertIsNone(error)
        self.assertEqual(data["fields"][0]["key"], "department")
        payload["fields"][0]["label"] = "所属部门"
        renamed, error = server.validate_task_input(payload)
        self.assertIsNone(error)
        self.assertEqual(renamed["fields"][0]["key"], "department")

    def test_past_slot_on_current_day_is_closed(self):
        task_id, _ = self.create_task("当天时间")
        today = date.today().isoformat()
        with server.connect() as conn:
            conn.execute("DELETE FROM task_dates WHERE task_id=?", (task_id,))
            conn.execute("INSERT INTO task_dates (task_id,date) VALUES (?,?)", (task_id, today))
            conn.execute("UPDATE task_periods SET start_time='00:00',end_time='00:15' WHERE task_id=?", (task_id,))
            task = server.load_task(conn, task_id=task_id)
            slots = server.slot_map_for_task(conn, task)
        self.assertEqual(slots[0]["status"], "closed")


if __name__ == "__main__":
    unittest.main()
