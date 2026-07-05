"""Process entry point and backwards-compatible public API."""

import os
from http.server import ThreadingHTTPServer

from .config import *  # noqa: F401,F403
from .database import *  # noqa: F401,F403
from .domain import *  # noqa: F401,F403
from .handler import BookingHandler
from .worker import start_worker


def main():
    if APP_ENV == "production" and ADMIN_PASSWORD_IS_DEFAULT:
        raise RuntimeError("生产环境禁止使用默认后台密码，请设置 ADMIN_PASSWORD")
    init_db()
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), BookingHandler)
    stop_event, worker = start_worker()
    print(f"Booking server running at http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    finally:
        stop_event.set()
        worker.join(timeout=2)
        server.server_close()
