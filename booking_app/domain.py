"""Task validation, scheduling, and booking domain rules."""

import json
import re
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import EMAIL_SESSION_TTL_HOURS
from .database import now_text, task_config_snapshot
from .utils import add_minutes, normalize_email, parse_date, time_minutes

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
    result["date_overrides"] = {
        row["date"]: {
            "closed": bool(row["is_closed"]),
            "periods": json.loads(row["periods_json"] or "[]"),
        }
        for row in conn.execute(
            "SELECT date,is_closed,periods_json FROM task_date_overrides WHERE task_id=? ORDER BY date",
            (task["id"],),
        )
    }
    return result


def task_from_snapshot(base_task, snapshot):
    task = dict(base_task)
    task.update(snapshot)
    task["periods"] = [
        {
            "id": period.get("id", index),
            "label": period["label"],
            "start_time": period.get("start_time", period.get("start", "")),
            "end_time": period.get("end_time", period.get("end", "")),
            "sort_order": period.get("sort_order", index),
        }
        for index, period in enumerate(snapshot.get("periods", []))
    ]
    task["fields"] = [
        {
            "id": field.get("id", index),
            "field_key": field.get("field_key", field.get("key", f"field_{index}")),
            "label": field["label"],
            "field_type": field.get("field_type", field.get("type", "text")),
            "options_json": field.get("options_json", json.dumps(field.get("options", []), ensure_ascii=False)),
            "required": int(bool(field.get("required"))),
            "sort_order": field.get("sort_order", index),
        }
        for index, field in enumerate(snapshot.get("fields", []))
    ]
    task["date_overrides"] = snapshot.get("date_overrides", {})
    return task


def load_public_task(conn, public_id):
    row = conn.execute("SELECT * FROM tasks WHERE public_id=? COLLATE NOCASE", (public_id,)).fetchone()
    if not row:
        return None
    base = dict(row)
    version = int(base.get("published_version") or 0)
    if not version:
        return load_task(conn, task_id=base["id"])
    stored = conn.execute(
        "SELECT config_json FROM task_versions WHERE task_id=? AND version=?",
        (base["id"], version),
    ).fetchone()
    if not stored:
        return load_task(conn, task_id=base["id"])
    return task_from_snapshot(base, json.loads(stored["config_json"]))


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
        "minAdvanceMinutes": int(task.get("min_advance_minutes", 0)),
        "maxAdvanceDays": int(task.get("max_advance_days", 365)),
        "cancelCutoffMinutes": int(task.get("cancel_cutoff_minutes", 0)),
        "rescheduleCutoffMinutes": int(task.get("reschedule_cutoff_minutes", 0)),
        "maxReschedules": int(task.get("max_reschedules", 10)),
        "policyText": task.get("policy_text", ""),
        "privacyText": task.get("privacy_text", ""),
        "retentionDays": int(task.get("retention_days", 365)),
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
        "dateOverrides": [
            {
                "date": date_value,
                "closed": bool(rule.get("closed")),
                "periods": [
                    {
                        "label": period.get("label", f"时段 {index + 1}"),
                        "start": period.get("start", period.get("start_time", "")),
                        "end": period.get("end", period.get("end_time", "")),
                    }
                    for index, period in enumerate(rule.get("periods", []))
                ],
            }
            for date_value, rule in sorted(task.get("date_overrides", {}).items())
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
                "publishedVersion": int(task.get("published_version", 0)),
                "hasUnpublishedChanges": bool(task.get("has_unpublished_changes", 0)),
            }
        )
    return result


def generate_slots(task):
    slots = []
    duration = int(task["slot_minutes"])
    for date_value in task["dates"]:
        override = task.get("date_overrides", {}).get(date_value)
        if override and override.get("closed"):
            continue
        periods = override.get("periods", []) if override else task["periods"]
        for index, period in enumerate(periods):
            start = time_minutes(period.get("start_time", period.get("start", "")))
            end = time_minutes(period.get("end_time", period.get("end", "")))
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
                        "periodId": period.get("id", f"{date_value}:{index}"),
                        "periodLabel": period["label"],
                    }
                )
                current += duration
    return slots


