"""HTTP transport and API route handlers."""

import csv
import hmac
import io
import json
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import (
    ADMIN_LOGIN_FAILURES, ADMIN_PASSWORD, ADMIN_PASSWORD_IS_DEFAULT,
    ADMIN_SESSION_COOKIE, DEV_VERIFICATION_CODE,
    EMAIL_RE, MAX_VERIFICATION_ATTEMPTS, ROOT, STATUS_TRANSITIONS,
    VERIFICATION_TTL_MINUTES,
)
from .database import connect, generate_public_id, now_text
from .domain import (
    config_conflicts, create_email_session, email_session_is_valid,
    booking_reschedule_history, format_booking, generate_slots, load_public_task, load_task,
    parse_datetime, save_task_config, slot_map_for_task, task_payload, validate_task_input,
)
from .emailer import send_verification_email
from .audit import record_booking_event
from .notifications import enqueue_notification, process_notification
from .security import booking_manage_token
from .utils import normalize_email
from .config import PUBLIC_BASE_URL

class PublicRoutesMixin:
    def public_booking_ics(self, booking_ref, token):
        with connect() as conn:
            row = self.booking_access(conn, booking_ref, token)
            if not row:
                self.send_json({"error": "日历链接无效"}, HTTPStatus.FORBIDDEN); return
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
            sequence = conn.execute("SELECT COUNT(*) count FROM booking_reschedules WHERE booking_id=?", (row["id"],)).fetchone()["count"]
        start = f"{row['date'].replace('-', '')}T{row['time'].replace(':', '')}00"
        end = f"{row['date'].replace('-', '')}T{row['end_time'].replace(':', '')}00"
        clean = lambda value: str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
        body = "\r\n".join(["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//Trusted Booking V3//CN","BEGIN:VEVENT",f"UID:{booking_ref}@booking.local",f"SEQUENCE:{sequence}",f"DTSTART;TZID={task['timezone']}:{start}",f"DTEND;TZID={task['timezone']}:{end}",f"SUMMARY:{clean(task['title'])}",f"LOCATION:{clean(task['location'])}",f"STATUS:{'CONFIRMED' if row['status']=='confirmed' else 'CANCELLED'}","END:VEVENT","END:VCALENDAR",""]).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/calendar; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename={booking_ref}.ics")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def booking_access(self, conn, booking_ref, token):
        row = conn.execute("""SELECT b.*,t.public_id,
            (SELECT COUNT(*) FROM booking_reschedules r WHERE r.booking_id=b.id) reschedule_count
            FROM bookings b JOIN tasks t ON t.id=b.task_id WHERE b.booking_ref=?""", (booking_ref,)).fetchone()
        settings = conn.execute("SELECT app_secret FROM system_settings WHERE id=1").fetchone()
        if not row or not settings or not hmac.compare_digest(booking_manage_token(settings["app_secret"], booking_ref), token or ""):
            return None
        return row

    def notification_payload(self, conn, task, booking):
        settings = conn.execute("SELECT app_secret FROM system_settings WHERE id=1").fetchone()
        token = booking_manage_token(settings["app_secret"], booking["bookingRef"])
        return {
            "task": {"title": task["title"], "location": task.get("location", "")},
            "booking": booking,
            "manageUrl": f"{PUBLIC_BASE_URL}/manage/{booking['bookingRef']}?token={token}",
            "calendarUrl": f"{PUBLIC_BASE_URL}/api/public/bookings/{booking['bookingRef']}.ics?token={token}",
        }, token

    def public_get_task(self, public_id):
        with connect() as conn:
            task = load_public_task(conn, public_id)
            if not task:
                self.send_json({"error": "预约任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            slots = slot_map_for_task(conn, task)
            current_booking = None
        result = task_payload(task)
        result["slots"] = [
            {key: slot[key] for key in ("date", "time", "endTime", "periodId", "periodLabel", "status", "availabilityReason")}
            | {"status": "occupied" if slot["status"] in ("booked", "blocked") else slot["status"]}
            for slot in slots
        ]
        self.send_json(result)

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
                with connect() as conn:
                    send_verification_email(conn, email, code)
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
        idempotency_key = str(self.headers.get("Idempotency-Key", payload.get("idempotencyKey", ""))).strip()[:128]
        if not name or not EMAIL_RE.match(email):
            self.send_json({"error": "请输入姓名和有效邮箱"}, HTTPStatus.BAD_REQUEST)
            return
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = load_public_task(conn, public_id)
            if not task:
                self.send_json({"error": "预约任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            if task["status"] != "published":
                self.send_json({"error": "该任务当前不可预约"}, HTTPStatus.CONFLICT)
                return
            if not email_session_is_valid(conn, email, str(payload.get("verificationToken", ""))):
                self.send_json({"error": "请先完成邮箱验证"}, HTTPStatus.FORBIDDEN)
                return
            if idempotency_key:
                existing = conn.execute("SELECT * FROM bookings WHERE task_id=? AND idempotency_key=?", (task["id"], idempotency_key)).fetchone()
                if existing:
                    if (existing["name"], existing["email"].lower(), existing["date"], existing["time"]) != (name, email.lower(), date_value, time_value):
                        self.send_json({"error": "幂等键已用于不同的预约请求"}, HTTPStatus.CONFLICT)
                        return
                    result = format_booking(existing)
                    notification_payload, manage_token = self.notification_payload(conn, task, result)
                    result["manageToken"] = manage_token
                    result["managePath"] = notification_payload["manageUrl"]
                    self.send_json(result)
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
                booking_ref = "APT-" + secrets.token_hex(5).upper()
                policy = {
                    "cancelCutoffMinutes": int(task.get("cancel_cutoff_minutes", 0)),
                    "rescheduleCutoffMinutes": int(task.get("reschedule_cutoff_minutes", 0)),
                    "maxReschedules": int(task.get("max_reschedules", 10)),
                    "policyText": task.get("policy_text", ""),
                }
                cursor = conn.execute(
                    """
                    INSERT INTO bookings (task_id,name,email,date,time,end_time,answers_json,booking_ref,policy_json,idempotency_key,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (task["id"],name,email,date_value,time_value,selected["endTime"],json.dumps(normalized_answers, ensure_ascii=False),booking_ref,json.dumps(policy, ensure_ascii=False),idempotency_key,now_text()),
                )
            except sqlite3.IntegrityError:
                self.send_json({"error": "该邮箱已预约此任务，或该时间刚被占用"}, HTTPStatus.CONFLICT)
                return
            row = conn.execute("SELECT * FROM bookings WHERE id=?", (cursor.lastrowid,)).fetchone()
            result = format_booking(row)
            notification_payload, manage_token = self.notification_payload(conn, task, result)
            notification_id = enqueue_notification(conn, f"booking:{row['id']}:confirmed", row["id"], task["id"], email, "booking_confirmed", notification_payload)
            record_booking_event(conn, row["id"], task["id"], "created", actor_type="customer", actor_id=email, request_key=idempotency_key or None, details={"date": date_value, "time": time_value})
        sent = process_notification(notification_id)
        result["manageToken"] = manage_token
        result["managePath"] = notification_payload["manageUrl"]
        result["notificationStatus"] = "sent" if sent else "queued"
        self.send_json(result, HTTPStatus.CREATED)

    def public_lookup_booking(self, public_id):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        email = normalize_email(payload.get("email"))
        with connect() as conn:
            task = load_public_task(conn, public_id)
            if not task:
                self.send_json({"error": "预约任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            if not email_session_is_valid(conn, email, str(payload.get("verificationToken", ""))):
                self.send_json({"error": "请先完成邮箱验证"}, HTTPStatus.FORBIDDEN)
                return
            row = conn.execute(
                """
                SELECT b.*,(SELECT COUNT(*) FROM booking_reschedules r WHERE r.booking_id=b.id) reschedule_count
                FROM bookings b WHERE b.task_id=? AND b.email=? COLLATE NOCASE AND b.status='confirmed'
                ORDER BY b.created_at DESC LIMIT 1
                """,
                (task["id"], email),
            ).fetchone()
            history = booking_reschedule_history(conn, row["id"]) if row else []
        self.send_json({"booking": format_booking(row) if row else None, "reschedules": history})

    def public_reschedule_booking(self, public_id):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        email = normalize_email(payload.get("email"))
        date_value = str(payload.get("date", ""))
        time_value = str(payload.get("time", ""))
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = load_public_task(conn, public_id)
            if not task:
                self.send_json({"error": "预约任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            if task["status"] != "published":
                self.send_json({"error": "该任务当前不可改期"}, HTTPStatus.CONFLICT)
                return
            if not email_session_is_valid(conn, email, str(payload.get("verificationToken", ""))):
                self.send_json({"error": "请先完成邮箱验证"}, HTTPStatus.FORBIDDEN)
                return
            booking = conn.execute(
                "SELECT * FROM bookings WHERE task_id=? AND email=? COLLATE NOCASE AND status='confirmed'",
                (task["id"], email),
            ).fetchone()
            if not booking:
                self.send_json({"error": "该邮箱在此任务中没有有效预约"}, HTTPStatus.NOT_FOUND)
                return
            policy = json.loads(booking["policy_json"] or "{}")
            count = conn.execute("SELECT COUNT(*) count FROM booking_reschedules WHERE booking_id=?", (booking["id"],)).fetchone()["count"]
            if count >= int(policy.get("maxReschedules", task.get("max_reschedules", 10))):
                self.send_json({"error": "该预约已达到最多改期次数"}, HTTPStatus.CONFLICT)
                return
            cutoff = int(policy.get("rescheduleCutoffMinutes", task.get("reschedule_cutoff_minutes", 0)))
            start_at = datetime.strptime(f"{booking['date']} {booking['time']}", "%Y-%m-%d %H:%M")
            if datetime.now() + timedelta(minutes=cutoff) >= start_at:
                self.send_json({"error": "已超过允许改期的截止时间"}, HTTPStatus.CONFLICT)
                return
            if booking["date"] == date_value and booking["time"] == time_value:
                self.send_json({"error": "请选择与当前预约不同的时间"}, HTTPStatus.BAD_REQUEST)
                return
            selected = next(
                (
                    slot
                    for slot in slot_map_for_task(conn, task)
                    if slot["date"] == date_value and slot["time"] == time_value
                ),
                None,
            )
            if not selected:
                self.send_json({"error": "所选时间不属于当前发布版本"}, HTTPStatus.BAD_REQUEST)
                return
            if selected["status"] != "available":
                self.send_json({"error": "该时间当前不可改约，请刷新后重试"}, HTTPStatus.CONFLICT)
                return
            changed_at = now_text()
            try:
                conn.execute(
                    """
                    INSERT INTO booking_reschedules (
                        booking_id,task_id,old_date,old_time,old_end_time,
                        new_date,new_time,new_end_time,actor,changed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        booking["id"], task["id"], booking["date"], booking["time"], booking["end_time"],
                        date_value, time_value, selected["endTime"], "user", changed_at,
                    ),
                )
                conn.execute(
                    "UPDATE bookings SET date=?,time=?,end_time=? WHERE id=? AND status='confirmed'",
                    (date_value, time_value, selected["endTime"], booking["id"]),
                )
            except sqlite3.IntegrityError:
                self.send_json({"error": "该时间刚被占用，请选择其他时间"}, HTTPStatus.CONFLICT)
                return
            row = conn.execute(
                """SELECT b.*,(SELECT COUNT(*) FROM booking_reschedules r WHERE r.booking_id=b.id) reschedule_count
                FROM bookings b WHERE b.id=?""",
                (booking["id"],),
            ).fetchone()
            history = booking_reschedule_history(conn, booking["id"])
            result = format_booking(row)
            notification_payload, _ = self.notification_payload(conn, task, result)
            notification_id = enqueue_notification(conn, f"booking:{booking['id']}:rescheduled:{len(history)}", booking["id"], task["id"], email, "booking_rescheduled", notification_payload)
            record_booking_event(conn, booking["id"], task["id"], "rescheduled", actor_type="customer", actor_id=email, details={"oldDate": booking["date"], "oldTime": booking["time"], "newDate": date_value, "newTime": time_value})
        process_notification(notification_id)
        result["reschedules"] = history
        self.send_json(result)

    def public_cancel_booking(self, public_id):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "请求格式错误"}, HTTPStatus.BAD_REQUEST)
            return
        email = normalize_email(payload.get("email"))
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = load_public_task(conn, public_id)
            if not task:
                self.send_json({"error": "预约任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            if not email_session_is_valid(conn, email, str(payload.get("verificationToken", ""))):
                self.send_json({"error": "请先完成邮箱验证"}, HTTPStatus.FORBIDDEN)
                return
            reason = str(payload.get("reason", "用户取消")).strip() or "用户取消"
            booking = conn.execute("SELECT * FROM bookings WHERE task_id=? AND email=? COLLATE NOCASE AND status='confirmed'", (task["id"], email)).fetchone()
            if not booking:
                self.send_json({"error": "该邮箱在此任务中没有预约"}, HTTPStatus.NOT_FOUND)
                return
            policy = json.loads(booking["policy_json"] or "{}")
            cutoff = int(policy.get("cancelCutoffMinutes", task.get("cancel_cutoff_minutes", 0)))
            start_at = datetime.strptime(f"{booking['date']} {booking['time']}", "%Y-%m-%d %H:%M")
            if datetime.now() + timedelta(minutes=cutoff) >= start_at:
                self.send_json({"error": "已超过允许取消的截止时间"}, HTTPStatus.CONFLICT)
                return
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
            cancelled = conn.execute("SELECT * FROM bookings WHERE id=?", (booking["id"],)).fetchone()
            result = format_booking(cancelled)
            notification_payload, _ = self.notification_payload(conn, task, result)
            notification_id = enqueue_notification(conn, f"booking:{booking['id']}:cancelled", booking["id"], task["id"], email, "booking_cancelled", notification_payload)
            record_booking_event(conn, booking["id"], task["id"], "cancelled", actor_type="customer", actor_id=email, details={"reason": reason})
        process_notification(notification_id)
        self.send_json({"ok": True})

    def public_manage_get(self, booking_ref, token):
        with connect() as conn:
            row = self.booking_access(conn, booking_ref, token)
            if not row:
                self.send_json({"error": "管理链接无效或已失效"}, HTTPStatus.FORBIDDEN)
                return
            history = booking_reschedule_history(conn, row["id"])
            verification_token, verification_expires = create_email_session(conn, row["email"])
        self.send_json({"booking": format_booking(row), "reschedules": history, "publicId": row["public_id"], "verificationToken": verification_token, "verificationExpiresAt": verification_expires.strftime("%Y-%m-%d %H:%M:%S")})
