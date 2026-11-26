"""Application configuration loaded from environment variables and .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_NEWS_RSS_URLS: tuple[str, ...] = (
    "https://vnexpress.net/rss/tin-noi-bat.rss",
    "https://tuoitre.vn/rss/tin-moi-nhat.rss",
)


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


def _env(key: str, default: str) -> str:
    """Return stripped environment variable value, falling back to default if unset or blank."""
    value = os.getenv(key, "").strip()
    return value if value else default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class GoldPriceConfig:
    enabled: bool
    url: str
    target_price: float
    check_interval: int


@dataclass(frozen=True)
class MorningNewsConfig:
    enabled: bool = True
    rss_urls: tuple[str, ...] = DEFAULT_NEWS_RSS_URLS
    max_articles: int = 20
    max_highlights: int = 12
    send_hour: int = 7
    send_minute: int = 0
    timezone: str = "Asia/Ho_Chi_Minh"
    check_interval: int = 900
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    telegram_chat_id: str
    log_level: str
    gold_price: GoldPriceConfig
    morning_news: MorningNewsConfig = field(default_factory=MorningNewsConfig)
    state_file: str = ".bot_state.json"
    telegram_webhook_secret: str = ""
    github_token: str = ""
    github_repository: str = ""

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

        gold_enabled = _env_bool("GOLD_ENABLED", True)
        gold_url = _env("GOLD_URL", "https://webgia.com/gia-vang/sjc/")
        gold_target_price = float(_env("GOLD_TARGET_PRICE", "13500000"))
        gold_check_interval = int(_env("GOLD_CHECK_INTERVAL", "3600"))
        state_file = _env("STATE_FILE", ".bot_state.json")

        raw_rss_urls = _env("NEWS_RSS_URLS", "")
        parsed_rss_urls = tuple(
            u.strip() for u in raw_rss_urls.split(",") if u.strip()
        ) or DEFAULT_NEWS_RSS_URLS
        news_send_hour = max(0, min(23, int(_env("NEWS_SEND_HOUR", "7"))))
        news_send_minute = max(0, min(59, int(_env("NEWS_SEND_MINUTE", "0"))))
        news_max_articles = max(1, int(_env("NEWS_MAX_ARTICLES", "20")))
        news_max_highlights = max(1, int(_env("NEWS_MAX_HIGHLIGHTS", "12")))
        news_check_interval = max(60, int(_env("NEWS_CHECK_INTERVAL", "900")))

        return cls(
            telegram_token=token,
            telegram_chat_id=chat_id,
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            gold_price=GoldPriceConfig(
                enabled=gold_enabled,
                url=gold_url,
                target_price=gold_target_price,
                check_interval=gold_check_interval,
            ),
            morning_news=MorningNewsConfig(
                enabled=_env_bool("NEWS_ENABLED", True),
                rss_urls=parsed_rss_urls,
                max_articles=news_max_articles,
                max_highlights=news_max_highlights,
                send_hour=news_send_hour,
                send_minute=news_send_minute,
                timezone=_env("NEWS_TIMEZONE", "Asia/Ho_Chi_Minh"),
                check_interval=news_check_interval,
                gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
                gemini_model=_env("GEMINI_MODEL", "gemini-2.5-flash"),
            ),
            state_file=state_file,
            telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip(),
            github_token=os.getenv("GITHUB_TOKEN", "").strip(),
            github_repository=os.getenv("GITHUB_REPOSITORY", "").strip(),
        )
