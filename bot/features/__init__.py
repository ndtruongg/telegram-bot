"""Feature registry for loading enabled bot features."""

from __future__ import annotations

from bot.config import Settings
from bot.core.feature import BaseFeature
from bot.core.github_vars import GitHubVariablesClient
from bot.core.state import StateStore
from bot.core.telegram_client import TelegramClient
from bot.features.daily_vocab import DailyVocabFeature
from bot.features.gold_price import GoldPriceFeature
from bot.features.lunar_calendar import LunarCalendarFeature
from bot.features.morning_briefing import MorningBriefingFeature
from bot.features.morning_news import MorningNewsFeature
from bot.features.reminders import RemindersFeature
from bot.features.weather import WeatherFeature


def load_features(
    settings: Settings,
    telegram: TelegramClient,
    state_store: StateStore | None = None,
    github_vars: GitHubVariablesClient | None = None,
    include_disabled: bool = False,
) -> list[BaseFeature]:
    """Instantiate and return bot features.

    Khi thêm tính năng mới, chỉ cần import class tính năng và thêm vào danh sách bên dưới.
    """
    features: list[BaseFeature] = []
    briefing_cfg = settings.morning_briefing
    suppress_individual = briefing_cfg.enabled and briefing_cfg.suppress_individual

    if briefing_cfg.enabled or include_disabled:
        features.append(
            MorningBriefingFeature(
                telegram=telegram,
                config=briefing_cfg,
                weather_config=settings.weather,
                gold_config=settings.gold_price,
                news_config=settings.morning_news,
                reminders_config=settings.reminders,
                state_store=state_store,
                github_vars=github_vars,
            )
        )

    if settings.gold_price.enabled or include_disabled:
        features.append(
            GoldPriceFeature(
                telegram=telegram,
                config=settings.gold_price,
                state_store=state_store,
                github_vars=github_vars,
            )
        )

    if settings.morning_news.enabled or include_disabled:
        features.append(
            MorningNewsFeature(
                telegram=telegram,
                config=settings.morning_news,
                state_store=state_store,
                suppress_scheduled=suppress_individual and briefing_cfg.include_news,
            )
        )

    if settings.reminders.enabled or include_disabled:
        features.append(
            RemindersFeature(
                telegram=telegram,
                config=settings.reminders,
                state_store=state_store,
                github_vars=github_vars,
                suppress_morning_digest=suppress_individual and briefing_cfg.include_reminders,
            )
        )

    if settings.daily_vocab.enabled or include_disabled:
        features.append(
            DailyVocabFeature(
                telegram=telegram,
                config=settings.daily_vocab,
                state_store=state_store,
                github_vars=github_vars,
            )
        )

    if settings.lunar_calendar.enabled or include_disabled:
        features.append(
            LunarCalendarFeature(
                telegram=telegram,
                config=settings.lunar_calendar,
                state_store=state_store,
            )
        )

    if settings.weather.enabled or include_disabled:
        features.append(
            WeatherFeature(
                telegram=telegram,
                config=settings.weather,
                state_store=state_store,
                github_vars=github_vars,
                suppress_morning_digest=suppress_individual and briefing_cfg.include_weather,
            )
        )

    return features
