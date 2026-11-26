"""Gold price alert feature implementation."""

from __future__ import annotations

import logging
from typing import Callable

from bot.config import GoldPriceConfig
from bot.core.feature import BaseFeature
from bot.core.telegram_client import TelegramClient
from bot.features.gold_price.scraper import GoldPrice, fetch_sjc_gold_price
from bot.utils.parsers import format_price

logger = logging.getLogger(__name__)


class GoldPriceFeature(BaseFeature):
    """Monitors SJC gold sell price and sends a Telegram alert when it hits target."""

    def __init__(
        self,
        telegram: TelegramClient,
        config: GoldPriceConfig,
        fetcher: Callable[[str], GoldPrice | None] = fetch_sjc_gold_price,
    ) -> None:
        super().__init__(telegram)
        self._config = config
        self._fetcher = fetcher
        self._last_alerted_price: float | None = None

    @property
    def name(self) -> str:
        return "gold_price"

    @property
    def interval_seconds(self) -> int:
        return self._config.check_interval

    def execute(self) -> None:
        data = self._fetcher(self._config.url)
        if data is None:
            return

        logger.info(
            "%s - Mua: %s | Bán: %s (Mục tiêu: %s)",
            data.name,
            data.buy_raw,
            data.sell_raw,
            format_price(self._config.target_price),
        )

        if data.sell <= self._config.target_price:
            # Tránh spam cùng một mức giá liên tục qua nhiều chu kỳ
            if self._last_alerted_price == data.sell:
                logger.debug("Giá %s đã được cảnh báo trước đó, bỏ qua.", data.sell_raw)
                return

            message = self._build_alert_message(data)
            if self.telegram.send_message(message):
                self._last_alerted_price = data.sell
        else:
            # Reset trạng thái khi giá tăng trở lại trên ngưỡng mục tiêu
            self._last_alerted_price = None

    def _build_alert_message(self, data: GoldPrice) -> str:
        return (
            f"🔔 <b>CẢNH BÁO GIÁ TỐT!</b>\n\n"
            f"Loại: <b>{data.name}</b>\n"
            f"Giá bán ra hiện tại: <b>{data.sell_raw}</b> "
            f"(Mục tiêu: {format_price(self._config.target_price)})\n"
            f"Giá mua vào: {data.buy_raw}\n"
            f"🔗 <a href='{self._config.url}'>Xem chi tiết</a>"
        )
