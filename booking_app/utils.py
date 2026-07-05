"""Small parsing and normalization helpers."""

from datetime import datetime

from .config import TIME_RE

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
