"""Morning news summary feature for Telegram bot."""

from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.config import MorningNewsConfig
from bot.core.commands import CommandContext, CommandRouter
from bot.core.feature import BaseFeature
from bot.core.state import StateStore
from bot.core.telegram_client import TelegramClient
from bot.features.morning_news.rss import NewsArticle, fetch_news_articles
from bot.features.morning_news.summarizer import NewsDigest, summarize_with_gemini

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_LEN = 4096


class MorningNewsFeature(BaseFeature):
    """Fetches Vietnamese top news RSS feeds and sends a daily AI-summarized morning digest."""

    _STATE_KEY_LAST_SENT_DATE = "last_sent_date"

    def __init__(
        self,
        telegram: TelegramClient,
        config: MorningNewsConfig,
        fetcher: Callable[[tuple[str, ...], int], list[NewsArticle]] | None = None,
        summarizer: Callable[[list[NewsArticle]], NewsDigest] | None = None,
        state_store: StateStore | None = None,
        now_provider: Callable[[ZoneInfo], datetime] | None = None,
        suppress_scheduled: bool = False,
    ) -> None:
        super().__init__(telegram)
        self._config = config
        self._fetcher = fetcher or (
            lambda urls, limit: fetch_news_articles(urls, max_articles=limit)
        )
        self._summarizer = summarizer or (
            lambda articles: summarize_with_gemini(
                articles,
                api_key=self._config.gemini_api_key,
                model=self._config.gemini_model,
                max_highlights=self._config.max_highlights,
            )
        )
        self._state_store = state_store
        self._now_provider = now_provider or (lambda tz: datetime.now(tz))
        self._suppress_scheduled = suppress_scheduled
        self._last_sent_date: str | None = (
            state_store.get(self.name, self._STATE_KEY_LAST_SENT_DATE)
            if state_store is not None
            else None
        )

    @property
    def name(self) -> str:
        return "morning_news"

    @property
    def interval_seconds(self) -> int:
        return self._config.check_interval

    def _get_timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self._config.timezone)
        except ZoneInfoNotFoundError:
            logger.warning(
                "Múi giờ '%s' không hợp lệ, dùng mặc định Asia/Ho_Chi_Minh.",
                self._config.timezone,
            )
            return ZoneInfo("Asia/Ho_Chi_Minh")

    def _set_last_sent_date(self, date_str: str) -> None:
        self._last_sent_date = date_str
        if self._state_store is not None:
            self._state_store.set(self.name, self._STATE_KEY_LAST_SENT_DATE, date_str)

    def register_commands(self, router: CommandRouter) -> None:
        """Expose /news_now and /news_status Telegram commands."""
        router.register(
            command="news_now",
            description="Tổng hợp và tóm tắt tin tức nổi bật ngay lập tức",
            handler=self._cmd_news_now,
        )
        router.register(
            command="news_status",
            description="Xem lịch gửi bản tin sáng và trạng thái nguồn tin",
            handler=self._cmd_news_status,
        )

    def execute(self) -> None:
        if self._suppress_scheduled:
            logger.info(
                "Bản tin sáng đã được gộp vào morning_briefing, bỏ qua gửi riêng."
            )
            return

        tz = self._get_timezone()
        now = self._now_provider(tz)
        today_str = now.date().isoformat()

        if self._last_sent_date == today_str:
            logger.info("Bản tin sáng ngày %s đã được gửi trước đó, bỏ qua.", today_str)
            return

        if (now.hour, now.minute) < (self._config.send_hour, self._config.send_minute):
            logger.info(
                "Chưa đến giờ gửi bản tin sáng (%02d:%02d %s, hiện tại %02d:%02d), bỏ qua.",
                self._config.send_hour,
                self._config.send_minute,
                self._config.timezone,
                now.hour,
                now.minute,
            )
            return

        message = self._generate_digest_message(now)
        if not self.telegram.send_message(message, disable_web_page_preview=True):
            raise RuntimeError("Gửi bản tin buổi sáng qua Telegram thất bại.")

        self._set_last_sent_date(today_str)
        logger.info("Đã gửi bản tin sáng ngày %s thành công.", today_str)

    def _generate_digest_message(self, now: datetime) -> str:
        articles = self._fetcher(self._config.rss_urls, self._config.max_articles)
        if not articles:
            raise RuntimeError("Không thể tải tin tức từ các nguồn RSS đã cấu hình.")

        digest = self._summarizer(articles)
        return self._format_digest_message(digest, now)

    def _cmd_news_now(self, _ctx: CommandContext) -> str:
        now = self._now_provider(self._get_timezone())
        try:
            return self._generate_digest_message(now)
        except Exception:
            logger.exception("Không thể tạo bản tin nhanh qua /news_now")
            return "⚠️ Không thể lấy hoặc tóm tắt tin tức lúc này. Vui lòng thử lại sau."

    def _cmd_news_status(self, _ctx: CommandContext) -> str:
        enabled_text = "Đang bật ✅" if self._config.enabled else "Đang tắt ⏸️"
        ai_text = (
            f"Gemini (<code>{html.escape(self._config.gemini_model)}</code>) ✅"
            if self._config.gemini_api_key
            else "Tóm tắt nhanh RSS (chưa có <code>GEMINI_API_KEY</code>) ⚙️"
        )
        schedule_text = (
            f"{self._config.send_hour:02d}:{self._config.send_minute:02d} "
            f"({html.escape(self._config.timezone)})"
        )
        sources = "\n".join(
            f"• {html.escape(url)}" for url in self._config.rss_urls
        )
        last_sent = html.escape(self._last_sent_date) if self._last_sent_date else "Chưa ghi nhận"
        return (
            f"📰 <b>TRẠNG THÁI BẢN TIN BUỔI SÁNG</b>\n\n"
            f"Trạng thái: <b>{enabled_text}</b>\n"
            f"Giờ gửi hằng ngày: <b>{schedule_text}</b>\n"
            f"Tóm tắt AI: {ai_text}\n"
            f"Số tin tối đa: <b>{self._config.max_highlights}</b> tin nổi bật "
            f"(từ {self._config.max_articles} bài RSS)\n"
            f"Lần gửi gần nhất: <b>{last_sent}</b>\n\n"
            f"<b>Nguồn RSS:</b>\n{sources}"
        )

    @staticmethod
    def _format_digest_message(digest: NewsDigest, now: datetime) -> str:
        date_label = now.strftime("%d/%m/%Y")
        lines = [f"☀️ <b>BẢN TIN BUỔI SÁNG — {date_label}</b>", ""]

        if digest.overview:
            lines.append(f"<i>{html.escape(digest.overview)}</i>")
            lines.append("")

        for idx, item in enumerate(digest.items, start=1):
            safe_headline = html.escape(item.headline)
            safe_summary = html.escape(item.summary)
            safe_source = html.escape(item.article.source)
            safe_link = html.escape(item.article.link, quote=True)

            block_lines = [f"<b>{idx}. {safe_headline}</b>"]
            if safe_summary:
                block_lines.append(safe_summary)
            block_lines.append(f"🔗 <a href='{safe_link}'>{safe_source}</a>")
            block = "\n".join(block_lines)

            # Respect Telegram 4096-character limit cleanly at item boundaries
            candidate = "\n".join(lines + [block, ""])
            if len(candidate) > _TELEGRAM_MAX_LEN - 140:
                break
            lines.append(block)
            lines.append("")

        if digest.used_ai:
            lines.append("🤖 <i>Tóm tắt tự động bởi Gemini AI</i>")
        else:
            note = (
                f" ({html.escape(digest.fallback_reason)})"
                if digest.fallback_reason
                else ""
            )
            lines.append(f"🗞️ <i>Tổng hợp nhanh từ RSS{note}</i>")

        return "\n".join(lines).strip()
