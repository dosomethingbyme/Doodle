"""HTTP transport and API route handlers."""

import csv
import io
import json
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from http import HTTPStatus
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import (
    ADMIN_LOGIN_FAILURES, ADMIN_PASSWORD, ADMIN_PASSWORD_IS_DEFAULT,
    ADMIN_SESSION_COOKIE, DEV_VERIFICATION_CODE,
    EMAIL_RE, MAX_VERIFICATION_ATTEMPTS, ROOT, STATUS_TRANSITIONS,
    VALID_STATUSES, VERIFICATION_TTL_MINUTES,
)
from .config import PUBLIC_BASE_URL
from .auth import authenticate, create_session, revoke_session, revoke_user_sessions
from .audit import record_audit, record_booking_event
from .database import connect, generate_public_id, now_text
from .domain import (
    config_conflicts, create_email_session, email_session_is_valid,
    format_booking, generate_slots, load_public_task, load_task, parse_datetime,
    publish_task_version, save_task_config, slot_map_for_task, task_booking_conflicts,
    task_payload, validate_task_input,
)
from .utils import normalize_email
from .settings import public_email_settings, save_email_settings
from .notifications import enqueue_notification, retry_due, process_notification
from .emailer import send_email_message
from .security import booking_manage_token, hash_password
from .audit import booking_timeline
from email.message import EmailMessage


def cleanup_invalid_blocks(conn, task):
    valid = {(slot["date"], slot["time"]) for slot in generate_slots(task)}
    rows = conn.execute(
        "SELECT id,date,time FROM blocked_slots WHERE task_id=?",
        (task["id"],),
    ).fetchall()
    invalid = [row for row in rows if (row["date"], row["time"]) not in valid]
    for row in invalid:
        conn.execute("DELETE FROM blocked_slots WHERE id=?", (row["id"],))
    return len(invalid)


