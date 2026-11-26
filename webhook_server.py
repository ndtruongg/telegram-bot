"""Standalone HTTP webhook server for Render, Docker, or local testing."""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bot.config import Settings
from bot.core.logger import setup_logging
from bot.core.webhook import process_webhook_request

logger = logging.getLogger(__name__)

WEBHOOK_PATHS = {"/", "/webhook", "/api/webhook"}
HEALTH_PATHS = {"/", "/health", "/api/webhook"}


class WebhookHTTPHandler(BaseHTTPRequestHandler):
    """Serves health checks and Telegram webhook POST requests."""

    def do_GET(self) -> None:
        path = self.path.split("?", maxsplit=1)[0]
        if path in HEALTH_PATHS:
            self._send_json(200, {"ok": True, "service": "telegram-bot-webhook"})
        else:
            self._send_json(404, {"ok": False, "error": "Not Found"})

    def do_POST(self) -> None:
        path = self.path.split("?", maxsplit=1)[0]
        if path not in WEBHOOK_PATHS:
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
    logger.info("Webhook server đang lắng nghe tại http://%s:%d/api/webhook", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Đang tắt webhook server...")
        server.server_close()


if __name__ == "__main__":
    main()
