"""Database overrides layered on environment defaults."""

from . import config
from .database import now_text


EMAIL_FIELDS = ("host", "port", "user", "password", "from", "use_ssl", "starttls")


def effective_email_settings(conn):
    row = conn.execute("SELECT * FROM system_settings WHERE id=1").fetchone()
    overrides = dict(row) if row else {}
    defaults = {
        "host": config.SMTP_HOST,
        "port": config.SMTP_PORT,
        "user": config.SMTP_USER,
        "password": config.SMTP_PASSWORD,
        "from": config.SMTP_FROM,
        "use_ssl": config.SMTP_USE_SSL,
        "starttls": config.SMTP_STARTTLS,
    }
    result = {}
    sources = {}
    for key in EMAIL_FIELDS:
        stored = overrides.get(f"smtp_{key}")
        result[key] = defaults[key] if stored is None else stored
        sources[key] = "env" if stored is None else "admin"
    result["use_ssl"] = bool(result["use_ssl"])
    result["starttls"] = bool(result["starttls"])
    return result, sources


def public_email_settings(conn):
    values, sources = effective_email_settings(conn)
    return {
        "host": values["host"],
        "port": values["port"],
        "user": values["user"],
        "from": values["from"],
        "useSsl": values["use_ssl"],
        "starttls": values["starttls"],
        "passwordConfigured": bool(values["password"]),
        "sources": {"useSsl" if key == "use_ssl" else key: value for key, value in sources.items() if key != "password"},
        "passwordSource": sources["password"],
    }


def save_email_settings(conn, payload, user_id):
    if payload.get("useSsl") and payload.get("starttls"):
        raise ValueError("SSL 与 STARTTLS 不能同时启用")
    mapping = {
        "host": "smtp_host", "port": "smtp_port", "user": "smtp_user",
        "from": "smtp_from", "useSsl": "smtp_use_ssl", "starttls": "smtp_starttls",
    }
    updates = {}
    inherit = set(payload.get("inherit", []))
    for public, column in mapping.items():
        if public in inherit:
            updates[column] = None
        elif public in payload:
            value = payload[public]
            if public == "port":
                value = int(value or 0)
                if value < 0 or value > 65535:
                    raise ValueError("SMTP 端口无效")
            elif public in ("useSsl", "starttls"):
                value = int(bool(value))
            else:
                value = str(value).strip()
            updates[column] = value
    if "password" in inherit:
        updates["smtp_password"] = None
    elif str(payload.get("password", "")):
        updates["smtp_password"] = str(payload["password"])
    if not updates:
        return
    updates["updated_by"] = user_id
    updates["updated_at"] = now_text()
    assignments = ",".join(f"{key}=?" for key in updates)
    conn.execute(f"UPDATE system_settings SET {assignments} WHERE id=1", tuple(updates.values()))
