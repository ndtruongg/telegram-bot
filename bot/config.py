"""Application configuration loaded from environment variables and .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(env_path: Path) -> None:
    """Load key=value pairs from a .env file into os.environ (without overwriting existing vars)."""
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class GoldPriceConfig:
    enabled: bool
    url: str
    target_price: float
    check_interval: int


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    telegram_chat_id: str
    log_level: str
    gold_price: GoldPriceConfig

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "Settings":
        _load_dotenv(Path(env_file))

        token = os.getenv("TELEGRAM_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

        if not token or not chat_id:
            raise ValueError(
                "Thiếu TELEGRAM_TOKEN hoặc TELEGRAM_CHAT_ID. "
                "Vui lòng cấu hình trong file .env hoặc biến môi trường."
            )

        gold_enabled = os.getenv("GOLD_ENABLED", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        gold_url = os.getenv("GOLD_URL", "https://webgia.com/gia-vang/sjc/").strip()
        gold_target_price = float(os.getenv("GOLD_TARGET_PRICE", "13500000"))
        gold_check_interval = int(os.getenv("GOLD_CHECK_INTERVAL", "3600"))

        return cls(
            telegram_token=token,
            telegram_chat_id=chat_id,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            gold_price=GoldPriceConfig(
                enabled=gold_enabled,
                url=gold_url,
                target_price=gold_target_price,
                check_interval=gold_check_interval,
            ),
        )
