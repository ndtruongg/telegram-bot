"""Consolidated Morning Briefing feature combining Weather, Gold Price, Reminders, and News."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.config import (
    GoldPriceConfig,
    MorningBriefingConfig,
    MorningNewsConfig,
    RemindersConfig,
    WeatherConfig,
)
from bot.core.commands import CommandContext, CommandRouter
from bot.core.feature import BaseFeature
from bot.core.github_vars import GitHubVariablesClient
from bot.core.state import StateStore
from bot.core.telegram_client import TelegramClient
from bot.features.gold_price.scraper import GoldPrice, fetch_sjc_gold_price
from bot.features.lunar_calendar.calendar import VIETNAM_TZ_OFFSET, get_day_auspicious_info
from bot.features.morning_news.rss import NewsArticle, fetch_news_articles
from bot.features.morning_news.summarizer import NewsDigest, summarize_with_gemini
from bot.features.reminders.models import (
    REPEAT_LABELS_VI,
    ReminderItem,
    deserialize_reminders,
    format_countdown_badge,
    format_date_vi,
)
from bot.features.weather.client import (
    WeatherAlertCheck,
    WeatherReport,
    build_raincoat_advice,
    classify_aqi,
    classify_uv,
    describe_weather_code,
    detect_weather_alerts,
    fetch_weather_report,
)
from bot.utils.parsers import format_price, parse_user_price

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_LEN = 4096
_SECTION_DIVIDER = "───────────────"


def _fmt_temp(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}°C" if abs(value - round(value)) >= 0.05 else f"{int(round(value))}°C"


def _tz_offset_hours(now: datetime) -> float:
    offset = now.utcoffset()
    if offset is None:
        return VIETNAM_TZ_OFFSET
    return offset.total_seconds() / 3600.0


@dataclass
class _BriefingBundle:
    """Collected data from all enabled sections for rendering and state syncing."""

    weather_report: WeatherReport | None = None
    weather_alerts: WeatherAlertCheck | None = None
    weather_error: bool = False

    gold_price: GoldPrice | None = None
    gold_target: float | None = None
    gold_error: bool = False

    today_reminders: list[tuple[ReminderItem, date]] = field(default_factory=list)
    upcoming_reminders: list[tuple[int, ReminderItem, date]] = field(default_factory=list)
    nearest_future_reminder: tuple[int, ReminderItem, date] | None = None
    reminder_sent_keys: list[str] = field(default_factory=list)
    reminders_error: bool = False

    news_digest: NewsDigest | None = None
    news_error: bool = False

    succeeded_sections: int = 0
    enabled_sections: int = 0


class MorningBriefingFeature(BaseFeature):
    """Sends a single consolidated morning briefing (Weather + Gold + Reminders + News) at 07:00."""

    _STATE_KEY_LAST_SENT_DATE = "last_sent_date"
    _GITHUB_VAR_WEATHER_CITY = "WEATHER_CITY"
    _GITHUB_VAR_GOLD_TARGET = "GOLD_TARGET_PRICE"
    _GITHUB_VAR_REMINDERS_DATA = "REMINDERS_DATA"

    def __init__(
        self,
        telegram: TelegramClient,
        config: MorningBriefingConfig,
        *,
        weather_config: WeatherConfig | None = None,
        gold_config: GoldPriceConfig | None = None,
        news_config: MorningNewsConfig | None = None,
        reminders_config: RemindersConfig | None = None,
        weather_fetcher: Callable[..., WeatherReport] | None = None,
        gold_fetcher: Callable[[str], GoldPrice | None] | None = None,
        news_fetcher: Callable[[tuple[str, ...], int], list[NewsArticle]] | None = None,
        news_summarizer: Callable[[list[NewsArticle]], NewsDigest] | None = None,
        state_store: StateStore | None = None,
        github_vars: GitHubVariablesClient | None = None,
        now_provider: Callable[[ZoneInfo], datetime] | None = None,
    ) -> None:
        super().__init__(telegram)
        self._config = config
        self._weather_config = weather_config
        self._gold_config = gold_config
        self._news_config = news_config
        self._reminders_config = reminders_config

        self._weather_fetcher = weather_fetcher or fetch_weather_report
        self._gold_fetcher = gold_fetcher or fetch_sjc_gold_price
        self._news_fetcher = news_fetcher or (
            lambda urls, limit: fetch_news_articles(urls, max_articles=limit)
        )
        self._news_summarizer = news_summarizer or (
            lambda articles: summarize_with_gemini(
                articles,
                api_key=self._news_config.gemini_api_key if self._news_config else "",
                model=self._news_config.gemini_model if self._news_config else "gemini-3.5-flash-lite",
                max_highlights=self._config.max_news_items,
            )
        )

        self._state_store = state_store
        self._github_vars = github_vars
        self._now_provider = now_provider or (lambda tz: datetime.now(tz))
        self._last_sent_date: str | None = (
            state_store.get(self.name, self._STATE_KEY_LAST_SENT_DATE)
            if state_store is not None
            else None
        )

    @property
    def name(self) -> str:
        return "morning_briefing"

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

    def _active_weather_city(self) -> str:
        assert self._weather_config is not None
        if self._github_vars is not None:
            raw_city = self._github_vars.get_variable(self._GITHUB_VAR_WEATHER_CITY)
            if raw_city is not None and raw_city.strip():
                return raw_city.strip()
        if self._state_store is not None:
            local_city = self._state_store.get("weather", "city")
            if isinstance(local_city, str) and local_city.strip():
                return local_city.strip()
        return self._weather_config.city

    def _active_gold_target(self) -> float:
        assert self._gold_config is not None
        if self._github_vars is not None:
            raw_target = self._github_vars.get_variable(self._GITHUB_VAR_GOLD_TARGET)
            if raw_target is not None:
                parsed = parse_user_price(raw_target)
                if parsed is not None:
                    return parsed
        return self._gold_config.target_price

    def _load_reminders(self) -> list[ReminderItem]:
        assert self._reminders_config is not None
        if self._github_vars is not None:
            raw_val = self._github_vars.get_variable(self._GITHUB_VAR_REMINDERS_DATA)
            if raw_val is not None:
                return deserialize_reminders(raw_val)
        if self._reminders_config.reminders_json:
            return deserialize_reminders(self._reminders_config.reminders_json)
        if self._state_store is not None:
            saved_raw = self._state_store.get("reminders", "local_reminders_json")
            if isinstance(saved_raw, str) and saved_raw.strip():
                return deserialize_reminders(saved_raw)
        return []

    def register_commands(self, router: CommandRouter) -> None:
        """Expose /briefing and /briefing_status Telegram slash commands."""
        router.register(
            command="briefing",
            description="Xem ngay bản tin tổng hợp (Thời tiết + Giá vàng + Tin tức + Nhắc việc)",
            handler=self._cmd_briefing,
        )
        router.register(
            command="briefing_status",
            description="Xem lịch gửi và cấu hình bản tin tổng hợp buổi sáng",
            handler=self._cmd_briefing_status,
        )

    def execute(self) -> None:
        tz = self._get_timezone()
        now = self._now_provider(tz)
        today = now.date()
        today_str = today.isoformat()

        if self._last_sent_date == today_str:
            logger.info("Bản tin tổng hợp sáng ngày %s đã được gửi trước đó, bỏ qua.", today_str)
            return

        if (now.hour, now.minute) < (self._config.send_hour, self._config.send_minute):
            logger.info(
                "Chưa đến giờ gửi bản tin tổng hợp sáng (%02d:%02d %s, hiện tại %02d:%02d), bỏ qua.",
                self._config.send_hour,
                self._config.send_minute,
                self._config.timezone,
                now.hour,
                now.minute,
            )
            return

        bundle = self._collect_bundle(now)
        if bundle.enabled_sections == 0:
            logger.info("Bản tin tổng hợp buổi sáng chưa bật mục nào, bỏ qua.")
            return
        if bundle.succeeded_sections == 0:
            raise RuntimeError(
                "Không thể tải dữ liệu cho bất kỳ mục nào trong bản tin tổng hợp buổi sáng."
            )

        message = self._format_briefing_message(bundle, now)
        if not self.telegram.send_message(message, disable_web_page_preview=True):
            raise RuntimeError("Gửi bản tin tổng hợp buổi sáng qua Telegram thất bại.")

        self._set_last_sent_date(today_str)
        self._sync_component_states(bundle, now)
        logger.info(
            "Đã gửi bản tin tổng hợp buổi sáng ngày %s (%d/%d mục thành công).",
            today_str,
            bundle.succeeded_sections,
            bundle.enabled_sections,
        )

    def _collect_bundle(self, now: datetime) -> _BriefingBundle:
        bundle = _BriefingBundle()
        today = now.date()

        # 1. Weather
        if self._config.include_weather and self._weather_config is not None:
            bundle.enabled_sections += 1
            try:
                city = self._active_weather_city()
                use_coords = (
                    self._weather_config.latitude is not None
                    and self._weather_config.longitude is not None
                    and city.strip().lower() == self._weather_config.city.strip().lower()
                )
                lat = self._weather_config.latitude if use_coords else None
                lon = self._weather_config.longitude if use_coords else None
                report_tz = self._weather_config.timezone if use_coords else "auto"
                report = self._weather_fetcher(
                    city,
                    latitude=lat,
                    longitude=lon,
                    timezone=report_tz,
                )
                bundle.weather_report = report
                if self._weather_config.alerts_enabled:
                    bundle.weather_alerts = detect_weather_alerts(
                        report,
                        rain_lookahead_hours=self._weather_config.rain_lookahead_hours,
                        rain_alert_mm=self._weather_config.rain_alert_mm,
                        rain_alert_prob=self._weather_config.rain_alert_prob,
                        aqi_threshold=self._weather_config.aqi_alert_threshold,
                    )
                bundle.succeeded_sections += 1
            except Exception:
                logger.exception("Lỗi tải thời tiết cho bản tin tổng hợp sáng.")
                bundle.weather_error = True

        # 2. Gold Price
        if self._config.include_gold and self._gold_config is not None:
            bundle.enabled_sections += 1
            try:
                target_price = self._active_gold_target()
                gold = self._gold_fetcher(self._gold_config.url)
                if gold is None:
                    raise RuntimeError(f"Không thể lấy giá vàng từ {self._gold_config.url}")
                bundle.gold_price = gold
                bundle.gold_target = target_price
                bundle.succeeded_sections += 1
            except Exception:
                logger.exception("Lỗi tải giá vàng cho bản tin tổng hợp sáng.")
                bundle.gold_error = True

        # 3. Reminders
        if self._config.include_reminders and self._reminders_config is not None:
            bundle.enabled_sections += 1
            try:
                items = self._load_reminders()
                future_other: list[tuple[int, ReminderItem, date]] = []

                for item in items:
                    occ_date = item.next_occurrence(today)
                    days_left = (occ_date - today).days
                    if days_left < 0:
                        continue

                    token = item.notification_token()
                    parsed_time = item.parsed_time()

                    if days_left == 0:
                        bundle.today_reminders.append((item, occ_date))
                        bundle.reminder_sent_keys.append(
                            f"{item.id}:{occ_date.isoformat()}:{token}:d0"
                        )
                        # If timed reminder is already due at/before briefing time, mark at_time
                        # so RemindersFeature does not double-send in the same morning tick.
                        if parsed_time is not None and (now.hour, now.minute) >= parsed_time:
                            bundle.reminder_sent_keys.append(
                                f"{item.id}:{occ_date.isoformat()}:{token}:at_time"
                            )
                    else:
                        lead_days = item.effective_lead_days(self._reminders_config.lead_days)
                        if days_left in lead_days:
                            bundle.upcoming_reminders.append((days_left, item, occ_date))
                            bundle.reminder_sent_keys.append(
                                f"{item.id}:{occ_date.isoformat()}:{token}:d{days_left}"
                            )
                        else:
                            future_other.append((days_left, item, occ_date))

                bundle.today_reminders.sort(
                    key=lambda entry: (entry[0].event_time or "99:99", entry[0].id)
                )
                bundle.upcoming_reminders.sort(
                    key=lambda entry: (entry[0], entry[1].event_time or "99:99", entry[1].id)
                )
                if not bundle.today_reminders and not bundle.upcoming_reminders and future_other:
                    future_other.sort(
                        key=lambda entry: (entry[0], entry[1].event_time or "99:99", entry[1].id)
                    )
                    bundle.nearest_future_reminder = future_other[0]

                bundle.succeeded_sections += 1
            except Exception:
                logger.exception("Lỗi tải nhắc việc cho bản tin tổng hợp sáng.")
                bundle.reminders_error = True

        # 4. News
        if self._config.include_news and self._news_config is not None:
            bundle.enabled_sections += 1
            try:
                articles = self._news_fetcher(
                    self._news_config.rss_urls,
                    self._news_config.max_articles,
                )
                if not articles:
                    raise RuntimeError("Không tải được bài viết nào từ nguồn RSS.")
                bundle.news_digest = self._news_summarizer(articles)
                bundle.succeeded_sections += 1
            except Exception:
                logger.exception("Lỗi tải tin tức cho bản tin tổng hợp sáng.")
                bundle.news_error = True

        return bundle

    def _sync_component_states(self, bundle: _BriefingBundle, now: datetime) -> None:
        """Update shared state keys for consolidated components to avoid duplicate morning alerts."""
        if self._state_store is None:
            return

        today = now.date()
        today_str = today.isoformat()
        now_iso = now.isoformat()

        if bundle.weather_report is not None:
            self._state_store.set("weather", "last_sent_date", today_str)
            self._state_store.set("weather", "last_alert_check_at", now_iso)
            if bundle.weather_alerts is not None:
                if bundle.weather_alerts.heavy_rain is not None:
                    self._state_store.set("weather", "last_rain_alert_at", now_iso)
                if bundle.weather_alerts.bad_aqi is not None:
                    self._state_store.set("weather", "last_aqi_alert_at", now_iso)

        if bundle.news_digest is not None:
            self._state_store.set("morning_news", "last_sent_date", today_str)

        if bundle.reminder_sent_keys:
            existing = self._state_store.get("reminders", "sent_notifications")
            sent_map: dict[str, str] = (
                {str(k): str(v) for k, v in existing.items()}
                if isinstance(existing, dict)
                else {}
            )
            for key in bundle.reminder_sent_keys:
                sent_map[key] = today_str
            cutoff = (today - timedelta(days=14)).isoformat()
            pruned = {k: v for k, v in sent_map.items() if v >= cutoff}
            self._state_store.set("reminders", "sent_notifications", pruned)

    def _cmd_briefing(self, _ctx: CommandContext) -> str:
        now = self._now_provider(self._get_timezone())
        bundle = self._collect_bundle(now)
        if bundle.enabled_sections == 0:
            return "⚠️ Bản tin tổng hợp buổi sáng hiện chưa bật mục nội dung nào."
        if bundle.succeeded_sections == 0:
            return "⚠️ Không thể tải dữ liệu bản tin tổng hợp lúc này. Vui lòng thử lại sau."
        return self._format_briefing_message(bundle, now)

    def _cmd_briefing_status(self, _ctx: CommandContext) -> str:
        enabled_text = "Đang bật ✅" if self._config.enabled else "Đang tắt ⏸️"
        schedule_text = (
            f"{self._config.send_hour:02d}:{self._config.send_minute:02d} "
            f"({html.escape(self._config.timezone)})"
        )
        mode_text = (
            "Gộp 1 tin duy nhất (tắt gửi sáng rời rạc) ✅"
            if self._config.suppress_individual
            else "Gửi song song cùng các tính năng riêng lẻ ⚙️"
        )
        last_sent = (
            html.escape(self._last_sent_date) if self._last_sent_date else "Chưa ghi nhận"
        )

        def _flag(enabled: bool) -> str:
            return "✅" if enabled else "⏸️"

        return (
            f"☀️ <b>TRẠNG THÁI BẢN TIN TỔNG HỢP BUỔI SÁNG</b>\n\n"
            f"Trạng thái: <b>{enabled_text}</b>\n"
            f"Giờ gửi hằng ngày: <b>{schedule_text}</b>\n"
            f"Chế độ gửi: <b>{mode_text}</b>\n"
            f"Lần gửi gần nhất: <b>{last_sent}</b>\n\n"
            f"<b>Các mục trong bản tin:</b>\n"
            f"• {_flag(self._config.include_weather)} Thời tiết &amp; AQI\n"
            f"• {_flag(self._config.include_gold)} Giá vàng SJC\n"
            f"• {_flag(self._config.include_reminders)} Nhắc việc &amp; Đếm ngược hôm nay\n"
            f"• {_flag(self._config.include_news)} Tin tức nổi bật (tối đa <b>{self._config.max_news_items}</b> tin)\n\n"
            f"<i>Gõ /briefing để xem bản tin tổng hợp ngay lập tức</i>"
        )

    def _format_briefing_message(self, bundle: _BriefingBundle, now: datetime) -> str:
        date_vi = format_date_vi(now.date(), include_weekday=True)
        lines: list[str] = [f"☀️ <b>BẢN TIN TỔNG HỢP BUỔI SÁNG — {date_vi}</b>"]

        # Compact Vietnamese lunar date subtitle (pure local computation, zero network latency)
        try:
            lunar_info = get_day_auspicious_info(now.date(), time_zone=_tz_offset_hours(now))
            day_icon = "🟢 Hoàng Đạo" if lunar_info.is_hoang_dao else "🔴 Hắc Đạo"
            lines.append(
                f"<i>🌙 Âm lịch: {lunar_info.lunar.format_short()} • "
                f"Ngày {lunar_info.day_can_chi} • {day_icon}</i>"
            )
        except Exception:
            logger.debug("Bỏ qua dòng âm lịch trong bản tin sáng.", exc_info=True)

        # 1. Weather section
        if self._config.include_weather and self._weather_config is not None:
            lines.extend(["", _SECTION_DIVIDER])
            lines.extend(self._format_weather_section(bundle))

        # 2. Gold price section
        if self._config.include_gold and self._gold_config is not None:
            lines.extend(["", _SECTION_DIVIDER])
            lines.extend(self._format_gold_section(bundle))

        # 3. Reminders section
        if self._config.include_reminders and self._reminders_config is not None:
            lines.extend(["", _SECTION_DIVIDER])
            lines.extend(self._format_reminders_section(bundle, now))

        # Build footer in advance so we can accurately budget space for news items
        footer_parts: list[str] = []
        if bundle.news_digest is not None:
            footer_parts.append(
                "🤖 Tin tức: Gemini AI" if bundle.news_digest.used_ai else "🗞️ Tin tức: RSS"
            )
        if bundle.weather_report is not None:
            footer_parts.append("🌤️ Open-Meteo")
        footer_parts.append("Gõ /briefing để cập nhật")
        footer = f"<i>{' • '.join(footer_parts)}</i>"

        # 4. News section (budget-aware so the complete message stays <= 4096 chars)
        if self._config.include_news and self._news_config is not None:
            lines.extend(["", _SECTION_DIVIDER])
            self._append_news_section(lines, bundle, footer=footer)

        lines.extend(["", _SECTION_DIVIDER, footer])
        return self._truncate_message(lines)

    @staticmethod
    def _format_weather_section(bundle: _BriefingBundle) -> list[str]:
        if bundle.weather_error or bundle.weather_report is None:
            return [
                "🌤️ <b>THỜI TIẾT &amp; KHÔNG KHÍ</b>",
                "• ⚠️ <i>Không thể tải dữ liệu thời tiết lúc này.</i>",
            ]

        report = bundle.weather_report
        loc_label = html.escape(report.location.name)
        cur_icon, cur_desc = describe_weather_code(report.current_weather_code)

        section = [f"🌤️ <b>THỜI TIẾT — {loc_label}</b>"]

        # Current snapshot
        cur_parts = [f"{cur_icon} {html.escape(cur_desc)} <b>{_fmt_temp(report.current_temp)}</b>"]
        if report.current_apparent_temp is not None:
            cur_parts[-1] += f" (cảm giác {_fmt_temp(report.current_apparent_temp)})"
        if report.current_humidity is not None:
            cur_parts.append(f"💧 {report.current_humidity}%")
        if report.current_wind_speed is not None:
            cur_parts.append(f"💨 {report.current_wind_speed:.0f} km/h")
        section.append("• Hiện tại: " + " • ".join(cur_parts))

        # Today summary
        today = report.today
        if today is not None:
            today_parts = [f"<b>{_fmt_temp(today.temp_min)} – {_fmt_temp(today.temp_max)}</b>"]
            if today.precipitation_prob_max is not None:
                rain_sum = (
                    f" (~{today.precipitation_sum:.1f} mm)"
                    if today.precipitation_sum is not None and today.precipitation_sum > 0
                    else ""
                )
                today_parts.append(f"🌧️ Mưa <b>{today.precipitation_prob_max}%</b>{rain_sum}")
            if today.uv_index_max is not None:
                uv_icon, uv_label, _ = classify_uv(today.uv_index_max)
                today_parts.append(f"{uv_icon} UV <b>{today.uv_index_max:.1f}</b> ({uv_label})")
            section.append("• Hôm nay: " + " • ".join(today_parts))

        # Air quality
        aq = report.air_quality
        if aq is not None and aq.us_aqi is not None:
            aqi_icon, aqi_label, _ = classify_aqi(aq.us_aqi)
            pm_suffix = f" • PM2.5: {aq.pm2_5:.1f} µg/m³" if aq.pm2_5 is not None else ""
            section.append(
                f"• Không khí: {aqi_icon} US AQI <b>{aq.us_aqi}</b> ({aqi_label}{pm_suffix})"
            )

        # Raincoat advice
        rain_icon, rain_advice = build_raincoat_advice(report)
        section.append(f"• {rain_icon} {rain_advice}")

        # Imminent alerts (if any)
        alerts = bundle.weather_alerts
        if alerts is not None and alerts.has_alert:
            if alerts.heavy_rain is not None:
                hr = alerts.heavy_rain
                section.append(
                    f"• ⚠️ <b>Cảnh báo mưa lớn</b> lúc <b>{html.escape(hr.peak_hour_label)}</b> "
                    f"(~{hr.max_precipitation_mm:.1f} mm/h, {hr.max_probability}%)"
                )
            if alerts.bad_aqi is not None:
                ba = alerts.bad_aqi
                section.append(
                    f"• {ba.level_icon} <b>Không khí {ba.level_label.lower()}</b> "
                    f"(US AQI <b>{ba.us_aqi}</b>) — nhớ đeo khẩu trang!"
                )

        return section

    def _format_gold_section(self, bundle: _BriefingBundle) -> list[str]:
        if bundle.gold_error or bundle.gold_price is None:
            return [
                "💰 <b>GIÁ VÀNG SJC</b>",
                "• ⚠️ <i>Không thể lấy dữ liệu giá vàng lúc này.</i>",
            ]

        data = bundle.gold_price
        target = bundle.gold_target if bundle.gold_target is not None else (
            self._gold_config.target_price if self._gold_config else 0.0
        )
        diff = data.sell - target
        if diff <= 0:
            status_line = "✅ <b>Đã đạt ngưỡng mục tiêu!</b>"
        else:
            status_line = f"⏳ Cao hơn mục tiêu <b>{format_price(diff)}</b>"

        url = html.escape(self._gold_config.url, quote=True) if self._gold_config else ""
        link_suffix = f" (<a href='{url}'>chi tiết</a>)" if url else ""

        return [
            "💰 <b>GIÁ VÀNG SJC</b>",
            f"• <b>{html.escape(data.name)}</b>: Mua <b>{html.escape(data.buy_raw)}</b> | "
            f"Bán <b>{html.escape(data.sell_raw)}</b>{link_suffix}",
            f"• Mục tiêu: <b>{format_price(target)}</b> — {status_line}",
        ]

    @staticmethod
    def _format_reminders_section(bundle: _BriefingBundle, now: datetime) -> list[str]:
        if bundle.reminders_error:
            return [
                "📌 <b>NHẮC VIỆC &amp; SỰ KIỆN</b>",
                "• ⚠️ <i>Không thể tải danh sách nhắc việc lúc này.</i>",
            ]

        section = ["📌 <b>NHẮC VIỆC &amp; SỰ KIỆN</b>"]

        if not bundle.today_reminders and not bundle.upcoming_reminders:
            if bundle.nearest_future_reminder is not None:
                days_left, item, occ = bundle.nearest_future_reminder
                section.append(
                    f"• <i>Hôm nay không có nhắc việc. Gần nhất: "
                    f"<b>{html.escape(item.title)}</b> ({format_countdown_badge(days_left)} — "
                    f"{occ.strftime('%d/%m')})</i>"
                )
            else:
                section.append("• <i>Hôm nay không có nhắc việc hay sự kiện đến hạn.</i>")
            return section

        for item, _occ in bundle.today_reminders:
            parsed_time = item.parsed_time()
            due_now = parsed_time is not None and (now.hour, now.minute) >= parsed_time
            icon = "⏰" if due_now else "🔥"
            time_prefix = f"<b>{item.event_time}</b> — " if item.event_time else ""
            repeat_str = (
                f" • 🔁 {REPEAT_LABELS_VI[item.repeat]}" if item.repeat != "none" else ""
            )
            due_tag = " <i>(đến giờ!)</i>" if due_now else ""
            section.append(
                f"• {icon} <b>Hôm nay:</b> {time_prefix}<b>{html.escape(item.title)}</b>"
                f"{due_tag}{repeat_str} (<code>#{item.id}</code>)"
            )

        for days_left, item, occ in bundle.upcoming_reminders:
            badge = format_countdown_badge(days_left)
            time_str = f" {item.event_time}" if item.event_time else ""
            section.append(
                f"• {badge}: <b>{html.escape(item.title)}</b> "
                f"({occ.strftime('%d/%m')}{time_str})"
            )

        return section

    def _append_news_section(
        self,
        lines: list[str],
        bundle: _BriefingBundle,
        *,
        footer: str,
    ) -> None:
        lines.append("📰 <b>TIN TỨC NỔI BẬT</b>")
        if bundle.news_error or bundle.news_digest is None or not bundle.news_digest.items:
            lines.append("• ⚠️ <i>Không thể tải tin tức lúc này.</i>")
            return

        digest = bundle.news_digest
        reserve_len = len(_SECTION_DIVIDER) + len(footer) + 90
        base_len = sum(len(line) + 1 for line in lines)

        if digest.overview:
            overview_line = f"<i>{html.escape(digest.overview)}</i>"
            if base_len + len(overview_line) + 1 < _TELEGRAM_MAX_LEN - reserve_len:
                lines.append(overview_line)
                base_len += len(overview_line) + 1

        max_items = max(1, self._config.max_news_items)
        added = 0
        for idx, item in enumerate(digest.items[:max_items], start=1):
            safe_headline = html.escape(item.headline)
            safe_summary = html.escape(item.summary)
            safe_source = html.escape(item.article.source)
            safe_link = html.escape(item.article.link, quote=True)

            block_lines = [f"<b>{idx}. {safe_headline}</b>"]
            if safe_summary:
                block_lines.append(safe_summary)
            block_lines.append(f"🔗 <a href='{safe_link}'>{safe_source}</a>")
            block = "\n".join(block_lines)

            # Separate each news item with a blank line for clean readability
            extra_len = len(block) + 2
            if base_len + extra_len > _TELEGRAM_MAX_LEN - reserve_len:
                break
            lines.append("")
            lines.append(block)
            base_len += extra_len
            added += 1

        if added == 0:
            lines.append("• <i>Gõ /news_now để xem đầy đủ tin tức sáng nay.</i>")

    @staticmethod
    def _truncate_message(lines: list[str]) -> str:
        output: list[str] = []
        current_len = 0
        truncated = False
        for line in lines:
            added_len = len(line) + (1 if output else 0)
            if current_len + added_len > _TELEGRAM_MAX_LEN - 60:
                truncated = True
                break
            output.append(line)
            current_len += added_len

        if truncated:
            while output and not output[-1]:
                output.pop()
            output.append("")
            output.append("<i>… (đã rút gọn do vượt giới hạn ký tự Telegram)</i>")
        return "\n".join(output).strip()