def validate_period_list(raw_periods, duration, scope=""):
    periods = []
    for index, raw in enumerate(raw_periods):
        label = str(raw.get("label", "")).strip() or f"时段 {index + 1}"
        start = str(raw.get("start", raw.get("start_time", ""))).strip()
        end = str(raw.get("end", raw.get("end_time", ""))).strip()
        start_value = time_minutes(start)
        end_value = time_minutes(end)
        prefix = f"{scope}的" if scope else ""
        if start_value is None or end_value is None or end_value <= start_value:
            return None, f"{prefix}{label}结束时间必须晚于开始时间"
        if (end_value - start_value) % duration:
            return None, f"{prefix}{label}长度必须能被 {duration} 分钟整除"
        periods.append(
            {"label": label, "start": start, "end": end, "startValue": start_value, "endValue": end_value}
        )
    periods.sort(key=lambda item: item["startValue"])
    for previous, current in zip(periods, periods[1:]):
        if current["startValue"] < previous["endValue"]:
            return None, f"{scope + '的' if scope else ''}{current['label']}与{previous['label']}存在重叠"
    return periods, None


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
    try:
        min_advance = int(payload.get("minAdvanceMinutes", 0))
        max_advance = int(payload.get("maxAdvanceDays", 365))
        cancel_cutoff = int(payload.get("cancelCutoffMinutes", 0))
        reschedule_cutoff = int(payload.get("rescheduleCutoffMinutes", 0))
        max_reschedules = int(payload.get("maxReschedules", 10))
        retention_days = int(payload.get("retentionDays", 365))
    except (TypeError, ValueError):
        return None, "预约时间规则必须是整数"
    if min_advance < 0 or min_advance > 10080:
        return None, "最少提前时间必须在 0–10080 分钟之间"
    if max_advance < 1 or max_advance > 730:
        return None, "最远可预约天数必须在 1–730 天之间"
    if cancel_cutoff < 0 or reschedule_cutoff < 0 or max(cancel_cutoff, reschedule_cutoff) > 10080:
        return None, "取消和改期截止时间必须在 0–10080 分钟之间"
    if max_reschedules < 0 or max_reschedules > 100:
        return None, "最多改期次数必须在 0–100 次之间"
    if retention_days < 30 or retention_days > 3650:
        return None, "数据保留天数必须在 30–3650 天之间"

    raw_dates = payload.get("dates", [])
    dates = sorted({str(value) for value in raw_dates if parse_date(value)})
    if not dates:
        return None, "请至少选择一个可预约日期"
    if len(dates) > 366:
        return None, "一个任务最多设置 366 个日期"

    periods, period_error = validate_period_list(payload.get("periods", []), duration)
    if period_error:
        return None, period_error
    if not periods:
        return None, "请至少添加一个开放时段"

    overrides = []
    seen_override_dates = set()
    for raw in payload.get("dateOverrides", []):
        date_value = str(raw.get("date", ""))
        if date_value not in dates:
            return None, "日期例外必须属于任务的可预约日期"
        if date_value in seen_override_dates:
            return None, f"日期 {date_value} 的例外规则重复"
        seen_override_dates.add(date_value)
        closed = bool(raw.get("closed"))
        override_periods = []
        if not closed:
            override_periods, override_error = validate_period_list(
                raw.get("periods", []), duration, date_value
            )
            if override_error:
                return None, override_error
            if not override_periods:
                return None, f"日期 {date_value} 的例外时段不能为空"
        overrides.append({"date": date_value, "closed": closed, "periods": override_periods})

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
        "minAdvanceMinutes": min_advance,
        "maxAdvanceDays": max_advance,
        "cancelCutoffMinutes": cancel_cutoff,
        "rescheduleCutoffMinutes": reschedule_cutoff,
        "maxReschedules": max_reschedules,
        "policyText": str(payload.get("policyText", "")).strip()[:4000],
        "privacyText": str(payload.get("privacyText", "")).strip()[:4000],
        "retentionDays": retention_days,
        "dates": dates,
        "periods": periods,
        "dateOverrides": overrides,
        "fields": fields,
    }, None


