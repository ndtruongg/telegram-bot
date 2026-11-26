"""Feature registry for loading enabled bot features."""

from __future__ import annotations

from bot.config import Settings
from bot.core.feature import BaseFeature
from bot.core.github_vars import GitHubVariablesClient
from bot.core.state import StateStore
from bot.core.telegram_client import TelegramClient
from bot.features.gold_price import GoldPriceFeature
from bot.features.morning_news import MorningNewsFeature


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
            )
        )

    return features
