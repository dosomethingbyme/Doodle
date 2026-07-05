"""Durable email outbox and retry processing."""

import json
from datetime import datetime, timedelta

from .database import connect, now_text
from .emailer import send_notification_email
from .security import booking_manage_token
from .config import PUBLIC_BASE_URL


def enqueue_notification(conn, event_key, booking_id, task_id, recipient, event_type, payload):
    timestamp = now_text()
    conn.execute(
        """INSERT OR IGNORE INTO notification_outbox
        (event_key,booking_id,task_id,recipient,event_type,payload_json,status,attempts,next_attempt_at,created_at)
        VALUES (?,?,?,?,?,?,'pending',0,?,?)""",
        (event_key, booking_id, task_id, recipient, event_type, json.dumps(payload, ensure_ascii=False), timestamp, timestamp),
    )
    row = conn.execute("SELECT id FROM notification_outbox WHERE event_key=?", (event_key,)).fetchone()
    return row["id"]


def process_notification(notification_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM notification_outbox WHERE id=?", (notification_id,)).fetchone()
        if not row or row["status"] == "sent":
            return True
        try:
            send_notification_email(conn, row["recipient"], row["event_type"], json.loads(row["payload_json"]))
        except Exception as exc:
            attempts = int(row["attempts"]) + 1
            delay = min(60, 2 ** min(attempts, 5))
            next_at = (datetime.now() + timedelta(minutes=delay)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE notification_outbox SET status='failed',attempts=?,next_attempt_at=?,last_error=? WHERE id=?",
                (attempts, next_at, str(exc)[:1000], notification_id),
            )
            return False
        conn.execute("UPDATE notification_outbox SET status='sent',attempts=attempts+1,sent_at=?,last_error='' WHERE id=?", (now_text(), notification_id))
        return True


def retry_due(limit=20, force=False):
    with connect() as conn:
        condition = "status!='sent'" if force else "status!='sent' AND next_attempt_at<=?"
        params = () if force else (now_text(),)
        ids = [row["id"] for row in conn.execute(f"SELECT id FROM notification_outbox WHERE {condition} ORDER BY id LIMIT ?", (*params, limit))]
    return {"processed": len(ids), "sent": sum(process_notification(item) for item in ids)}


def enqueue_due_reminders():
    now = datetime.now()
    with connect() as conn:
        setting = conn.execute("SELECT app_secret,reminder_minutes FROM system_settings WHERE id=1").fetchone()
        if not setting:
            return 0
        count = 0
        rows = conn.execute("""SELECT b.*,t.title,t.location FROM bookings b JOIN tasks t ON t.id=b.task_id
            WHERE b.status='confirmed' AND b.date>=?""", (now.date().isoformat(),)).fetchall()
        for row in rows:
            start = datetime.strptime(f"{row['date']} {row['time']}", "%Y-%m-%d %H:%M")
            delta = (start - now).total_seconds() / 60
            if not (0 < delta <= int(setting["reminder_minutes"])):
                continue
            booking = {"name": row["name"], "email": row["email"], "date": row["date"], "time": row["time"], "endTime": row["end_time"], "bookingRef": row["booking_ref"]}
            token = booking_manage_token(setting["app_secret"], row["booking_ref"])
            payload = {"task": {"title": row["title"], "location": row["location"]}, "booking": booking, "manageUrl": f"{PUBLIC_BASE_URL}/manage/{row['booking_ref']}?token={token}", "calendarUrl": f"{PUBLIC_BASE_URL}/api/public/bookings/{row['booking_ref']}.ics?token={token}"}
            enqueue_notification(conn, f"booking:{row['id']}:reminder:{row['date']}:{row['time']}", row["id"], row["task_id"], row["email"], "booking_reminder", payload)
            count += 1
    return count
