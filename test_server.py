import json
import http.client
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import server
from booking_app import config
from booking_app import domain
from booking_app import public_routes
from booking_app import notifications
from booking_app.handler import BookingHandler
from scripts import backup


class BookingServerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = config.DB_PATH
        config.DB_PATH = Path(self.tempdir.name) / "test.sqlite3"
        server.init_db()

    def tearDown(self):
        config.DB_PATH = self.original_db_path
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
        config.DB_PATH = legacy_path
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
        with server.connect() as conn:
            version = conn.execute("SELECT published_version FROM tasks WHERE id=?", (task["id"],)).fetchone()[0]
        self.assertEqual(version, 1)

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

    def test_sequential_strategy_skips_expired_slots(self):
        task_id, _ = self.create_task("过期时段顺延", opening_strategy="sequential")

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2099, 7, 4, 12, 0)
                return value.replace(tzinfo=tz) if tz else value

        with server.connect() as conn:
            conn.execute("DELETE FROM task_dates WHERE task_id=?", (task_id,))
            conn.execute("INSERT INTO task_dates (task_id,date) VALUES (?, '2099-07-04')", (task_id,))
            conn.execute(
                "UPDATE task_periods SET start_time='11:00',end_time='13:00' WHERE task_id=?",
                (task_id,),
            )
            task = server.load_task(conn, task_id=task_id)
            original_datetime = domain.datetime
            domain.datetime = FixedDateTime
            try:
                statuses = [slot["status"] for slot in server.slot_map_for_task(conn, task)]
            finally:
                domain.datetime = original_datetime
        self.assertEqual(statuses[:6], ["closed", "closed", "closed", "closed", "closed", "available"])
        self.assertTrue(all(status == "locked" for status in statuses[6:]))

    def test_date_override_replaces_default_period_or_closes_date(self):
        payload = {
            "name": "日期例外",
            "title": "日期例外",
            "slotMinutes": 15,
            "openingStrategy": "any",
            "minAdvanceMinutes": 0,
            "maxAdvanceDays": 365,
            "dates": ["2099-07-04", "2099-07-05"],
            "periods": [{"label": "默认上午", "start": "09:00", "end": "10:00"}],
            "dateOverrides": [
                {"date": "2099-07-04", "closed": True, "periods": []},
                {
                    "date": "2099-07-05",
                    "closed": False,
                    "periods": [{"label": "特别下午", "start": "14:00", "end": "15:00"}],
                },
            ],
            "fields": [],
        }
        data, error = server.validate_task_input(payload)
        self.assertIsNone(error)
        task_id, _ = self.create_task("日期例外底表")
        with server.connect() as conn:
            server.save_task_config(conn, task_id, data)
            task = server.load_task(conn, task_id=task_id)
            slots = server.generate_slots(task)
        self.assertEqual({slot["date"] for slot in slots}, {"2099-07-05"})
        self.assertEqual(slots[0]["time"], "14:00")
        self.assertEqual(slots[0]["periodLabel"], "特别下午")

    def test_time_window_closes_too_soon_and_too_far_slots(self):
        task_id, _ = self.create_task("预约窗口")

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2099, 7, 4, 12, 0)
                return value.replace(tzinfo=tz) if tz else value

        with server.connect() as conn:
            conn.execute("DELETE FROM task_dates WHERE task_id=?", (task_id,))
            conn.executemany(
                "INSERT INTO task_dates (task_id,date) VALUES (?,?)",
                [(task_id, "2099-07-04"), (task_id, "2099-07-06")],
            )
            conn.execute(
                "UPDATE task_periods SET start_time='12:00',end_time='13:00' WHERE task_id=?",
                (task_id,),
            )
            conn.execute(
                "UPDATE tasks SET min_advance_minutes=30,max_advance_days=1 WHERE id=?",
                (task_id,),
            )
            task = server.load_task(conn, task_id=task_id)
            original_datetime = domain.datetime
            domain.datetime = FixedDateTime
            try:
                slots = server.slot_map_for_task(conn, task)
            finally:
                domain.datetime = original_datetime
        today = [slot for slot in slots if slot["date"] == "2099-07-04"]
        far = [slot for slot in slots if slot["date"] == "2099-07-06"]
        self.assertEqual([slot["status"] for slot in today[:3]], ["closed", "closed", "closed"])
        self.assertEqual(today[3]["status"], "available")
        self.assertTrue(all(slot["availabilityReason"] == "too_far" for slot in far))

    def test_concurrent_slot_claim_has_exactly_one_winner(self):
        task_id, booking_date = self.create_task("并发占位")
        barrier = threading.Barrier(2)
        outcomes = []

        def claim(email):
            barrier.wait()
            try:
                with server.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("""INSERT INTO bookings(task_id,name,email,date,time,end_time,answers_json,created_at)
                        VALUES (?,'并发用户',?,?,'09:00','09:15','{}',?)""", (task_id, email, booking_date, server.now_text()))
                outcomes.append("won")
            except sqlite3.IntegrityError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=claim, args=(f"race{index}@example.com",)) for index in range(2)]
        for item in threads: item.start()
        for item in threads: item.join(timeout=5)
        self.assertEqual(sorted(outcomes), ["conflict", "won"])

    def test_verified_backup_can_be_restored(self):
        task_id, _ = self.create_task("备份恢复")
        backup_path = Path(self.tempdir.name) / "verified-backup.sqlite3"
        result = backup.create(backup_path)
        self.assertEqual(result["tables"], 19)
        restored_path = Path(self.tempdir.name) / "restored.sqlite3"
        original = config.DB_PATH
        config.DB_PATH = restored_path
        try:
            restored = backup.restore(backup_path, force=True)
            self.assertEqual(restored["tables"], result["tables"])
            with server.connect() as conn:
                self.assertIsNotNone(conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone())
        finally:
            config.DB_PATH = original


class BookingHttpTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = config.DB_PATH
        self.original_dev_code = public_routes.DEV_VERIFICATION_CODE
        self.original_notification_sender = notifications.send_notification_email
        config.DB_PATH = Path(self.tempdir.name) / "http-test.sqlite3"
        public_routes.DEV_VERIFICATION_CODE = "123456"
        notifications.send_notification_email = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("test smtp offline"))
        server.init_db()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), BookingHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        public_routes.DEV_VERIFICATION_CODE = self.original_dev_code
        notifications.send_notification_email = self.original_notification_sender
        config.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def request(self, method, path, payload=None, cookie="", extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
            if method in ("POST", "PUT", "DELETE") and getattr(self, "csrf", ""):
                headers["X-CSRF-Token"] = self.csrf
        headers.update(extra_headers or {})
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        content_type = response_headers.get("content-type", "")
        data = json.loads(raw.decode("utf-8")) if "json" in content_type else raw.decode("utf-8")
        return response.status, response_headers, data

    def login(self):
        status, headers, result = self.request("POST", "/api/admin/login", {"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD})
        self.assertEqual(status, 200)
        self.csrf = result["csrfToken"]
        return headers["set-cookie"].split(";", 1)[0]

    def test_static_assets_and_admin_session(self):
        status, _, html = self.request("GET", "/admin")
        self.assertEqual(status, 200)
        self.assertIn("/static/admin.css", html)
        self.assertIn("/static/admin.js", html)
        self.assertIn("/static/admin_product.js", html)
        self.assertIn("/static/admin_v3.js", html)
        status, headers, script = self.request("GET", "/static/admin.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["content-type"])
        self.assertIn("renderRoute", script)
        status, _, product_script = self.request("GET", "/static/admin_product.js")
        self.assertEqual(status, 200)
        self.assertIn("publishVersion", product_script)
        cookie = self.login()
        status, _, tasks = self.request("GET", "/api/admin/tasks", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(tasks, [])
        status, _, _ = self.request("GET", "/static/%2e%2e/.env")
        self.assertEqual(status, 404)

    def test_csrf_and_email_settings_env_fallback(self):
        cookie = self.login()
        csrf = self.csrf
        self.csrf = ""
        status, _, result = self.request("POST", "/api/admin/tasks", {}, cookie)
        self.assertEqual((status, result["error"]), (403, "安全校验失败，请刷新页面后重试"))
        self.csrf = csrf
        status, _, settings = self.request("GET", "/api/admin/settings/email", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(settings["sources"]["host"], "env")
        status, _, settings = self.request("PUT", "/api/admin/settings/email", {
            "host": "smtp.internal", "port": 2525, "user": "mailer", "password": "secret",
            "from": "booking@example.com", "useSsl": False, "starttls": True,
        }, cookie)
        self.assertEqual(status, 200)
        self.assertEqual((settings["host"], settings["port"], settings["passwordConfigured"]), ("smtp.internal", 2525, True))
        self.assertNotIn("password", settings)
        status, _, restored = self.request("PUT", "/api/admin/settings/email", {
            "inherit": ["host", "port", "user", "password", "from", "useSsl", "starttls"]
        }, cookie)
        self.assertEqual((status, restored["sources"]["host"]), (200, "env"))
        status, _, _ = self.request("POST", "/api/admin/users", {
            "username": "limited_operator", "password": "Operator-2026!", "role": "operator",
        }, cookie)
        self.assertEqual(status, 201)
        status, operator_headers, operator_login = self.request("POST", "/api/admin/login", {
            "username": "limited_operator", "password": "Operator-2026!",
        })
        self.assertEqual(status, 200)
        operator_cookie = operator_headers["set-cookie"].split(";", 1)[0]
        status, _, forbidden = self.request("GET", "/api/admin/settings/email", cookie=operator_cookie)
        self.assertEqual((status, forbidden["error"]), (403, "当前账号没有执行此操作的权限"))

    def test_request_body_limit(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        connection.putrequest("POST", "/api/admin/login")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "1048577")
        connection.endheaders()
        response = connection.getresponse()
        result = json.loads(response.read().decode("utf-8"))
        connection.close()
        self.assertEqual((response.status, result["error"]), (413, "请求内容超过 1 MB 限制"))

    def test_complete_booking_http_flow(self):
        cookie = self.login()
        booking_date = (date.today() + timedelta(days=1)).isoformat()
        task_input = {
            "name": "HTTP 集成任务",
            "title": "HTTP 集成预约",
            "description": "覆盖完整预约流程",
            "location": "测试地点",
            "contact": "",
            "timezone": "Asia/Shanghai",
            "slotMinutes": 15,
            "openingStrategy": "any",
            "maxReschedules": 1,
            "policyText": "如需调整，请通过安全管理链接操作。",
            "privacyText": "信息仅用于本次预约。",
            "dates": [booking_date],
            "periods": [{"label": "上午", "start": "09:00", "end": "10:00"}],
            "fields": [{"key": "department", "label": "部门", "type": "text", "options": [], "required": True}],
        }
        status, _, task = self.request("POST", "/api/admin/tasks", task_input, cookie)
        self.assertEqual(status, 201)
        task_id, public_id = task["id"], task["publicId"]
        status, _, result = self.request(
            "POST", f"/api/admin/tasks/{task_id}/status", {"status": "published"}, cookie
        )
        self.assertEqual((status, result["status"]), (200, "published"))
        status, _, public_task = self.request("GET", f"/api/public/tasks/{public_id}")
        self.assertEqual(status, 200)
        available = next(slot for slot in public_task["slots"] if slot["status"] == "available")

        email = "flow@example.com"
        status, _, sent = self.request("POST", "/api/verification/send", {"email": email})
        self.assertEqual((status, sent["devCode"]), (200, "123456"))
        status, _, verified = self.request(
            "POST", "/api/verification/verify", {"email": email, "code": "123456"}
        )
        self.assertEqual(status, 200)
        token = verified["verificationToken"]
        booking_payload = {
            "name": "流程用户",
            "email": email,
            "verificationToken": token,
            "date": available["date"],
            "time": available["time"],
            "answers": {"department": "研发"},
        }
        status, _, booking = self.request(
            "POST", f"/api/public/tasks/{public_id}/bookings", booking_payload, extra_headers={"Idempotency-Key": "flow-create-1"}
        )
        self.assertEqual((status, booking["status"]), (201, "confirmed"))
        self.assertTrue(booking["bookingRef"].startswith("APT-"))
        self.assertEqual(booking["notificationStatus"], "queued")
        status, _, duplicate = self.request(
            "POST", f"/api/public/tasks/{public_id}/bookings", booking_payload, extra_headers={"Idempotency-Key": "flow-create-1"}
        )
        self.assertEqual((status, duplicate["id"]), (200, booking["id"]))
        status, _, managed = self.request("GET", f"/api/public/bookings/{booking['bookingRef']}?token={booking['manageToken']}")
        self.assertEqual((status, managed["booking"]["id"]), (200, booking["id"]))
        status, headers, calendar = self.request("GET", f"/api/public/bookings/{booking['bookingRef']}.ics?token={booking['manageToken']}")
        self.assertEqual(status, 200)
        self.assertIn("text/calendar", headers["content-type"])
        self.assertIn(f"UID:{booking['bookingRef']}@booking.local", calendar)
        status, _, found = self.request(
            "POST",
            f"/api/public/tasks/{public_id}/bookings/lookup",
            {"email": email, "verificationToken": token},
        )
        self.assertEqual((status, found["booking"]["id"]), (200, booking["id"]))
        next_slot = next(
            slot for slot in public_task["slots"]
            if slot["status"] == "available" and slot["time"] != available["time"]
        )
        status, _, moved = self.request(
            "PUT",
            f"/api/public/tasks/{public_id}/bookings/by-email",
            {
                "email": email,
                "verificationToken": token,
                "date": next_slot["date"],
                "time": next_slot["time"],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual((moved["date"], moved["time"], moved["rescheduleCount"]), (next_slot["date"], next_slot["time"], 1))
        self.assertEqual(moved["reschedules"][0]["oldTime"], available["time"])
        third_slot = next(slot for slot in public_task["slots"] if slot["status"] == "available" and slot["time"] not in (available["time"], next_slot["time"]))
        status, _, blocked_move = self.request("PUT", f"/api/public/tasks/{public_id}/bookings/by-email", {
            "email": email, "verificationToken": token, "date": third_slot["date"], "time": third_slot["time"],
        })
        self.assertEqual(status, 409)
        self.assertIn("最多改期", blocked_move["error"])
        status, _, found = self.request(
            "POST",
            f"/api/public/tasks/{public_id}/bookings/lookup",
            {"email": email, "verificationToken": token},
        )
        self.assertEqual(found["booking"]["time"], next_slot["time"])
        self.assertEqual(len(found["reschedules"]), 1)
        status, _, _ = self.request(
            "DELETE",
            f"/api/public/tasks/{public_id}/bookings/by-email",
            {"email": email, "verificationToken": token, "reason": "集成测试取消"},
        )
        self.assertEqual(status, 200)
        status, _, found = self.request(
            "POST",
            f"/api/public/tasks/{public_id}/bookings/lookup",
            {"email": email, "verificationToken": token},
        )
        self.assertEqual((status, found["booking"]), (200, None))
        status, _, proxy = self.request("POST", f"/api/admin/tasks/{task_id}/bookings", {
            "name": "前台代约", "email": "proxy@example.com", "date": third_slot["date"], "time": third_slot["time"], "internalNotes": "电话确认",
        }, cookie)
        self.assertEqual((status, proxy["status"]), (201, "confirmed"))
        fourth_slot = next(slot for slot in public_task["slots"] if slot["status"] == "available" and slot["time"] not in (available["time"], next_slot["time"], third_slot["time"]))
        status, _, proxy_moved = self.request("PUT", f"/api/admin/tasks/{task_id}/bookings/{proxy['id']}", {
            "date": fourth_slot["date"], "time": fourth_slot["time"], "internalNotes": "管理员协助改期",
        }, cookie)
        self.assertEqual((status, proxy_moved["time"], proxy_moved["rescheduleCount"]), (200, fourth_slot["time"], 1))
        status, _, timeline = self.request("GET", f"/api/admin/tasks/{task_id}/bookings/{proxy['id']}", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual([event["eventType"] for event in timeline], ["created", "admin_updated"])
        status, _, notifications = self.request("GET", "/api/admin/notifications", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(notifications), 3)

    def test_published_version_stays_live_until_explicit_publish(self):
        cookie = self.login()
        booking_date = (date.today() + timedelta(days=1)).isoformat()
        task_input = {
            "name": "版本任务",
            "title": "线上标题 v1",
            "description": "第一版",
            "location": "",
            "contact": "",
            "timezone": "Asia/Shanghai",
            "slotMinutes": 15,
            "openingStrategy": "any",
            "minAdvanceMinutes": 0,
            "maxAdvanceDays": 30,
            "dates": [booking_date],
            "periods": [{"label": "上午", "start": "09:00", "end": "10:00"}],
            "dateOverrides": [],
            "fields": [],
        }
        status, _, task = self.request("POST", "/api/admin/tasks", task_input, cookie)
        self.assertEqual(status, 201)
        task_id, public_id = task["id"], task["publicId"]
        status, _, _ = self.request(
            "POST", f"/api/admin/tasks/{task_id}/status", {"status": "published"}, cookie
        )
        self.assertEqual(status, 200)
        status, _, live_v1 = self.request("GET", f"/api/public/tasks/{public_id}")
        self.assertEqual((status, live_v1["title"]), (200, "线上标题 v1"))
        status, _, _ = self.request(
            "POST",
            f"/api/admin/tasks/{task_id}/blocked-slots",
            {"date": booking_date, "time": "09:00"},
            cookie,
        )
        self.assertEqual(status, 201)

        task_input["title"] = "草稿标题 v2"
        task_input["description"] = "第二版尚未发布"
        task_input["periods"] = [{"label": "新上午", "start": "10:00", "end": "11:00"}]
        status, _, draft = self.request("PUT", f"/api/admin/tasks/{task_id}", task_input, cookie)
        self.assertEqual(status, 200)
        self.assertTrue(draft["hasUnpublishedChanges"])
        self.assertEqual(draft["publishedVersion"], 1)
        status, _, still_v1 = self.request("GET", f"/api/public/tasks/{public_id}")
        self.assertEqual((status, still_v1["title"]), (200, "线上标题 v1"))
        old_slot = next(slot for slot in still_v1["slots"] if slot["time"] == "09:00")
        self.assertEqual(old_slot["status"], "occupied")

        status, _, published = self.request("POST", f"/api/admin/tasks/{task_id}/publish", {}, cookie)
        self.assertEqual((status, published["version"]), (200, 2))
        self.assertEqual(published["removedBlocks"], 1)
        status, _, live_v2 = self.request("GET", f"/api/public/tasks/{public_id}")
        self.assertEqual((status, live_v2["title"]), (200, "草稿标题 v2"))
        self.assertNotIn("09:00", [slot["time"] for slot in live_v2["slots"]])
        status, _, versions = self.request("GET", f"/api/admin/tasks/{task_id}/versions", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual([item["version"] for item in versions], [2, 1])


if __name__ == "__main__":
    unittest.main()
