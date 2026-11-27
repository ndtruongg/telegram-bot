"""Application configuration loaded from environment variables and .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_NEWS_RSS_URLS: tuple[str, ...] = (
    "https://vnexpress.net/rss/tin-noi-bat.rss",
    "https://tuoitre.vn/rss/tin-moi-nhat.rss",
)

DEFAULT_REMINDER_LEAD_DAYS: tuple[int, ...] = (7, 3, 1, 0)

DEFAULT_LUNAR_LEAD_DAYS: tuple[int, ...] = (3, 1, 0)
DEFAULT_LUNAR_MOON_LEAD_DAYS: tuple[int, ...] = (1, 0)

DEFAULT_VOCAB_SEND_TIMES: tuple[tuple[int, int], ...] = ((8, 0), (20, 0))
DEFAULT_VOCAB_TOPICS: tuple[str, ...] = ("it", "communication", "ielts", "mixed")
DEFAULT_GEMINI_MODEL: str = "gemini-3.5-flash-lite"


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


def _env_optional_float(key: str) -> float | None:
    raw = os.getenv(key, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_lead_days(raw: str, default: tuple[int, ...] = DEFAULT_REMINDER_LEAD_DAYS) -> tuple[int, ...]:
    text = raw.strip()
    if not text:
        return default
    days: set[int] = set()
    for part in text.split(","):
        cleaned = part.strip()
        if not cleaned:
            continue
        try:
            val = int(cleaned)
        except ValueError:
            continue
        if 0 <= val <= 365:
            days.add(val)
    if not days:
        return default
    days.add(0)
    return tuple(sorted(days, reverse=True))


def _parse_send_times(
    raw: str,
    default: tuple[tuple[int, int], ...] = DEFAULT_VOCAB_SEND_TIMES,
) -> tuple[tuple[int, int], ...]:
    """Parse comma-separated HH:MM (or HHhMM) schedule times into sorted unique (hour, minute) tuples."""
    text = raw.strip()
    if not text:
        return default
    slots: set[tuple[int, int]] = set()
    for part in text.split(","):
        cleaned = part.strip().lower().replace("h", ":")
        if not cleaned:
            continue
        if ":" in cleaned:
            h_str, _, m_str = cleaned.partition(":")
            m_str = m_str or "0"
        else:
            h_str, m_str = cleaned, "0"
        try:
            hour = int(h_str)
            minute = int(m_str)
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            slots.add((hour, minute))
    return tuple(sorted(slots)) if slots else default


def _parse_vocab_topic(raw: str, default: str = "mixed") -> str:
    """Normalize configured vocabulary topic name."""
    cleaned = raw.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "it": "it",
        "tech": "it",
        "dev": "it",
        "software": "it",
        "cntt": "it",
        "communication": "communication",
        "comm": "communication",
        "daily": "communication",
        "speaking": "communication",
        "giaotiep": "communication",
        "giao_tiep": "communication",
        "work": "communication",
        "ielts": "ielts",
        "academic": "ielts",
        "exam": "ielts",
        "mixed": "mixed",
        "mix": "mixed",
        "all": "mixed",
        "auto": "mixed",
        "random": "mixed",
        "tonghop": "mixed",
        "tong_hop": "mixed",
    }
    return aliases.get(cleaned, default)


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
    check_interval: int = 60
    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_GEMINI_MODEL


@dataclass(frozen=True)
class RemindersConfig:
    enabled: bool = True
    send_hour: int = 7
    send_minute: int = 0
    timezone: str = "Asia/Ho_Chi_Minh"
    lead_days: tuple[int, ...] = DEFAULT_REMINDER_LEAD_DAYS
    check_interval: int = 60
    reminders_json: str = ""


@dataclass(frozen=True)
class DailyVocabConfig:
    enabled: bool = True
    topic: str = "mixed"
    words_per_batch: int = 4
    send_times: tuple[tuple[int, int], ...] = DEFAULT_VOCAB_SEND_TIMES
    timezone: str = "Asia/Ho_Chi_Minh"
    check_interval: int = 60
    max_history: int = 300
    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_GEMINI_MODEL
    history_json: str = ""


@dataclass(frozen=True)
class LunarCalendarConfig:
    enabled: bool = True
    send_hour: int = 7
    send_minute: int = 0
    timezone: str = "Asia/Ho_Chi_Minh"
    lead_days: tuple[int, ...] = DEFAULT_LUNAR_LEAD_DAYS
    moon_lead_days: tuple[int, ...] = DEFAULT_LUNAR_MOON_LEAD_DAYS
    check_interval: int = 60


@dataclass(frozen=True)
class WeatherConfig:
    enabled: bool = True
    city: str = "Ho Chi Minh City"
    latitude: float | None = None
    longitude: float | None = None
    send_hour: int = 6
    send_minute: int = 30
    timezone: str = "Asia/Ho_Chi_Minh"
    check_interval: int = 60
    alerts_enabled: bool = True
    alert_check_interval: int = 1800
    alert_cooldown_minutes: int = 180
    alert_start_hour: int = 6
    alert_end_hour: int = 22
    rain_lookahead_hours: int = 3
    rain_alert_mm: float = 5.0
    rain_alert_prob: int = 75
    aqi_alert_threshold: int = 151


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    telegram_chat_id: str
    log_level: str
    gold_price: GoldPriceConfig
    morning_news: MorningNewsConfig = field(default_factory=MorningNewsConfig)
    reminders: RemindersConfig = field(default_factory=RemindersConfig)
    daily_vocab: DailyVocabConfig = field(default_factory=DailyVocabConfig)
    lunar_calendar: LunarCalendarConfig = field(default_factory=LunarCalendarConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    state_file: str = ".bot_state.json"
    bot_state_json: str = ""
    telegram_webhook_secret: str = ""
    cron_secret: str = ""
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
        news_check_interval = max(60, int(_env("NEWS_CHECK_INTERVAL", "60")))

        reminders_send_hour = max(0, min(23, int(_env("REMINDERS_SEND_HOUR", "7"))))
        reminders_send_minute = max(0, min(59, int(_env("REMINDERS_SEND_MINUTE", "0"))))
        reminders_check_interval = max(60, int(_env("REMINDERS_CHECK_INTERVAL", "60")))
        reminders_lead_days = _parse_lead_days(_env("REMINDERS_LEAD_DAYS", ""))

        vocab_topic = _parse_vocab_topic(_env("VOCAB_TOPIC", "mixed"))
        vocab_words_per_batch = max(1, min(10, int(_env("VOCAB_WORDS_PER_BATCH", "4"))))
        vocab_send_times = _parse_send_times(_env("VOCAB_SEND_TIMES", ""))
        vocab_check_interval = max(60, int(_env("VOCAB_CHECK_INTERVAL", "60")))
        vocab_max_history = max(20, min(1000, int(_env("VOCAB_MAX_HISTORY", "300"))))
        lunar_send_hour = max(0, min(23, int(_env("LUNAR_SEND_HOUR", "7"))))
        lunar_send_minute = max(0, min(59, int(_env("LUNAR_SEND_MINUTE", "0"))))
        lunar_check_interval = max(60, int(_env("LUNAR_CHECK_INTERVAL", "60")))
        lunar_lead_days = _parse_lead_days(
            _env("LUNAR_LEAD_DAYS", ""),
            default=DEFAULT_LUNAR_LEAD_DAYS,
        )
        lunar_moon_lead_days = _parse_lead_days(
            _env("LUNAR_MOON_LEAD_DAYS", ""),
            default=DEFAULT_LUNAR_MOON_LEAD_DAYS,
        )
        weather_city = _env("WEATHER_CITY", "Ho Chi Minh City")
        weather_lat = _env_optional_float("WEATHER_LATITUDE")
        weather_lon = _env_optional_float("WEATHER_LONGITUDE")
        weather_send_hour = max(0, min(23, int(_env("WEATHER_SEND_HOUR", "6"))))
        weather_send_minute = max(0, min(59, int(_env("WEATHER_SEND_MINUTE", "30"))))
        weather_check_interval = max(60, int(_env("WEATHER_CHECK_INTERVAL", "60")))
        weather_alert_check_interval = max(300, int(_env("WEATHER_ALERT_CHECK_INTERVAL", "1800")))
        weather_alert_cooldown = max(15, int(_env("WEATHER_ALERT_COOLDOWN_MINUTES", "180")))
        weather_alert_start_hour = max(0, min(23, int(_env("WEATHER_ALERT_START_HOUR", "6"))))
        weather_alert_end_hour = max(0, min(23, int(_env("WEATHER_ALERT_END_HOUR", "22"))))
        weather_rain_lookahead = max(1, min(12, int(_env("WEATHER_RAIN_LOOKAHEAD_HOURS", "3"))))
        weather_rain_alert_mm = max(0.5, float(_env("WEATHER_RAIN_ALERT_MM", "5.0")))
        weather_rain_alert_prob = max(10, min(100, int(_env("WEATHER_RAIN_ALERT_PROB", "75"))))
        weather_aqi_threshold = max(50, min(500, int(_env("WEATHER_AQI_THRESHOLD", "151"))))
        gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        gemini_model = _env("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)

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
                gemini_api_key=gemini_api_key,
                gemini_model=gemini_model,
            ),
            reminders=RemindersConfig(
                enabled=_env_bool("REMINDERS_ENABLED", True),
                send_hour=reminders_send_hour,
                send_minute=reminders_send_minute,
                timezone=_env("REMINDERS_TIMEZONE", "Asia/Ho_Chi_Minh"),
                lead_days=reminders_lead_days,
                check_interval=reminders_check_interval,
                reminders_json=os.getenv("REMINDERS_DATA", "").strip(),
            ),
            daily_vocab=DailyVocabConfig(
                enabled=_env_bool("VOCAB_ENABLED", True),
                topic=vocab_topic,
                words_per_batch=vocab_words_per_batch,
                send_times=vocab_send_times,
                timezone=_env("VOCAB_TIMEZONE", "Asia/Ho_Chi_Minh"),
                check_interval=vocab_check_interval,
                max_history=vocab_max_history,
                gemini_api_key=gemini_api_key,
                gemini_model=gemini_model,
                history_json=os.getenv("VOCAB_HISTORY", "").strip(),
            ),
            lunar_calendar=LunarCalendarConfig(
                enabled=_env_bool("LUNAR_ENABLED", True),
                send_hour=lunar_send_hour,
                send_minute=lunar_send_minute,
                timezone=_env("LUNAR_TIMEZONE", "Asia/Ho_Chi_Minh"),
                lead_days=lunar_lead_days,
                moon_lead_days=lunar_moon_lead_days,
                check_interval=lunar_check_interval,
            ),
            weather=WeatherConfig(
                enabled=_env_bool("WEATHER_ENABLED", True),
                city=weather_city,
                latitude=weather_lat,
                longitude=weather_lon,
                send_hour=weather_send_hour,
                send_minute=weather_send_minute,
                timezone=_env("WEATHER_TIMEZONE", "Asia/Ho_Chi_Minh"),
                check_interval=weather_check_interval,
                alerts_enabled=_env_bool("WEATHER_ALERTS_ENABLED", True),
                alert_check_interval=weather_alert_check_interval,
                alert_cooldown_minutes=weather_alert_cooldown,
                alert_start_hour=weather_alert_start_hour,
                alert_end_hour=weather_alert_end_hour,
                rain_lookahead_hours=weather_rain_lookahead,
                rain_alert_mm=weather_rain_alert_mm,
                rain_alert_prob=weather_rain_alert_prob,
                aqi_alert_threshold=weather_aqi_threshold,
            ),
            state_file=state_file,
            bot_state_json=os.getenv("BOT_STATE_JSON", "").strip(),
            telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip(),
            cron_secret=os.getenv("CRON_SECRET", "").strip(),
            github_token=os.getenv("GITHUB_TOKEN", "").strip(),
            github_repository=os.getenv("GITHUB_REPOSITORY", "").strip(),
        )