class AdminRoutesMixin:
    def admin_notification_payload(self, conn, task, booking):
        secret = conn.execute("SELECT app_secret FROM system_settings WHERE id=1").fetchone()["app_secret"]
        token = booking_manage_token(secret, booking["bookingRef"])
        return {"task": {"title": task["title"], "location": task.get("location", "")}, "booking": booking, "manageUrl": f"{PUBLIC_BASE_URL}/manage/{booking['bookingRef']}?token={token}", "calendarUrl": f"{PUBLIC_BASE_URL}/api/public/bookings/{booking['bookingRef']}.ics?token={token}"}

    def admin_create_booking(self, task_id):
        try:
            payload = self.read_json()
            name = str(payload.get("name", "")).strip()
            email = normalize_email(payload.get("email"))
            date_value, time_value = str(payload.get("date", "")), str(payload.get("time", ""))
            if not name or not EMAIL_RE.match(email):
                raise ValueError("请输入姓名和有效邮箱")
            with connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                task = load_task(conn, task_id=task_id)
                if not task:
                    self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND); return
                selected = next((slot for slot in slot_map_for_task(conn, task) if slot["date"] == date_value and slot["time"] == time_value), None)
                if not selected or selected["status"] not in ("available", "closed") or selected.get("booking") or selected.get("blockedRow"):
                    self.send_json({"error": "该时间不可代客预约"}, HTTPStatus.CONFLICT); return
                reference = "APT-" + secrets.token_hex(5).upper()
                policy = {"cancelCutoffMinutes": task.get("cancel_cutoff_minutes", 0), "rescheduleCutoffMinutes": task.get("reschedule_cutoff_minutes", 0), "maxReschedules": task.get("max_reschedules", 10), "policyText": task.get("policy_text", "")}
                cursor = conn.execute("""INSERT INTO bookings(task_id,name,email,date,time,end_time,answers_json,booking_ref,policy_json,internal_notes,created_by_user_id,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (task_id,name,email,date_value,time_value,selected["endTime"],json.dumps(payload.get("answers", {}), ensure_ascii=False),reference,json.dumps(policy, ensure_ascii=False),str(payload.get("internalNotes", ""))[:2000],self.current_session()["user_id"],now_text()))
                row = conn.execute("SELECT * FROM bookings WHERE id=?", (cursor.lastrowid,)).fetchone()
                result = format_booking(row)
                event_id = enqueue_notification(conn, f"booking:{row['id']}:confirmed", row["id"], task_id, email, "booking_confirmed", self.admin_notification_payload(conn, task, result))
                record_booking_event(conn, row["id"], task_id, "created", actor_type="admin", actor_id=self.current_session()["user_id"], details={"date": date_value, "time": time_value})
                record_audit(conn, "booking_created", "booking", row["id"], actor_user_id=self.current_session()["user_id"], actor_label=self.current_session()["username"])
        except (json.JSONDecodeError, ValueError, sqlite3.IntegrityError) as exc:
            self.send_json({"error": str(exc) or "该邮箱或时间已被占用"}, HTTPStatus.BAD_REQUEST); return
        process_notification(event_id)
        self.send_json(result, HTTPStatus.CREATED)

    def admin_update_booking(self, task_id, booking_id):
        try:
            payload = self.read_json()
            with connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                task = load_task(conn, task_id=task_id)
                booking = conn.execute("SELECT * FROM bookings WHERE id=? AND task_id=? AND status='confirmed'", (booking_id, task_id)).fetchone()
                if not task or not booking:
                    self.send_json({"error": "有效预约不存在"}, HTTPStatus.NOT_FOUND); return
                name = str(payload.get("name", booking["name"])).strip()
                email = normalize_email(payload.get("email", booking["email"]))
                date_value, time_value = str(payload.get("date", booking["date"])), str(payload.get("time", booking["time"]))
                selected = next((slot for slot in generate_slots(task) if slot["date"] == date_value and slot["time"] == time_value), None)
                if not name or not EMAIL_RE.match(email) or not selected:
                    raise ValueError("预约信息或时间无效")
                if (date_value, time_value) != (booking["date"], booking["time"]):
                    conflict = conn.execute("SELECT 1 FROM bookings WHERE task_id=? AND date=? AND time=? AND status='confirmed' AND id!=?", (task_id,date_value,time_value,booking_id)).fetchone()
                    blocked = conn.execute("SELECT 1 FROM blocked_slots WHERE task_id=? AND date=? AND time=?", (task_id,date_value,time_value)).fetchone()
                    if conflict or blocked:
                        self.send_json({"error": "目标时间已被占用"}, HTTPStatus.CONFLICT); return
                    conn.execute("INSERT INTO booking_reschedules(booking_id,task_id,old_date,old_time,old_end_time,new_date,new_time,new_end_time,actor,changed_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (booking_id,task_id,booking["date"],booking["time"],booking["end_time"],date_value,time_value,selected["endTime"],f"admin:{self.current_session()['username']}",now_text()))
                conn.execute("UPDATE bookings SET name=?,email=?,date=?,time=?,end_time=?,internal_notes=? WHERE id=?", (name,email,date_value,time_value,selected["endTime"],str(payload.get("internalNotes", booking["internal_notes"]))[:2000],booking_id))
                row = conn.execute("SELECT b.*,(SELECT COUNT(*) FROM booking_reschedules r WHERE r.booking_id=b.id) reschedule_count FROM bookings b WHERE b.id=?", (booking_id,)).fetchone()
                result = format_booking(row)
                count = result["rescheduleCount"]
                event_id = enqueue_notification(conn, f"booking:{booking_id}:admin-updated:{now_text()}:{secrets.token_hex(2)}", booking_id, task_id, email, "booking_rescheduled" if count else "booking_confirmed", self.admin_notification_payload(conn, task, result))
                record_booking_event(conn, booking_id, task_id, "admin_updated", actor_type="admin", actor_id=self.current_session()["user_id"], details={"date": date_value, "time": time_value})
                record_audit(conn, "booking_updated", "booking", booking_id, actor_user_id=self.current_session()["user_id"], actor_label=self.current_session()["username"])
        except (json.JSONDecodeError, ValueError, sqlite3.IntegrityError) as exc:
            self.send_json({"error": str(exc) or "预约更新失败"}, HTTPStatus.BAD_REQUEST); return
        process_notification(event_id)
        self.send_json(result)
    def admin_email_settings(self):
        with connect() as conn:
            self.send_json(public_email_settings(conn))

    def admin_save_email_settings(self):
        try:
            payload = self.read_json()
            with connect() as conn:
                save_email_settings(conn, payload, self.current_session()["user_id"])
                record_audit(conn, "email_settings_updated", "system_settings", 1, actor_user_id=self.current_session()["user_id"], actor_label=self.current_session()["username"])
                result = public_email_settings(conn)
        except (json.JSONDecodeError, ValueError) as exc:
            self.send_json({"error": str(exc) or "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json(result)

    def admin_test_email(self):
        try:
            recipient = normalize_email(self.read_json().get("recipient"))
            if not EMAIL_RE.match(recipient):
                raise ValueError("请输入有效的测试收件邮箱")
            with connect() as conn:
                message = EmailMessage()
                settings = public_email_settings(conn)
                message["Subject"] = "预约系统邮件配置测试"
                message["From"] = settings["from"]
                message["To"] = recipient
                message.set_content("邮件服务器配置有效，预约系统可以发送通知。")
                send_email_message(conn, message)
        except Exception as exc:
            self.send_json({"error": f"测试邮件发送失败：{exc}"}, HTTPStatus.BAD_GATEWAY)
            return
        self.send_json({"ok": True})

    def admin_notifications(self, query):
        status = query.get("status", [""])[0]
        clause = "WHERE status=?" if status else ""
        params = (status,) if status else ()
        with connect() as conn:
            rows = conn.execute(f"SELECT * FROM notification_outbox {clause} ORDER BY id DESC LIMIT 200", params).fetchall()
        self.send_json([{**dict(row), "payload_json": None} for row in rows])

    def admin_audit(self):
        with connect() as conn:
            rows = conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 200").fetchall()
        self.send_json([dict(row) for row in rows])

    def admin_agenda(self, query):
        target = query.get("date", [date.today().isoformat()])[0]
        with connect() as conn:
            rows = conn.execute(
                """SELECT b.*,t.name task_name,t.public_id FROM bookings b JOIN tasks t ON t.id=b.task_id
                WHERE b.date=? AND b.status='confirmed' ORDER BY b.time,t.name""", (target,)
            ).fetchall()
        self.send_json([format_booking(row) | {"taskName": row["task_name"], "publicId": row["public_id"]} for row in rows])

    def admin_users(self):
        with connect() as conn:
            rows = conn.execute("SELECT id,username,role,is_active,created_at,last_login_at FROM admin_users ORDER BY id").fetchall()
        self.send_json([dict(row) for row in rows])

    def admin_create_user(self):
        try:
            payload = self.read_json()
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))
            role = str(payload.get("role", "operator"))
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username) or len(password) < 10 or role not in ("owner", "operator"):
                raise ValueError("账号需 3–40 位，密码至少 10 位，角色必须有效")
            with connect() as conn:
                timestamp = now_text()
                cursor = conn.execute("INSERT INTO admin_users(username,password_hash,role,is_active,created_at,updated_at) VALUES (?,?,?,1,?,?)", (username, hash_password(password), role, timestamp, timestamp))
                record_audit(conn, "admin_user_created", "admin_user", cursor.lastrowid, actor_user_id=self.current_session()["user_id"], actor_label=self.current_session()["username"], details={"username": username, "role": role})
        except (json.JSONDecodeError, ValueError, sqlite3.IntegrityError) as exc:
            self.send_json({"error": str(exc) or "账号已存在"}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json({"ok": True}, HTTPStatus.CREATED)

    def admin_update_user(self, user_id):
        try:
            payload = self.read_json()
            with connect() as conn:
                user = conn.execute("SELECT * FROM admin_users WHERE id=?", (user_id,)).fetchone()
                if not user:
                    self.send_json({"error": "管理员不存在"}, HTTPStatus.NOT_FOUND); return
                role = str(payload.get("role", user["role"]))
                active = int(bool(payload.get("isActive", user["is_active"])))
                password = str(payload.get("password", ""))
                if role not in ("owner", "operator") or (password and len(password) < 10):
                    raise ValueError("角色无效或新密码不足 10 位")
                if user_id == self.current_session()["user_id"] and not active:
                    raise ValueError("不能停用当前登录账号")
                password_hash = hash_password(password) if password else user["password_hash"]
                conn.execute("UPDATE admin_users SET role=?,is_active=?,password_hash=?,updated_at=? WHERE id=?", (role,active,password_hash,now_text(),user_id))
                if password or not active or role != user["role"]:
                    revoke_user_sessions(conn, user_id)
                record_audit(conn, "admin_user_updated", "admin_user", user_id, actor_user_id=self.current_session()["user_id"], actor_label=self.current_session()["username"], details={"role": role, "isActive": bool(active), "passwordChanged": bool(password)})
        except (json.JSONDecodeError, ValueError) as exc:
            self.send_json({"error": str(exc) or "请求格式错误"}, HTTPStatus.BAD_REQUEST); return
        self.send_json({"ok": True})

    def admin_booking_timeline(self, booking_id):
        with connect() as conn:
            self.send_json(booking_timeline(conn, booking_id))

    def admin_retry_notifications(self, notification_id=None):
        result = {"processed": 1, "sent": int(process_notification(notification_id))} if notification_id else retry_due(force=True)
        self.send_json(result)
    def admin_login(self):
        client_ip = self.client_address[0]
        cutoff = datetime.now() - timedelta(minutes=5)
        failures = [value for value in ADMIN_LOGIN_FAILURES.get(client_ip, []) if value >= cutoff]
        ADMIN_LOGIN_FAILURES[client_ip] = failures
        if len(failures) >= 5:
            self.send_json({"error": "登录失败次数过多，请 5 分钟后重试"}, HTTPStatus.TOO_MANY_REQUESTS)
            return
        try:
            payload = self.read_json()
            username = str(payload.get("username", "admin")).strip() or "admin"
            password = str(payload.get("password", ""))
        except (json.JSONDecodeError, ValueError):
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        with connect() as conn:
            user = authenticate(conn, username, password)
        if not user:
            failures.append(datetime.now())
            ADMIN_LOGIN_FAILURES[client_ip] = failures
            self.send_json({"error": "后台密码错误"}, HTTPStatus.UNAUTHORIZED)
            return
        ADMIN_LOGIN_FAILURES.pop(client_ip, None)
        with connect() as conn:
            token, csrf_token, expires_at = create_session(conn, user["id"])
            conn.execute("UPDATE admin_users SET last_login_at=? WHERE id=?", (now_text(), user["id"]))
            record_audit(conn, "login", "admin_user", user["id"], actor_user_id=user["id"], actor_label=user["username"], ip_address=self.client_address[0], user_agent=self.headers.get("User-Agent", ""))
        body = json.dumps({
            "ok": True,
            "csrfToken": csrf_token,
            "expiresAt": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
            "user": {"id": user["id"], "username": user["username"], "role": user["role"]},
        }, ensure_ascii=False).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
        self.send_header("Set-Cookie", f"{ADMIN_SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800{secure}")
        self.end_headers()
        self.wfile.write(body)

    def admin_logout(self):
        session = self.current_session()
        with connect() as conn:
            revoke_session(conn, self.session_cookie())
            if session:
                record_audit(conn, "logout", "admin_user", session["user_id"], actor_user_id=session["user_id"], actor_label=session["username"], ip_address=self.client_address[0], user_agent=self.headers.get("User-Agent", ""))
        body = b'{"ok":true}'
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", f"{ADMIN_SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

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
                    {key: slot[key] for key in ("date", "time", "endTime", "periodId", "periodLabel", "status", "availabilityReason")}
                    for slot in slots
                ]
                self.send_json(result)
                return
            if parts[4] == "bookings":
                status = query.get("status", ["all"])[0]
                search = query.get("search", [""])[0].strip().lower()
                start_date = query.get("startDate", [""])[0]
                end_date = query.get("endDate", [""])[0]
                clauses = ["b.task_id=?"]
                params = [task_id]
                if status in ("confirmed", "cancelled"):
                    clauses.append("b.status=?")
                    params.append(status)
                if start_date:
                    clauses.append("b.date>=?")
                    params.append(start_date)
                if end_date:
                    clauses.append("b.date<=?")
                    params.append(end_date)
                if search:
                    clauses.append("(lower(b.name) LIKE ? OR lower(b.email) LIKE ? OR lower(b.answers_json) LIKE ?)")
                    pattern = f"%{search}%"
                    params.extend([pattern, pattern, pattern])
                rows = conn.execute(
                    f"""SELECT b.*,
                        (SELECT COUNT(*) FROM booking_reschedules r WHERE r.booking_id=b.id) reschedule_count
                        FROM bookings b WHERE {' AND '.join(clauses)} ORDER BY b.date,b.time,b.created_at""",
                    params,
                ).fetchall()
                self.send_json([format_booking(row) for row in rows])
                return
            if parts[4] == "versions":
                rows = conn.execute(
                    "SELECT version,published_at FROM task_versions WHERE task_id=? ORDER BY version DESC",
                    (task_id,),
                ).fetchall()
                self.send_json([dict(row) for row in rows])
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
            record_audit(conn, "task_created", "task", task["id"], actor_user_id=self.current_session()["user_id"], actor_label=self.current_session()["username"], details={"name": task["name"]})
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
            existing = conn.execute("SELECT published_version FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not existing:
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
                "date_overrides": {
                    item["date"]: {"closed": item["closed"], "periods": item["periods"]}
                    for item in data.get("dateOverrides", [])
                },
            }
            valid_keys = {(slot["date"], slot["time"]) for slot in generate_slots(pseudo_task)}
            blocked_conflicts = [
                dict(row)
                for row in conn.execute("SELECT id, date, time FROM blocked_slots WHERE task_id=?", (task_id,)).fetchall()
                if (row["date"], row["time"]) not in valid_keys
            ]
            has_live_version = bool(existing["published_version"])
            if blocked_conflicts and not has_live_version and not payload.get("forceRemoveBlocks"):
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
            if not has_live_version:
                for row in blocked_conflicts:
                    conn.execute("DELETE FROM blocked_slots WHERE id=?", (row["id"],))
            task = load_task(conn, task_id=task_id)
            record_audit(conn, "task_updated", "task", task_id, actor_user_id=self.current_session()["user_id"], actor_label=self.current_session()["username"])
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
                if status == "published":
                    conflicts = task_booking_conflicts(conn, task)
                    if conflicts:
                        self.send_json(
                            {"error": f"当前草稿与 {len(conflicts)} 个已有预约冲突，请先调整草稿。", "conflicts": conflicts},
                            HTTPStatus.CONFLICT,
                        )
                        return
                    if task["published_version"] == 0 or task["has_unpublished_changes"]:
                        cleanup_invalid_blocks(conn, task)
                        publish_task_version(conn, task_id)
                conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (status, now_text(), task_id))
                record_audit(conn, "task_status_changed", "task", task_id, actor_user_id=self.current_session()["user_id"], actor_label=self.current_session()["username"], details={"status": status})
            self.send_json({"ok": True, "status": status})
            return
        if action == "publish":
            with connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                task = load_task(conn, task_id=task_id)
                if not task:
                    self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                    return
                if task["status"] not in ("published", "paused"):
                    self.send_json({"error": "只有进行中或暂停的任务可以发布新版本"}, HTTPStatus.CONFLICT)
                    return
                conflicts = task_booking_conflicts(conn, task)
                if conflicts:
                    self.send_json(
                        {"error": f"当前草稿与 {len(conflicts)} 个已有预约冲突，请先调整草稿。", "conflicts": conflicts},
                        HTTPStatus.CONFLICT,
                    )
                    return
                removed_blocks = cleanup_invalid_blocks(conn, task)
                version = publish_task_version(conn, task_id)
                record_audit(conn, "task_version_published", "task", task_id, actor_user_id=self.current_session()["user_id"], actor_label=self.current_session()["username"], details={"version": version})
            self.send_json({"ok": True, "version": version, "removedBlocks": removed_blocks})
            return
        if action == "copy":
            self.admin_copy_task(task_id)
            return
        if action == "blocked-slots":
            self.admin_block_slot(task_id)
            return
        if action == "bookings":
            self.admin_create_booking(task_id)
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
                    opening_strategy, min_advance_minutes, max_advance_days,cancel_cutoff_minutes,reschedule_cutoff_minutes,
                    max_reschedules,policy_text,privacy_text,retention_days,status,is_default,created_at,updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 0, ?, ?)
                """,
                (
                    generate_public_id(conn), f"{source['name']}（副本）", source["title"], source["description"], source["location"],
                    source["contact"], source["timezone"], source["slot_minutes"], source["opening_strategy"],
                    source["min_advance_minutes"], source["max_advance_days"],source["cancel_cutoff_minutes"],
                    source["reschedule_cutoff_minutes"],source["max_reschedules"],source["policy_text"],
                    source["privacy_text"],source["retention_days"],timestamp,timestamp,
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
            conn.executemany(
                "INSERT INTO task_date_overrides (task_id,date,is_closed,periods_json) VALUES (?,?,?,?)",
                [
                    (
                        new_id,
                        date_value,
                        int(rule.get("closed", False)),
                        json.dumps(rule.get("periods", []), ensure_ascii=False),
                    )
                    for date_value, rule in source.get("date_overrides", {}).items()
                ],
            )
            task = load_task(conn, task_id=new_id)
            record_audit(conn, "task_copied", "task", new_id, actor_user_id=self.current_session()["user_id"], actor_label=self.current_session()["username"], details={"sourceTaskId": task_id})
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
                record_audit(conn, "slot_blocked", "task", task_id, actor_user_id=self.current_session()["user_id"], actor_label=self.current_session()["username"], details={"date": date_value, "time": time_value})
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
            with connect() as conn:
                record_audit(conn, "slot_unblocked", "task", task_id, actor_user_id=self.current_session()["user_id"], actor_label=self.current_session()["username"], details={"date": str(payload.get("date", "")), "time": str(payload.get("time", ""))})
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
                booking = conn.execute("SELECT * FROM bookings WHERE task_id=? AND id=? AND status='confirmed'", (task_id, booking_id)).fetchone()
                task = load_task(conn, task_id=task_id)
                if not booking or not task:
                    self.send_json({"error": "预约不存在或已取消"}, HTTPStatus.NOT_FOUND); return
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
            with connect() as conn:
                row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
                result = format_booking(row)
                event_id = enqueue_notification(conn, f"booking:{booking_id}:cancelled", booking_id, task_id, row["email"], "booking_cancelled", self.admin_notification_payload(conn, task, result))
                record_booking_event(conn, booking_id, task_id, "cancelled", actor_type="admin", actor_id=self.current_session()["user_id"], details={"reason": reason})
                record_audit(conn, "booking_cancelled", "booking", booking_id, actor_user_id=self.current_session()["user_id"], actor_label=self.current_session()["username"])
            process_notification(event_id)
            self.send_json({"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

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
            writer.writerow({key: ("'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value) for key, value in item.items()})
        body = ("\ufeff" + output.getvalue()).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename=task-{task['public_id']}-bookings.csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
