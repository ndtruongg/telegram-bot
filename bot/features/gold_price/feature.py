"""Gold price alert feature implementation."""

from __future__ import annotations

import logging
from typing import Callable

from bot.config import GoldPriceConfig
from bot.core.feature import BaseFeature
from bot.core.state import StateStore
from bot.core.telegram_client import TelegramClient
from bot.features.gold_price.scraper import GoldPrice, fetch_sjc_gold_price
from bot.utils.parsers import format_price

logger = logging.getLogger(__name__)


class GoldPriceFeature(BaseFeature):
    """Monitors SJC gold sell price and sends a Telegram alert when it hits target."""

    _STATE_KEY_LAST_ALERTED = "last_alerted_price"

    def __init__(
        self,
        telegram: TelegramClient,
        config: GoldPriceConfig,
        fetcher: Callable[[str], GoldPrice | None] = fetch_sjc_gold_price,
        state_store: StateStore | None = None,
    ) -> None:
        super().__init__(telegram)
        self._config = config
        self._fetcher = fetcher
        self._state_store = state_store
        saved_price = (
            state_store.get(self.name, self._STATE_KEY_LAST_ALERTED)
            if state_store is not None
            else None
        )
        self._last_alerted_price: float | None = (
            float(saved_price) if isinstance(saved_price, (int, float)) else None
        )

    @property
    def name(self) -> str:
        return "gold_price"

    @property
    def interval_seconds(self) -> int:
        return self._config.check_interval

    def _set_last_alerted_price(self, price: float | None) -> None:
        self._last_alerted_price = price
        if self._state_store is not None:
            self._state_store.set(self.name, self._STATE_KEY_LAST_ALERTED, price)

    def execute(self) -> None:
        data = self._fetcher(self._config.url)
        if data is None:
            raise RuntimeError(f"Không thể lấy dữ liệu giá vàng từ {self._config.url}")

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
                logger.info("Giá %s đã được cảnh báo trước đó, bỏ qua.", data.sell_raw)
                return

            message = self._build_alert_message(data)
            if not self.telegram.send_message(message):
                raise RuntimeError("Gửi thông báo cảnh báo giá vàng qua Telegram thất bại.")
            self._set_last_alerted_price(data.sell)
        else:
            # Reset trạng thái khi giá tăng trở lại trên ngưỡng mục tiêu
            self._set_last_alerted_price(None)

    def _build_alert_message(self, data: GoldPrice) -> str:
        return (
            f"🔔 <b>CẢNH BÁO GIÁ TỐT!</b>\n\n"
            f"Loại: <b>{data.name}</b>\n"
            f"Giá bán ra hiện tại: <b>{data.sell_raw}</b> "
            f"(Mục tiêu: {format_price(self._config.target_price)})\n"
            f"Giá mua vào: {data.buy_raw}\n"
            f"🔗 <a href='{self._config.url}'>Xem chi tiết</a>"
        )
