"""Core infrastructure shared across all bot features."""

from bot.core.commands import CommandContext, CommandRouter
from bot.core.feature import BaseFeature
from bot.core.github_vars import GitHubVariablesClient
from bot.core.logger import setup_logging
from bot.core.scheduler import BotScheduler
from bot.core.state import StateStore
from bot.core.telegram_client import TelegramClient

__all__ = [
    "BaseFeature",
    "BotScheduler",
    "CommandContext",
    "CommandRouter",
    "GitHubVariablesClient",
    "StateStore",
    "TelegramClient",
    "setup_logging",
]
