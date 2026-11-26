"""Entrypoint for running the multi-feature Telegram bot."""

from __future__ import annotations

import argparse
import logging
import sys

from bot.config import Settings
from bot.core import BotScheduler, StateStore, TelegramClient, setup_logging
from bot.core.webhook import build_command_router, build_github_vars_client
from bot.features import load_features


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Multi-feature Telegram Bot")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Chạy tất cả tính năng 1 lần rồi thoát (hữu ích cho cron job hoặc test).",
    )
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Trả về exit code 1 nếu có tính năng chạy lỗi trong chế độ --once (hữu ích cho CI/GitHub Actions).",
    )
    parser.add_argument(
        "--feature",
        action="append",
        dest="features",
        help="Chỉ chạy tính năng có tên chỉ định (có thể truyền nhiều lần). Bỏ trống để chạy tất cả.",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="Đường dẫn file lưu trạng thái giữa các lần chạy (mặc định đọc từ STATE_FILE hoặc .bot_state.json).",
    )
    parser.add_argument(
        "--sync-commands",
        action="store_true",
        help="Đăng ký danh sách lệnh / lên menu gợi ý của Telegram rồi thoát.",
    )
    parser.add_argument(
        "--set-webhook",
        metavar="URL",
        default=None,
        help="Đăng ký URL webhook (kèm TELEGRAM_WEBHOOK_SECRET nếu có) và đồng bộ danh sách lệnh lên Telegram.",
    )
    parser.add_argument(
        "--delete-webhook",
        action="store_true",
        help="Xoá webhook đang gắn trên Telegram Bot rồi thoát.",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Đường dẫn tới file cấu hình .env (mặc định: .env).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings.load(env_file=args.env)
    setup_logging(settings.log_level)

    logger = logging.getLogger(__name__)
    telegram = TelegramClient(
        token=settings.telegram_token,
        default_chat_id=settings.telegram_chat_id,
    )
    github_vars = build_github_vars_client(settings)

    if args.delete_webhook:
        ok = telegram.delete_webhook()
        sys.exit(0 if ok else 1)

    if args.set_webhook or args.sync_commands:
        router = build_command_router(
            settings=settings,
            telegram=telegram,
            github_vars=github_vars,
        )
        ok = True
        if args.set_webhook:
            ok = telegram.set_webhook(
                url=args.set_webhook,
                secret_token=settings.telegram_webhook_secret or None,
            ) and ok
        ok = telegram.set_my_commands(router.get_telegram_commands()) and ok
        sys.exit(0 if ok else 1)

    state_store = StateStore(args.state_file or settings.state_file)

    selected = set(args.features or [])
    loaded_features = load_features(
        settings=settings,
        telegram=telegram,
        state_store=state_store,
        github_vars=github_vars,
    )

    if selected:
        available_names = {f.name for f in loaded_features}
        unknown = sorted(selected - available_names)
        if unknown:
            logger.error(
                "Không tìm thấy hoặc chưa bật tính năng: %s (hiện có: %s)",
                ", ".join(unknown),
                ", ".join(sorted(available_names)) or "trống",
            )
            sys.exit(2)
        loaded_features = [f for f in loaded_features if f.name in selected]

    scheduler = BotScheduler()
    for feature in loaded_features:
        scheduler.register(feature)

    if args.once:
        logger.info("Chạy ở chế độ --once (1 chu kỳ).")
        all_ok = scheduler.run_once()
        if args.fail_on_error and not all_ok:
            sys.exit(1)
    else:
        scheduler.run_forever()


if __name__ == "__main__":
    main()
