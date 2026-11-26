"""Gold price alert feature implementation."""

from __future__ import annotations

import logging
from typing import Callable

from bot.config import GoldPriceConfig
from bot.core.commands import CommandContext, CommandRouter
from bot.core.feature import BaseFeature
from bot.core.github_vars import GitHubVariablesClient
from bot.core.state import StateStore
from bot.core.telegram_client import TelegramClient
from bot.features.gold_price.scraper import GoldPrice, fetch_sjc_gold_price
from bot.utils.parsers import format_price, parse_user_price, serialize_price

logger = logging.getLogger(__name__)


class GoldPriceFeature(BaseFeature):
    """Monitors SJC gold sell price and sends a Telegram alert when it hits target."""

    _STATE_KEY_LAST_ALERTED = "last_alerted_price"
    _STATE_KEY_LAST_TARGET = "last_target_price"
    _GITHUB_VAR_TARGET_PRICE = "GOLD_TARGET_PRICE"

    def __init__(
        self,
        telegram: TelegramClient,
        config: GoldPriceConfig,
        fetcher: Callable[[str], GoldPrice | None] = fetch_sjc_gold_price,
        state_store: StateStore | None = None,
        github_vars: GitHubVariablesClient | None = None,
    ) -> None:
        super().__init__(telegram)
        self._config = config
        self._fetcher = fetcher
        self._state_store = state_store
        self._github_vars = github_vars
        self._target_price: float = config.target_price

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

    def get_target_price(self, *, refresh_from_github: bool = False) -> float:
        """Return active target price, optionally fetching the live value from GitHub Variables."""
        if refresh_from_github and self._github_vars is not None:
            raw_val = self._github_vars.get_variable(self._GITHUB_VAR_TARGET_PRICE)
            if raw_val is not None:
                parsed = parse_user_price(raw_val)
                if parsed is not None:
                    self._target_price = parsed
                    return parsed
        return self._target_price

    def register_commands(self, router: CommandRouter) -> None:
        """Expose /gold_check, /gold_set, and /gold_status commands."""
        router.register(
            command="gold_check",
            description="Kiểm tra giá vàng hiện tại ngay lập tức",
            handler=self._cmd_gold_check,
        )
        router.register(
            command="gold_set",
            usage="/gold_set <số tiền>",
            description="Đổi mức giá mục tiêu (VD: /gold_set 13450000 hoặc /gold_set 84.5)",
            handler=self._cmd_gold_set,
        )
        router.register(
            command="gold_status",
            description="Xem mức giá mục tiêu hiện tại đang cài đặt",
            handler=self._cmd_gold_status,
        )

    def execute(self) -> None:
        target_price = self.get_target_price()

        # Nếu mục tiêu vừa thay đổi (VD qua /gold_set trên webhook), reset trạng thái chống spam
        if self._state_store is not None:
            prev_target = self._state_store.get(self.name, self._STATE_KEY_LAST_TARGET)
            if prev_target is not None and float(prev_target) != target_price:
                self._set_last_alerted_price(None)
            self._state_store.set(self.name, self._STATE_KEY_LAST_TARGET, target_price)

        data = self._fetcher(self._config.url)
        if data is None:
            raise RuntimeError(f"Không thể lấy dữ liệu giá vàng từ {self._config.url}")

        logger.info(
            "%s - Mua: %s | Bán: %s (Mục tiêu: %s)",
            data.name,
            data.buy_raw,
            data.sell_raw,
            format_price(target_price),
        )

        if data.sell <= target_price:
            # Tránh spam cùng một mức giá liên tục qua nhiều chu kỳ
            if self._last_alerted_price == data.sell:
                logger.info("Giá %s đã được cảnh báo trước đó, bỏ qua.", data.sell_raw)
                return

            message = self._build_alert_message(data, target_price)
            if not self.telegram.send_message(message):
                raise RuntimeError("Gửi thông báo cảnh báo giá vàng qua Telegram thất bại.")
            self._set_last_alerted_price(data.sell)
        else:
            # Reset trạng thái khi giá tăng trở lại trên ngưỡng mục tiêu
            self._set_last_alerted_price(None)

    def _cmd_gold_check(self, _ctx: CommandContext) -> str:
        target_price = self.get_target_price(refresh_from_github=True)
        data = self._fetcher(self._config.url)
        if data is None:
            return (
                f"⚠️ Không thể lấy dữ liệu giá vàng từ nguồn hiện tại.\n"
                f"🔗 <a href='{self._config.url}'>Mở trang nguồn</a>"
            )

        diff = data.sell - target_price
        if diff <= 0:
            status_line = "✅ <b>Đã đạt ngưỡng mục tiêu!</b>"
        else:
            status_line = f"⏳ Cao hơn mục tiêu <b>{format_price(diff)}</b>"

        return (
            f"💰 <b>GIÁ VÀNG HIỆN TẠI</b>\n\n"
            f"Loại: <b>{data.name}</b>\n"
            f"Giá mua vào: <b>{data.buy_raw}</b>\n"
            f"Giá bán ra: <b>{data.sell_raw}</b>\n"
            f"Giá mục tiêu: <b>{format_price(target_price)}</b>\n"
            f"Trạng thái: {status_line}\n"
            f"🔗 <a href='{self._config.url}'>Xem chi tiết</a>"
        )

    def _cmd_gold_set(self, ctx: CommandContext) -> str:
        if not ctx.args:
            return (
                "⚠️ Vui lòng nhập mức giá mục tiêu.\n"
                "Ví dụ:\n"
                "• <code>/gold_set 13450000</code> (theo đồng)\n"
                "• <code>/gold_set 13.450.000</code> (có dấu phân cách)\n"
                "• <code>/gold_set 84.5</code> (theo triệu)"
            )

        new_price = parse_user_price(ctx.args)
        if new_price is None:
            return (
                f"⚠️ Giá trị <code>{ctx.args}</code> không hợp lệ.\n"
                "Vui lòng nhập số dương, ví dụ: <code>/gold_set 13450000</code> hoặc <code>/gold_set 84.5</code>."
            )

        old_price = self.get_target_price(refresh_from_github=True)
        serialized = serialize_price(new_price)

        if self._github_vars is not None:
            if not self._github_vars.set_variable(self._GITHUB_VAR_TARGET_PRICE, serialized):
                return (
                    "❌ Không thể cập nhật GitHub Variable <code>GOLD_TARGET_PRICE</code>.\n"
                    "Vui lòng kiểm tra lại <code>GITHUB_TOKEN</code> (quyền <b>Variables: Read and write</b>) "
                    "và <code>GITHUB_REPOSITORY</code>."
                )
            sync_note = f"GitHub Variable (<code>{self._github_vars.repository}</code>)"
        else:
            sync_note = "Bộ nhớ tạm (chưa cấu hình <code>GITHUB_TOKEN</code> / <code>GITHUB_REPOSITORY</code>)"

        self._target_price = new_price
        self._set_last_alerted_price(None)
        if self._state_store is not None:
            self._state_store.set(self.name, self._STATE_KEY_LAST_TARGET, new_price)

        return (
            f"✅ <b>Đã cập nhật giá mục tiêu!</b>\n\n"
            f"Giá cũ: <b>{format_price(old_price)}</b>\n"
            f"Giá mới: <b>{format_price(new_price)}</b>\n"
            f"Lưu tại: {sync_note}"
        )

    def _cmd_gold_status(self, _ctx: CommandContext) -> str:
        target_price = self.get_target_price(refresh_from_github=True)
        source = (
            f"GitHub Variable (<code>{self._github_vars.repository}</code>)"
            if self._github_vars is not None
            else "Biến môi trường cục bộ"
        )
        enabled_text = "Đang bật ✅" if self._config.enabled else "Đang tắt ⏸️"
        return (
            f"📊 <b>TRẠNG THÁI THEO DÕI GIÁ VÀNG</b>\n\n"
            f"Trạng thái: <b>{enabled_text}</b>\n"
            f"Giá mục tiêu hiện tại: <b>{format_price(target_price)}</b>\n"
            f"Nguồn đồng bộ: {source}\n"
            f"🔗 <a href='{self._config.url}'>Nguồn giá</a>"
        )

    def _build_alert_message(self, data: GoldPrice, target_price: float) -> str:
        return (
            f"🔔 <b>CẢNH BÁO GIÁ TỐT!</b>\n\n"
            f"Loại: <b>{data.name}</b>\n"
            f"Giá bán ra hiện tại: <b>{data.sell_raw}</b> "
            f"(Mục tiêu: {format_price(target_price)})\n"
            f"Giá mua vào: {data.buy_raw}\n"
            f"🔗 <a href='{self._config.url}'>Xem chi tiết</a>"
        )
