"""Pluggable Telegram slash-command router for interactive bot features."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandContext:
    """Parsed context passed to a Telegram command handler."""

    chat_id: str
    command: str
    args: str
    raw_text: str


CommandHandler = Callable[[CommandContext], str]


@dataclass(frozen=True)
class CommandSpec:
    """Metadata and handler for a registered Telegram bot command."""

    command: str
    description: str
    handler: CommandHandler
    usage: str | None = None


class CommandRouter:
    """Routes incoming Telegram update payloads to registered slash-command handlers."""

    def __init__(self) -> None:
        self._commands: dict[str, CommandSpec] = {}
        self.register(
            command="start",
            description="Bắt đầu và xem hướng dẫn sử dụng bot",
            handler=self._handle_help,
        )
        self.register(
            command="help",
            description="Xem danh sách lệnh và hướng dẫn",
            handler=self._handle_help,
        )

    def register(
        self,
        command: str,
        description: str,
        handler: CommandHandler,
        usage: str | None = None,
    ) -> None:
        """Register a slash command (Telegram valid names use lowercase letters, digits, '_')."""
        normalized = command.lstrip("/").strip().lower().replace("-", "_")
        self._commands[normalized] = CommandSpec(
            command=normalized,
            description=description,
            handler=handler,
            usage=usage,
        )

    def get_telegram_commands(self) -> list[dict[str, str]]:
        """Build payload compatible with Telegram Bot API ``setMyCommands``."""
        return [
            {"command": spec.command, "description": spec.description}
            for spec in self._commands.values()
        ]

    @staticmethod
    def parse_command(text: str) -> tuple[str, str] | None:
        """Extract ``(normalized_command, args)`` from a slash message, or None if not a command."""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None

        parts = stripped.split(maxsplit=1)
        raw_cmd = parts[0][1:]
        if not raw_cmd:
            return None

        # Strip optional @BotUsername suffix and normalize hyphens to underscores
        cmd = raw_cmd.split("@", maxsplit=1)[0].strip().lower().replace("-", "_")
        args = parts[1].strip() if len(parts) > 1 else ""
        return (cmd, args) if cmd else None

    def handle_update(
        self,
        update: dict[str, Any],
        allowed_chat_id: str | None = None,
    ) -> tuple[str, str] | None:
        """Process a Telegram update dict and return ``(chat_id, reply_text)`` if a reply is needed."""
        message = update.get("message")
        if not isinstance(message, dict):
            return None

        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(chat, dict) or not isinstance(text, str):
            return None

        chat_id = str(chat.get("id", "")).strip()
        if not chat_id:
            return None

        if allowed_chat_id and chat_id != str(allowed_chat_id).strip():
            logger.warning("Bỏ qua tin nhắn từ chat_id không được phép: %s", chat_id)
            return None

        parsed = self.parse_command(text)
        if parsed is None:
            return None

        cmd, args = parsed
        spec = self._commands.get(cmd)
        if spec is None:
            safe_cmd = html.escape(cmd)
            return (
                chat_id,
                f"❓ Lệnh <code>/{safe_cmd}</code> không tồn tại.\n"
                f"Gõ /help để xem danh sách lệnh hỗ trợ.",
            )

        ctx = CommandContext(
            chat_id=chat_id,
            command=cmd,
            args=args,
            raw_text=text,
        )
        try:
            reply = spec.handler(ctx)
        except Exception:
            logger.exception("Lỗi khi xử lý lệnh /%s", cmd)
            safe_cmd = html.escape(cmd)
            reply = f"⚠️ Có lỗi xảy ra khi xử lý lệnh <code>/{safe_cmd}</code>. Vui lòng thử lại sau."

        return (chat_id, reply)

    def _handle_help(self, _ctx: CommandContext) -> str:
        lines = [
            "🤖 <b>HƯỚNG DẪN SỬ DỤNG BOT</b>",
            "",
            "Danh sách lệnh hiện có:",
        ]
        for spec in self._commands.values():
            label = html.escape(spec.usage or f"/{spec.command}")
            description = html.escape(spec.description)
            lines.append(f"• <code>{label}</code> — {description}")
        return "\n".join(lines)
