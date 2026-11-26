"""Base interface that every bot feature implements."""

from __future__ import annotations

from abc import ABC, abstractmethod

from bot.core.telegram_client import TelegramClient


class BaseFeature(ABC):
    """Abstract base class for pluggable bot features.

    To add a new feature:
    1. Create a subpackage under ``bot/features/<feature_name>/``.
    2. Subclass ``BaseFeature`` and implement ``name``, ``interval_seconds``, and ``execute()``.
    3. Register the feature in ``bot/features/__init__.py``.
    """

    def __init__(self, telegram: TelegramClient) -> None:
        self.telegram = telegram

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable unique name of the feature."""

    @property
    @abstractmethod
    def interval_seconds(self) -> int:
        """How often (in seconds) this feature should run."""

    @abstractmethod
    def execute(self) -> None:
        """Run one check/execution cycle of the feature."""
