"""Standalone HTTP webhook & cron server for Render, Docker, or local testing."""

from __future__ import annotations

import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from bot.config import Settings
from bot.core.logger import setup_logging
from bot.core.webhook import process_cron_request, process_webhook_request

logger = logging.getLogger(__name__)

WEBHOOK_PATHS = {"/", "/webhook", "/api/webhook"}
HEALTH_PATHS = {"/", "/health", "/api/webhook"}
CRON_PATHS = {"/cron", "/api/cron"}
_SECRET_QUERY_RE = re.compile(r"([?&]secret=)[^&\s]+", re.IGNORECASE)


class WebhookHTTPHandler(BaseHTTPRequestHandler):
    """Serves health checks, Telegram webhook POST requests, and HTTP cron triggers."""

    def log_message(self, format: str, *args: object) -> None:
        message = format % args
        sanitized = _SECRET_QUERY_RE.sub(r"\1***", message)
        logger.info("%s - %s", self.address_string(), sanitized)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path in CRON_PATHS:
            self._handle_cron(parsed.query)
        elif parsed.path in HEALTH_PATHS:
            self._send_json(200, {"ok": True, "service": "telegram-bot-webhook"})
        else:
            self._send_json(404, {"ok": False, "error": "Not Found"})

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path in CRON_PATHS:
            self._handle_cron(parsed.query)
            return

        if parsed.path not in WEBHOOK_PATHS:
            self._send_json(404, {"ok": False, "error": "Not Found"})
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_length) if content_length > 0 else b""
        try:
            status_code, payload = process_webhook_request(body, self.headers)
        except Exception:
            logger.exception("Lỗi không mong muốn khi xử lý webhook.")
            status_code, payload = 500, {"ok": False, "error": "Internal Server Error"}
        self._send_json(status_code, payload)

    def _handle_cron(self, query_string: str) -> None:
        try:
            status_code, payload = process_cron_request(query_string, self.headers)
        except Exception:
            logger.exception("Lỗi không mong muốn khi xử lý /api/cron.")
            status_code, payload = 500, {"ok": False, "error": "Internal Server Error"}
        self._send_json(status_code, payload)

    def _send_json(self, status_code: int, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    settings = Settings.load()
    setup_logging(settings.log_level)

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), WebhookHTTPHandler)
    logger.info(
        "Webhook & Cron server đang lắng nghe tại http://%s:%d (/api/webhook, /api/cron)",
        host,
        port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Đang tắt webhook server...")
        server.server_close()


if __name__ == "__main__":
    main()
