"""Feature registry for loading enabled bot features."""

from __future__ import annotations

from bot.config import Settings
from bot.core.feature import BaseFeature
from bot.core.telegram_client import TelegramClient
from bot.features.gold_price import GoldPriceFeature


def load_features(settings: Settings, telegram: TelegramClient) -> list[BaseFeature]:
    """Instantiate and return all enabled features.

    Khi thêm tính năng mới, chỉ cần import class tính năng và thêm vào danh sách bên dưới.
    """
    features: list[BaseFeature] = []

    if settings.gold_price.enabled:
        features.append(
            GoldPriceFeature(
                telegram=telegram,
                config=settings.gold_price,
            )
        )

    return features