def save_task_config(conn, task_id, data):
    timestamp = now_text()
    conn.execute(
        """
        UPDATE tasks SET name=?, title=?, description=?, location=?, contact=?, timezone=?,
            slot_minutes=?, opening_strategy=?, min_advance_minutes=?, max_advance_days=?,
            cancel_cutoff_minutes=?,reschedule_cutoff_minutes=?,max_reschedules=?,policy_text=?,privacy_text=?,retention_days=?,revision=revision+1,
            has_unpublished_changes=CASE WHEN published_version>0 THEN 1 ELSE has_unpublished_changes END,
            updated_at=? WHERE id=?
        """,
        (
            data["name"], data["title"], data["description"], data["location"], data["contact"],
            data["timezone"], data["slotMinutes"], data["openingStrategy"], data.get("minAdvanceMinutes", 0),
            data.get("maxAdvanceDays", 365), data.get("cancelCutoffMinutes", 0), data.get("rescheduleCutoffMinutes", 0),
            data.get("maxReschedules", 10), data.get("policyText", ""), data.get("privacyText", ""),
            data.get("retentionDays", 365), timestamp, task_id,
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
    conn.execute("DELETE FROM task_date_overrides WHERE task_id=?", (task_id,))
    conn.executemany(
        "INSERT INTO task_date_overrides (task_id,date,is_closed,periods_json) VALUES (?,?,?,?)",
        [
            (
                task_id,
                item["date"],
                int(item["closed"]),
                json.dumps(
                    [
                        {"label": period["label"], "start": period["start"], "end": period["end"]}
                        for period in item["periods"]
                    ],
                    ensure_ascii=False,
                ),
            )
            for item in data.get("dateOverrides", [])
        ],
    )


def publish_task_version(conn, task_id):
    snapshot = task_config_snapshot(conn, task_id)
    if snapshot is None:
        return None
    row = conn.execute(
        "SELECT COALESCE(MAX(version),0) AS value FROM task_versions WHERE task_id=?",
        (task_id,),
    ).fetchone()
    version = int(row["value"]) + 1
    published_at = now_text()
    conn.execute(
        "INSERT INTO task_versions (task_id,version,config_json,published_at) VALUES (?,?,?,?)",
        (task_id, version, json.dumps(snapshot, ensure_ascii=False), published_at),
    )
    conn.execute(
        "UPDATE tasks SET published_version=?,has_unpublished_changes=0,updated_at=? WHERE id=?",
        (version, published_at, task_id),
    )
    return version


def config_conflicts(conn, task_id, data):
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
    allowed = {(slot["date"], slot["time"], slot["endTime"]) for slot in generate_slots(pseudo_task)}
    conflicts = []
    for row in conn.execute(
        "SELECT id, name, email, date, time, end_time FROM bookings WHERE task_id=? AND status='confirmed' ORDER BY date, time", (task_id,)
    ):
        if (row["date"], row["time"], row["end_time"]) not in allowed:
            conflicts.append({"id": row["id"], "name": row["name"], "email": row["email"], "date": row["date"], "time": row["time"]})
    return conflicts


def task_booking_conflicts(conn, task):
    allowed = {(slot["date"], slot["time"], slot["endTime"]) for slot in generate_slots(task)}
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "date": row["date"],
            "time": row["time"],
        }
        for row in conn.execute(
            "SELECT id,name,email,date,time,end_time FROM bookings WHERE task_id=? AND status='confirmed' ORDER BY date,time",
            (task["id"],),
        )
        if (row["date"], row["time"], row["end_time"]) not in allowed
    ]


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
        earliest = local_now + timedelta(minutes=int(task.get("min_advance_minutes", 0)))
        latest_date = local_now.date() + timedelta(days=int(task.get("max_advance_days", 365)))
        too_soon = slot_start <= earliest
        too_far = parse_date(slot["date"]) > latest_date
        availability_reason = ""
        if booking:
            status = "booked"
        elif block:
            status = "blocked"
        elif task["status"] != "published":
            status = "closed"
            availability_reason = "task_unavailable"
        elif too_soon:
            status = "closed"
            availability_reason = "too_soon"
        elif too_far:
            status = "closed"
            availability_reason = "too_far"
        elif task["opening_strategy"] == "sequential" and not chain_open:
            status = "locked"
        else:
            status = "available"
        if booking or block:
            chain_open_by_period[period_key] = chain_open
        elif too_soon or too_far or task["status"] != "published":
            chain_open_by_period[period_key] = chain_open
        else:
            chain_open_by_period[period_key] = False
        result.append(
            {
                **slot,
                "status": status,
                "availabilityReason": availability_reason,
                "booking": booking,
                "blockedRow": block,
            }
        )
    return result


def format_booking(row):
    answers = json.loads(row["answers_json"] or "{}")
    keys = set(row.keys())
    return {
        "id": row["id"], "name": row["name"], "email": row["email"], "date": row["date"],
        "time": row["time"], "endTime": row["end_time"], "answers": answers,
        "status": row["status"], "cancelledAt": row["cancelled_at"],
        "cancellationReason": row["cancellation_reason"], "createdAt": row["created_at"],
        "bookingRef": row["booking_ref"] if "booking_ref" in keys else "",
        "internalNotes": row["internal_notes"] if "internal_notes" in keys else "",
        "rescheduleCount": int(row["reschedule_count"]) if "reschedule_count" in keys else 0,
    }


def booking_reschedule_history(conn, booking_id):
    return [
        {
            "id": row["id"],
            "oldDate": row["old_date"],
            "oldTime": row["old_time"],
            "oldEndTime": row["old_end_time"],
            "newDate": row["new_date"],
            "newTime": row["new_time"],
            "newEndTime": row["new_end_time"],
            "actor": row["actor"],
            "changedAt": row["changed_at"],
        }
        for row in conn.execute(
            "SELECT * FROM booking_reschedules WHERE booking_id=? ORDER BY id",
            (booking_id,),
        )
    ]


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
