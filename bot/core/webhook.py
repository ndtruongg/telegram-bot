"""Framework-agnostic Telegram webhook and HTTP cron request processors."""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs

from bot.config import Settings
from bot.core.commands import CommandRouter
from bot.core.feature import BaseFeature
from bot.core.github_vars import GitHubVariablesClient
from bot.core.state import StateStore
from bot.core.telegram_client import TelegramClient
from bot.features import load_features

logger = logging.getLogger(__name__)

_CRON_LOCK = threading.Lock()
_CRON_LAST_RUN_AT: dict[str, float] = {}
_INTERVAL_GRACE_SECONDS = 5.0


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


def _extract_cron_secret(
    headers: Mapping[str, str],
    query_params: Mapping[str, list[str]],
) -> str:
    for header_name in ("X-Cron-Secret", "X-Telegram-Bot-Api-Secret-Token"):
        val = _get_header_ci(headers, header_name).strip()
        if val:
            return val

    auth_header = _get_header_ci(headers, "Authorization").strip()
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    secret_values = query_params.get("secret") or []
    return secret_values[0].strip() if secret_values else ""


def _parse_requested_features(query_params: Mapping[str, list[str]]) -> list[str]:
    selected: list[str] = []
    for raw_val in query_params.get("feature", []):
        for part in raw_val.split(","):
            name = part.strip().lower()
            if name and name != "all" and name not in selected:
                selected.append(name)
    return selected


def _is_truthy_param(query_params: Mapping[str, list[str]], key: str) -> bool:
    values = query_params.get(key) or []
    return any(v.strip().lower() in {"1", "true", "yes", "on"} for v in values)


def reset_cron_schedule_cache() -> None:
    """Clear in-memory cron interval timestamps (primarily useful for tests)."""
    with _CRON_LOCK:
        _CRON_LAST_RUN_AT.clear()


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


def process_cron_request(
    query_string: str,
    headers: Mapping[str, str],
    *,
    settings: Settings | None = None,
    telegram: TelegramClient | None = None,
    github_vars: GitHubVariablesClient | None = None,
    state_store: StateStore | None = None,
    features: list[BaseFeature] | None = None,
    monotonic_provider: Callable[[], float] = time.monotonic,
) -> tuple[int, dict[str, Any]]:
    """Validate and execute scheduled bot features via an HTTP cron trigger (e.g. cron-job.org).

    Supports:
    - Auth via ``X-Cron-Secret``, ``Authorization: Bearer <secret>``,
      ``X-Telegram-Bot-Api-Secret-Token``, or ``?secret=<secret>`` against
      ``CRON_SECRET`` (falling back to ``TELEGRAM_WEBHOOK_SECRET``).
    - Optional ``?feature=reminders`` (or comma-separated list) to run specific features.
    - Automatic per-feature interval throttling when running all features (bypass with ``?force=1``
      or by naming a specific ``?feature=...``).
    """
    active_settings = settings or Settings.load()
    query_params = parse_qs(query_string, keep_blank_values=False)

    expected_secret = active_settings.cron_secret or active_settings.telegram_webhook_secret
    if expected_secret:
        received_secret = _extract_cron_secret(headers, query_params)
        if not hmac.compare_digest(received_secret, expected_secret):
            logger.warning("Từ chối request /api/cron do sai secret.")
            return 403, {"ok": False, "error": "Forbidden"}

    requested = _parse_requested_features(query_params)
    force = _is_truthy_param(query_params, "force") or bool(requested)

    if not _CRON_LOCK.acquire(blocking=False):
        logger.info("Bỏ qua lượt /api/cron vì lượt trước vẫn đang chạy.")
        return 200, {"ok": True, "busy": True, "ran": [], "skipped": [], "failed": []}

    try:
        active_telegram = telegram or TelegramClient(
            token=active_settings.telegram_token,
            default_chat_id=active_settings.telegram_chat_id,
        )
        active_github_vars = (
            github_vars
            if github_vars is not None
            else build_github_vars_client(active_settings)
        )
        active_state_store = (
            state_store
            if state_store is not None
            else StateStore(
                active_settings.state_file,
                github_vars=active_github_vars,
                initial_json=active_settings.bot_state_json,
            )
        )
        loaded_features = (
            features
            if features is not None
            else load_features(
                settings=active_settings,
                telegram=active_telegram,
                state_store=active_state_store,
                github_vars=active_github_vars,
            )
        )

        if requested:
            available_names = {f.name for f in loaded_features}
            unknown = [name for name in requested if name not in available_names]
            if unknown:
                return 400, {
                    "ok": False,
                    "error": f"Unknown or disabled feature(s): {', '.join(unknown)}",
                    "available": sorted(available_names),
                }
            requested_set = set(requested)
            target_features = [f for f in loaded_features if f.name in requested_set]
        else:
            target_features = loaded_features

        ran: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        errors: dict[str, str] = {}

        for feature in target_features:
            now_mono = monotonic_provider()
            if not force:
                last_run = _CRON_LAST_RUN_AT.get(feature.name)
                min_gap = max(0.0, float(feature.interval_seconds) - _INTERVAL_GRACE_SECONDS)
                if last_run is not None and (now_mono - last_run) < min_gap:
                    skipped.append(feature.name)
                    continue

            try:
                feature.execute()
                _CRON_LAST_RUN_AT[feature.name] = monotonic_provider()
                ran.append(feature.name)
            except Exception as exc:
                logger.exception("Lỗi khi chạy tính năng '%s' qua /api/cron.", feature.name)
                failed.append(feature.name)
                errors[feature.name] = f"{type(exc).__name__}: {str(exc)[:300]}"

        status_code = 200 if not failed else 500
        response_payload: dict[str, Any] = {
            "ok": not failed,
            "ran": ran,
            "skipped": skipped,
            "failed": failed,
        }
        if errors:
            response_payload["errors"] = errors
        return status_code, response_payload
    finally:
        _CRON_LOCK.release()
