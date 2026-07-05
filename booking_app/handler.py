"""HTTP transport and API route handlers."""

import csv
import hmac
import io
import json
import mimetypes
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import (
    ADMIN_LOGIN_FAILURES, ADMIN_PASSWORD, ADMIN_PASSWORD_IS_DEFAULT,
    ADMIN_SESSION_COOKIE, DEV_VERIFICATION_CODE,
    EMAIL_RE, MAX_VERIFICATION_ATTEMPTS, ROOT, STATUS_TRANSITIONS,
    VERIFICATION_TTL_MINUTES,
)
from .auth import has_role, load_session
from .database import connect, generate_public_id, now_text
from .domain import (
    config_conflicts, create_email_session, email_session_is_valid,
    format_booking, generate_slots, load_task, parse_datetime,
    save_task_config, slot_map_for_task, task_payload, validate_task_input,
)
from .utils import normalize_email

from .admin_routes import AdminRoutesMixin
from .public_routes import PublicRoutesMixin


class RequestBodyTooLarge(Exception):
    pass


class BookingHandler(AdminRoutesMixin, PublicRoutesMixin, SimpleHTTPRequestHandler):
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except RequestBodyTooLarge:
            self.close_connection = True
            self.send_json({"error": "请求内容超过 1 MB 限制"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

    def __init__(self, *args, **kwargs):
        self.request_id = secrets.token_hex(8)
        self._session_loaded = False
        self._session = None
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        self.send_header("X-Request-ID", self.request_id)
        super().end_headers()

    def log_message(self, fmt, *args):
        print(json.dumps({"time": self.log_date_time_string(), "requestId": self.request_id, "message": fmt % args}, ensure_ascii=False))

    def send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 1_048_576:
            raise RequestBodyTooLarge()
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def serve_html(self, filename):
        body = (ROOT / filename).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_static(self, request_path):
        decoded = unquote(request_path)
        relative = decoded.removeprefix("/static/")
        static_root = (ROOT / "static").resolve()
        candidate = (static_root / relative).resolve()
        try:
            candidate.relative_to(static_root)
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not relative or not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location):
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def session_cookie(self):
        raw = self.headers.get("Cookie", "")
        cookie = SimpleCookie()
        cookie.load(raw)
        value = cookie.get(ADMIN_SESSION_COOKIE)
        return value.value if value else ""

    def current_session(self):
        if self._session_loaded:
            return self._session
        self._session_loaded = True
        with connect() as conn:
            self._session = load_session(conn, self.session_cookie())
        return self._session

    def is_admin(self):
        return bool(self.current_session())

    def require_admin(self, role="operator"):
        session = self.current_session()
        if has_role(session, role):
            return True
        self.send_json(
            {"error": "请先登录后台" if not session else "当前账号没有执行此操作的权限"},
            HTTPStatus.UNAUTHORIZED if not session else HTTPStatus.FORBIDDEN,
        )
        return False

    def require_csrf(self):
        session = self.current_session()
        supplied = self.headers.get("X-CSRF-Token", "")
        if session and hmac.compare_digest(session["csrf_token"], supplied):
            return True
        self.send_json({"error": "安全校验失败，请刷新页面后重试"}, HTTPStatus.FORBIDDEN)
        return False

    def path_parts(self):
        return [part for part in urlparse(self.path).path.split("/") if part]

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        parts = self.path_parts()
        if path.startswith("/static/"):
            self.serve_static(path)
            return
        if path == "/healthz":
            try:
                with connect() as conn:
                    check = conn.execute("PRAGMA quick_check").fetchone()[0]
                    migration = conn.execute("SELECT COALESCE(MAX(version),0) version FROM schema_migrations").fetchone()["version"]
                self.send_json({"status": "ok" if check == "ok" else "unhealthy", "database": check, "schemaVersion": migration}, HTTPStatus.OK if check == "ok" else HTTPStatus.SERVICE_UNAVAILABLE)
            except Exception:
                self.send_json({"status": "unhealthy"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if path in ("/admin", "/admin.html") or path.startswith("/admin/"):
            self.serve_html("admin.html")
            return
        if path.startswith("/b/") and len(self.path_parts()) == 2:
            self.serve_html("index.html")
            return
        if path.startswith("/manage/") and len(self.path_parts()) == 2:
            self.serve_html("index.html")
            return
        if path in ("/", "/index.html"):
            with connect() as conn:
                task = conn.execute("SELECT public_id FROM tasks WHERE is_default=1 ORDER BY id LIMIT 1").fetchone()
                if not task:
                    task = conn.execute("SELECT public_id FROM tasks WHERE status='published' ORDER BY id LIMIT 1").fetchone()
            self.redirect(f"/b/{task['public_id']}" if task else "/admin")
            return
        if path == "/api/admin/session":
            session = self.current_session()
            self.send_json({
                "authenticated": bool(session),
                "defaultPassword": ADMIN_PASSWORD_IS_DEFAULT,
                "csrfToken": session["csrf_token"] if session else "",
                "user": {"id": session["user_id"], "username": session["username"], "role": session["role"]} if session else None,
            })
            return
        if path == "/api/admin/tasks":
            if self.require_admin():
                self.admin_list_tasks()
            return
        if path == "/api/admin/settings/email":
            if self.require_admin("owner"): self.admin_email_settings()
            return
        if path == "/api/admin/notifications":
            if self.require_admin(): self.admin_notifications(parse_qs(parsed.query))
            return
        if path == "/api/admin/audit":
            if self.require_admin("owner"): self.admin_audit()
            return
        if path == "/api/admin/agenda":
            if self.require_admin(): self.admin_agenda(parse_qs(parsed.query))
            return
        if path == "/api/admin/users":
            if self.require_admin("owner"): self.admin_users()
            return
        if len(parts) == 6 and parts[:3] == ["api", "admin", "tasks"] and parts[4] == "bookings" and parts[5].isdigit():
            if self.require_admin(): self.admin_booking_timeline(int(parts[5])); return
        if len(parts) == 4 and parts[:3] == ["api", "public", "bookings"]:
            token = parse_qs(parsed.query).get("token", [""])[0]
            if parts[3].endswith(".ics"):
                self.public_booking_ics(parts[3][:-4], token)
            else:
                self.public_manage_get(parts[3], token)
            return
        if len(parts) >= 4 and parts[:3] == ["api", "public", "tasks"]:
            self.public_get_task(parts[3])
            return
        if len(parts) >= 4 and parts[:3] == ["api", "admin", "tasks"]:
            if not self.require_admin():
                return
            self.admin_get_route(parts, parse_qs(parsed.query))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path
        parts = self.path_parts()
        if path == "/api/admin/login":
            self.admin_login()
            return
        if path == "/api/admin/logout":
            if not self.require_admin() or not self.require_csrf():
                return
            self.admin_logout()
            return
        if path == "/api/verification/send":
            self.verification_send()
            return
        if path == "/api/verification/verify":
            self.verification_verify()
            return
        if path == "/api/admin/tasks":
            if self.require_admin() and self.require_csrf():
                self.admin_create_task()
            return
        if path == "/api/admin/settings/email/test":
            if self.require_admin("owner") and self.require_csrf(): self.admin_test_email()
            return
        if path == "/api/admin/users":
            if self.require_admin("owner") and self.require_csrf(): self.admin_create_user()
            return
        if path == "/api/admin/notifications/retry":
            if self.require_admin() and self.require_csrf(): self.admin_retry_notifications()
            return
        if len(parts) == 5 and parts[:3] == ["api", "admin", "notifications"] and parts[4].isdigit():
            if self.require_admin() and self.require_csrf(): self.admin_retry_notifications(int(parts[4]))
            return
        if len(parts) >= 5 and parts[:3] == ["api", "public", "tasks"] and parts[4] == "bookings":
            if len(parts) == 6 and parts[5] == "lookup":
                self.public_lookup_booking(parts[3])
            else:
                self.public_create_booking(parts[3])
            return
        if len(parts) >= 5 and parts[:3] == ["api", "admin", "tasks"]:
            if not self.require_admin() or not self.require_csrf():
                return
            self.admin_post_route(parts)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PUT(self):
        path = urlparse(self.path).path
        if path == "/api/admin/settings/email":
            if self.require_admin("owner") and self.require_csrf(): self.admin_save_email_settings()
            return
        parts = self.path_parts()
        if len(parts) == 4 and parts[:3] == ["api", "admin", "users"] and parts[3].isdigit():
            if self.require_admin("owner") and self.require_csrf(): self.admin_update_user(int(parts[3]))
            return
        if len(parts) == 6 and parts[:3] == ["api", "admin", "tasks"] and parts[4] == "bookings" and parts[5].isdigit():
            if self.require_admin() and self.require_csrf(): self.admin_update_booking(int(parts[3]), int(parts[5]))
            return
        if len(parts) == 6 and parts[:3] == ["api", "public", "tasks"] and parts[4:] == ["bookings", "by-email"]:
            self.public_reschedule_booking(parts[3])
            return
        if len(parts) == 4 and parts[:3] == ["api", "admin", "tasks"]:
            if self.require_admin() and self.require_csrf():
                self.admin_update_task(parts[3])
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self):
        parts = self.path_parts()
        if len(parts) == 6 and parts[:3] == ["api", "public", "tasks"] and parts[4:] == ["bookings", "by-email"]:
            self.public_cancel_booking(parts[3])
            return
        if len(parts) >= 5 and parts[:3] == ["api", "admin", "tasks"]:
            if not self.require_admin() or not self.require_csrf():
                return
            self.admin_delete_route(parts)
            return
        self.send_error(HTTPStatus.NOT_FOUND)
