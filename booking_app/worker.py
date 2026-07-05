"""Small background worker for reminders, retries, and session cleanup."""

import threading

from .auth import cleanup_sessions
from .database import connect
from .notifications import enqueue_due_reminders, retry_due
from .database import now_text


def purge_expired_personal_data(conn):
    rows = conn.execute("""SELECT b.id,b.task_id FROM bookings b JOIN tasks t ON t.id=b.task_id
        WHERE b.email NOT LIKE 'deleted+%@invalid.local'
        AND date(b.date, '+' || t.retention_days || ' days') < date('now','localtime')""").fetchall()
    for row in rows:
        conn.execute("UPDATE bookings SET name='已按保留策略清理',email=?,answers_json='{}',internal_notes='' WHERE id=?", (f"deleted+{row['id']}@invalid.local", row["id"]))
        conn.execute("""INSERT INTO booking_events(booking_id,task_id,event_type,actor_type,details_json,created_at)
            VALUES (?,?,'personal_data_purged','system','{}',?)""", (row["id"], row["task_id"], now_text()))
    return len(rows)


def run_worker(stop_event):
    while not stop_event.wait(30):
        try:
            enqueue_due_reminders()
            retry_due()
            with connect() as conn:
                cleanup_sessions(conn)
                conn.execute("DELETE FROM email_sessions WHERE expires_at<datetime('now','localtime')")
                conn.execute("DELETE FROM email_verifications WHERE expires_at<datetime('now','localtime','-1 day')")
                purge_expired_personal_data(conn)
        except Exception as exc:
            print(f"background worker error: {exc}")


def start_worker():
    stop_event = threading.Event()
    thread = threading.Thread(target=run_worker, args=(stop_event,), daemon=True, name="booking-worker")
    thread.start()
    return stop_event, thread
