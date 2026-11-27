"""Vercel Python Serverless Function entrypoint for HTTP Cron triggers."""

from __future__ import annotations

import json
import logging
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

ROOT_DIR = str(Path(__file__).resolve().parent.parent)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from bot.core.logger import setup_logging
from bot.core.webhook import process_cron_request

setup_logging()
logger = logging.getLogger(__name__)


class handler(BaseHTTPRequestHandler):
    """Handles GET/POST /api/cron requests on Vercel."""

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        query_string = urlsplit(self.path).query
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
