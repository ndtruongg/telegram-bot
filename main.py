"""Entrypoint for running the multi-feature Telegram bot."""

from __future__ import annotations

import argparse
import logging

from bot.config import Settings
from bot.core import BotScheduler, TelegramClient, setup_logging
from bot.features import load_features


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Multi-feature Telegram Bot")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Chạy tất cả tính năng 1 lần rồi thoát (hữu ích cho cron job hoặc test).",
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

    scheduler = BotScheduler()
    for feature in load_features(settings=settings, telegram=telegram):
        scheduler.register(feature)

    if args.once:
        logger.info("Chạy ở chế độ --once (1 chu kỳ).")
        scheduler.run_once()
    else:
        scheduler.run_forever()


if __name__ == "__main__":
    main()
