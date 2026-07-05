"""Append-only audit and booking event helpers."""

import json

from .database import now_text


def record_audit(
    conn,
    action,
    entity_type,
    entity_id,
    *,
    actor_user_id=None,
    actor_label="system",
    details=None,
    ip_address="",
    user_agent="",
):
    conn.execute(
        """INSERT INTO audit_events
        (actor_user_id,actor_label,action,entity_type,entity_id,details_json,ip_address,user_agent,created_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            actor_user_id,
            actor_label,
            action,
            entity_type,
            str(entity_id),
            json.dumps(details or {}, ensure_ascii=False),
            ip_address,
            user_agent,
            now_text(),
        ),
    )


def record_booking_event(
    conn,
    booking_id,
    task_id,
    event_type,
    *,
    actor_type,
    actor_id="",
    request_key=None,
    details=None,
):
    cursor = conn.execute(
        """INSERT OR IGNORE INTO booking_events
        (booking_id,task_id,event_type,actor_type,actor_id,request_key,details_json,created_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (
            booking_id,
            task_id,
            event_type,
            actor_type,
            str(actor_id or ""),
            request_key,
            json.dumps(details or {}, ensure_ascii=False),
            now_text(),
        ),
    )
    return bool(cursor.rowcount)


def booking_timeline(conn, booking_id):
    return [
        {
            "id": row["id"],
            "eventType": row["event_type"],
            "actorType": row["actor_type"],
            "actorId": row["actor_id"],
            "details": json.loads(row["details_json"] or "{}"),
            "createdAt": row["created_at"],
        }
        for row in conn.execute(
            "SELECT * FROM booking_events WHERE booking_id=? ORDER BY id",
            (booking_id,),
        )
    ]
