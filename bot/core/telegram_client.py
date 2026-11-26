"""Reusable Telegram Bot API client."""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class TelegramClient:
    """Thin wrapper around the Telegram Bot HTTP API."""

    BASE_URL = "https://api.telegram.org"

    def __init__(self, token: str, default_chat_id: str, timeout: int = 10) -> None:
        self._token = token
        self._default_chat_id = default_chat_id
        self._timeout = timeout
        self._session = requests.Session()

    def _endpoint(self, method: str) -> str:
        return f"{self.BASE_URL}/bot{self._token}/{method}"

    def send_message(
        self,
        text: str,
        chat_id: str | None = None,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = False,
    ) -> bool:
        """Send a text message to a Telegram chat."""
        payload: dict[str, Any] = {
            "chat_id": chat_id or self._default_chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        try:
            response = self._session.post(
                self._endpoint("sendMessage"),
                data=payload,
                timeout=self._timeout,
            )
            if response.status_code == 200:
                logger.info("Đã gửi thông báo Telegram thành công.")
                return True
            logger.error("Lỗi gửi Telegram (%s): %s", response.status_code, response.text)
            return False
        except requests.RequestException as exc:
            logger.error("Lỗi kết nối Telegram: %s", exc)
            return False

    def get_updates(self, offset: int | None = None, timeout: int = 10) -> list[dict[str, Any]]:
        """Fetch incoming bot updates (useful when adding interactive /commands later)."""
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            response = self._session.get(
                self._endpoint("getUpdates"),
                params=params,
                timeout=timeout + 5,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("result", []) if data.get("ok") else []
        except requests.RequestException as exc:
            logger.error("Lỗi lấy cập nhật Telegram: %s", exc)
            return []

    def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
        """Register the bot's slash-command menu in Telegram clients."""
        try:
            response = self._session.post(
                self._endpoint("setMyCommands"),
                json={"commands": commands},
                timeout=self._timeout,
            )
            if response.status_code == 200 and response.json().get("ok"):
                logger.info("Đã đồng bộ %d lệnh Telegram thành công.", len(commands))
                return True
            logger.error("Lỗi setMyCommands (%s): %s", response.status_code, response.text)
            return False
        except requests.RequestException as exc:
            logger.error("Lỗi kết nối Telegram khi gọi setMyCommands: %s", exc)
            return False

    def set_webhook(self, url: str, secret_token: str | None = None) -> bool:
        """Configure the Telegram Bot webhook URL (with optional secret token header)."""
        payload: dict[str, Any] = {"url": url}
        if secret_token:
            payload["secret_token"] = secret_token
        try:
            response = self._session.post(
                self._endpoint("setWebhook"),
                json=payload,
                timeout=self._timeout,
            )
            if response.status_code == 200 and response.json().get("ok"):
                logger.info("Đã cài đặt Telegram Webhook: %s", url)
                return True
            logger.error("Lỗi setWebhook (%s): %s", response.status_code, response.text)
            return False
        except requests.RequestException as exc:
            logger.error("Lỗi kết nối Telegram khi gọi setWebhook: %s", exc)
            return False

    def delete_webhook(self) -> bool:
        """Remove the currently configured Telegram Bot webhook."""
        try:
            response = self._session.post(
                self._endpoint("deleteWebhook"),
                timeout=self._timeout,
            )
            if response.status_code == 200 and response.json().get("ok"):
                logger.info("Đã xoá Telegram Webhook.")
                return True
            logger.error("Lỗi deleteWebhook (%s): %s", response.status_code, response.text)
            return False
        except requests.RequestException as exc:
            logger.error("Lỗi kết nối Telegram khi gọi deleteWebhook: %s", exc)
            return False
