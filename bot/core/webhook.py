"""Framework-agnostic Telegram webhook request processor."""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any, Mapping

from bot.config import Settings
from bot.core.commands import CommandRouter
from bot.core.github_vars import GitHubVariablesClient
from bot.core.state import StateStore
from bot.core.telegram_client import TelegramClient
from bot.features import load_features

logger = logging.getLogger(__name__)


def build_github_vars_client(settings: Settings) -> GitHubVariablesClient | None:
    """Create a GitHubVariablesClient if GITHUB_TOKEN and GITHUB_REPOSITORY are configured."""
    if settings.github_token and settings.github_repository:
        return GitHubVariablesClient(
            token=settings.github_token,
            repository=settings.github_repository,
        )
    return None


def build_command_router(
    settings: Settings,
    telegram: TelegramClient,
    state_store: StateStore | None = None,
    github_vars: GitHubVariablesClient | None = None,
) -> CommandRouter:
    """Instantiate CommandRouter and register commands from all features."""
    router = CommandRouter()
    features = load_features(
        settings=settings,
        telegram=telegram,
        state_store=state_store,
        github_vars=github_vars,
        include_disabled=True,
    )
    for feature in features:
        feature.register_commands(router)
    return router


def _get_header_ci(headers: Mapping[str, str], target: str) -> str:
    target_lower = target.lower()
    for key, value in headers.items():
        if key.lower() == target_lower:
            return value
    return ""


def process_webhook_request(
    body: bytes,
    headers: Mapping[str, str],
    *,
    settings: Settings | None = None,
    telegram: TelegramClient | None = None,
    router: CommandRouter | None = None,
) -> tuple[int, dict[str, Any]]:
    """Validate and process an incoming Telegram Webhook HTTP POST payload.

    Returns ``(http_status_code, json_response_body)``.
    """
    active_settings = settings or Settings.load()

    expected_secret = active_settings.telegram_webhook_secret
    if expected_secret:
        received_secret = _get_header_ci(headers, "X-Telegram-Bot-Api-Secret-Token")
        if not hmac.compare_digest(received_secret, expected_secret):
            logger.warning("Từ chối request webhook do sai secret token.")
            return 403, {"ok": False, "error": "Forbidden"}

    try:
        update = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"ok": False, "error": "Invalid JSON payload"}

    if not isinstance(update, dict):
        return 400, {"ok": False, "error": "Payload must be a JSON object"}

    active_telegram = telegram or TelegramClient(
        token=active_settings.telegram_token,
        default_chat_id=active_settings.telegram_chat_id,
    )
    active_router = router or build_command_router(
        settings=active_settings,
        telegram=active_telegram,
        state_store=None,  # Serverless filesystem (Vercel) may be read-only; sync via GitHub Variables
        github_vars=build_github_vars_client(active_settings),
    )

    result = active_router.handle_update(
        update,
        allowed_chat_id=active_settings.telegram_chat_id,
    )
    if result is not None:
        chat_id, reply_text = result
        active_telegram.send_message(
            reply_text,
            chat_id=chat_id,
            disable_web_page_preview=True,
        )

    return 200, {"ok": True}
