"""Weather forecast and air quality alert feature powered by Open-Meteo."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.config import WeatherConfig
from bot.core.commands import CommandContext, CommandRouter
from bot.core.feature import BaseFeature
from bot.core.github_vars import GitHubVariablesClient
from bot.core.state import StateStore
from bot.core.telegram_client import TelegramClient
from bot.features.weather.client import (
    BadAqiAlertInfo,
    DailyForecast,
    GeoLocation,
    HeavyRainAlertInfo,
    WeatherAlertCheck,
    WeatherReport,
    build_raincoat_advice,
    classify_aqi,
    classify_uv,
    describe_weather_code,
    detect_weather_alerts,
    fetch_weather_report,
    geocode_city,
)

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_LEN = 4096

_WEEKDAY_VI = {
    0: "T2",
    1: "T3",
    2: "T4",
    3: "T5",
    4: "T6",
    5: "T7",
    6: "CN",
}


def _parse_iso_dt(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _format_day_short(date_iso: str) -> str:
    try:
        dt = datetime.strptime(date_iso[:10], "%Y-%m-%d").date()
        return f"{_WEEKDAY_VI[dt.weekday()]} {dt.strftime('%d/%m')}"
    except ValueError:
        return date_iso


def _fmt_temp(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}°C" if abs(value - round(value)) >= 0.05 else f"{int(round(value))}°C"


class WeatherFeature(BaseFeature):
    """Sends early-morning weather & AQI forecasts, alerts on heavy rain or bad AQI, and handles /weather."""

    _GITHUB_VAR_CITY = "WEATHER_CITY"
    _STATE_KEY_LAST_SENT_DATE = "last_sent_date"
    _STATE_KEY_LAST_ALERT_CHECK = "last_alert_check_at"
    _STATE_KEY_LAST_RAIN_ALERT = "last_rain_alert_at"
    _STATE_KEY_LAST_AQI_ALERT = "last_aqi_alert_at"
    _STATE_KEY_LOCAL_CITY = "city"

    def __init__(
        self,
        telegram: TelegramClient,
        config: WeatherConfig,
        fetcher: Callable[..., WeatherReport] | None = None,
        geocoder: Callable[[str], GeoLocation | None] | None = None,
        state_store: StateStore | None = None,
        github_vars: GitHubVariablesClient | None = None,
        now_provider: Callable[[ZoneInfo], datetime] | None = None,
        suppress_morning_digest: bool = False,
    ) -> None:
        super().__init__(telegram)
        self._config = config
        self._fetcher = fetcher or fetch_weather_report
        self._geocoder = geocoder or geocode_city
        self._state_store = state_store
        self._github_vars = github_vars
        self._now_provider = now_provider or (lambda tz: datetime.now(tz))
        self._suppress_morning_digest = suppress_morning_digest

        self._city: str = self._load_initial_city()
        self._last_sent_date: str | None = self._load_str_state(self._STATE_KEY_LAST_SENT_DATE)
        self._last_alert_check_at: str | None = self._load_str_state(self._STATE_KEY_LAST_ALERT_CHECK)
        self._last_rain_alert_at: str | None = self._load_str_state(self._STATE_KEY_LAST_RAIN_ALERT)
        self._last_aqi_alert_at: str | None = self._load_str_state(self._STATE_KEY_LAST_AQI_ALERT)

    @property
    def name(self) -> str:
        return "weather"

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

    def _load_str_state(self, key: str) -> str | None:
        if self._state_store is None:
            return None
        val = self._state_store.get(self.name, key)
        return val if isinstance(val, str) and val.strip() else None

    def _save_str_state(self, key: str, value: str | None) -> None:
        if self._state_store is not None:
            self._state_store.set(self.name, key, value)

    def _load_initial_city(self) -> str:
        saved = self._load_str_state(self._STATE_KEY_LOCAL_CITY)
        return saved or self._config.city

    def get_city(self, *, refresh_from_github: bool = False) -> str:
        """Return active default city, optionally refreshing from GitHub Variable WEATHER_CITY."""
        if refresh_from_github and self._github_vars is not None:
            raw_val = self._github_vars.get_variable(self._GITHUB_VAR_CITY)
            if raw_val is not None and raw_val.strip():
                self._city = raw_val.strip()
        return self._city

    def _persist_city(self, city_name: str) -> tuple[bool, str]:
        if self._github_vars is not None:
            if not self._github_vars.set_variable(self._GITHUB_VAR_CITY, city_name):
                return (
                    False,
                    "❌ Không thể cập nhật GitHub Variable <code>WEATHER_CITY</code>.\n"
                    "Vui lòng kiểm tra lại <code>GITHUB_TOKEN</code> (quyền <b>Variables: Read and write</b>) "
                    "và <code>GITHUB_REPOSITORY</code>.",
                )
            sync_note = f"GitHub Variable (<code>{html.escape(self._github_vars.repository)}</code>)"
        else:
            sync_note = (
                "File trạng thái cục bộ"
                if self._state_store is not None
                else "Bộ nhớ tạm (chưa cấu hình <code>GITHUB_TOKEN</code> / <code>GITHUB_REPOSITORY</code>)"
            )

        self._city = city_name
        if self._state_store is not None and self._github_vars is None:
            self._save_str_state(self._STATE_KEY_LOCAL_CITY, city_name)

        # Reset alert cooldowns so alerts for the new city are not suppressed
        self._last_rain_alert_at = None
        self._last_aqi_alert_at = None
        self._save_str_state(self._STATE_KEY_LAST_RAIN_ALERT, None)
        self._save_str_state(self._STATE_KEY_LAST_AQI_ALERT, None)
        return True, sync_note

    def _fetch_for_city(self, city: str, *, use_configured_coords: bool) -> WeatherReport:
        lat = self._config.latitude if use_configured_coords else None
        lon = self._config.longitude if use_configured_coords else None
        tz = self._config.timezone if use_configured_coords else "auto"
        return self._fetcher(city, latitude=lat, longitude=lon, timezone=tz)

    def _is_within_alert_hours(self, hour: int) -> bool:
        start = self._config.alert_start_hour
        end = self._config.alert_end_hour
        if start <= end:
            return start <= hour <= end
        return hour >= start or hour <= end

    def _is_alert_check_due(self, now: datetime) -> bool:
        if not self._config.alerts_enabled:
            return False
        if not self._is_within_alert_hours(now.hour):
            return False
        last_dt = _parse_iso_dt(self._last_alert_check_at)
        if last_dt is None:
            return True
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=now.tzinfo)
        elapsed = (now - last_dt).total_seconds()
        return elapsed >= max(60, self._config.alert_check_interval)

    def _is_in_cooldown(self, last_iso: str | None, now: datetime) -> bool:
        last_dt = _parse_iso_dt(last_iso)
        if last_dt is None:
            return False
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=now.tzinfo)
        cooldown = timedelta(minutes=max(1, self._config.alert_cooldown_minutes))
        return (now - last_dt) < cooldown

    def register_commands(self, router: CommandRouter) -> None:
        """Expose /weather, /weather_city, and /weather_status commands."""
        router.register(
            command="weather",
            usage="/weather [thành phố]",
            description="Xem thời tiết, UV, AQI và tư vấn mang áo mưa",
            handler=self._cmd_weather,
        )
        router.register(
            command="weather_city",
            usage="/weather_city [thành phố]",
            description="Xem hoặc đổi thành phố mặc định cho dự báo thời tiết",
            handler=self._cmd_weather_city,
        )
        router.register(
            command="weather_status",
            description="Xem lịch gửi dự báo sáng & cấu hình cảnh báo thời tiết",
            handler=self._cmd_weather_status,
        )

    def execute(self) -> None:
        tz = self._get_timezone()
        now = self._now_provider(tz)
        today_str = now.date().isoformat()

        morning_due = (
            not self._suppress_morning_digest
            and self._last_sent_date != today_str
            and (now.hour, now.minute) >= (self._config.send_hour, self._config.send_minute)
        )
        # Re-read shared alert timestamps in case morning_briefing updated them in the same run
        if self._state_store is not None:
            self._last_alert_check_at = self._load_str_state(self._STATE_KEY_LAST_ALERT_CHECK)
            self._last_rain_alert_at = self._load_str_state(self._STATE_KEY_LAST_RAIN_ALERT)
            self._last_aqi_alert_at = self._load_str_state(self._STATE_KEY_LAST_AQI_ALERT)
        alert_due = self._is_alert_check_due(now)

        if not morning_due and not alert_due:
            logger.info(
                "Chưa đến giờ gửi dự báo sáng (%02d:%02d) hoặc chu kỳ kiểm tra cảnh báo thời tiết, bỏ qua.",
                self._config.send_hour,
                self._config.send_minute,
            )
            return

        active_city = self.get_city(refresh_from_github=True)
        use_coords = (
            self._config.latitude is not None
            and self._config.longitude is not None
            and active_city.strip().lower() == self._config.city.strip().lower()
        )
        report = self._fetch_for_city(active_city, use_configured_coords=use_coords)

        alert_check = detect_weather_alerts(
            report,
            rain_lookahead_hours=self._config.rain_lookahead_hours,
            rain_alert_mm=self._config.rain_alert_mm,
            rain_alert_prob=self._config.rain_alert_prob,
            aqi_threshold=self._config.aqi_alert_threshold,
        )

        if morning_due:
            message = self._format_forecast_message(
                report,
                now=now,
                is_morning_digest=True,
                alert_check=alert_check if self._config.alerts_enabled else None,
            )
            if not self.telegram.send_message(message, disable_web_page_preview=True):
                raise RuntimeError("Gửi dự báo thời tiết sáng sớm qua Telegram thất bại.")

            self._last_sent_date = today_str
            self._save_str_state(self._STATE_KEY_LAST_SENT_DATE, today_str)

            # Morning forecast already includes active rain/AQI warnings, so record alert check & cooldown
            now_iso = now.isoformat()
            self._last_alert_check_at = now_iso
            self._save_str_state(self._STATE_KEY_LAST_ALERT_CHECK, now_iso)
            if self._config.alerts_enabled:
                if alert_check.heavy_rain is not None:
                    self._last_rain_alert_at = now_iso
                    self._save_str_state(self._STATE_KEY_LAST_RAIN_ALERT, now_iso)
                if alert_check.bad_aqi is not None:
                    self._last_aqi_alert_at = now_iso
                    self._save_str_state(self._STATE_KEY_LAST_AQI_ALERT, now_iso)

            logger.info(
                "Đã gửi dự báo thời tiết buổi sáng cho '%s' ngày %s.",
                report.location.display_name,
                today_str,
            )
            return

        # Periodic severe-weather / bad-AQI alert check
        send_rain = alert_check.heavy_rain is not None and not self._is_in_cooldown(
            self._last_rain_alert_at, now
        )
        send_aqi = alert_check.bad_aqi is not None and not self._is_in_cooldown(
            self._last_aqi_alert_at, now
        )

        now_iso = now.isoformat()
        if send_rain or send_aqi:
            alert_msg = self._format_alert_message(
                report,
                heavy_rain=alert_check.heavy_rain if send_rain else None,
                bad_aqi=alert_check.bad_aqi if send_aqi else None,
                rain_lookahead_hours=self._config.rain_lookahead_hours,
            )
            if not self.telegram.send_message(alert_msg, disable_web_page_preview=True):
                raise RuntimeError("Gửi cảnh báo thời tiết / AQI qua Telegram thất bại.")

            if send_rain:
                self._last_rain_alert_at = now_iso
                self._save_str_state(self._STATE_KEY_LAST_RAIN_ALERT, now_iso)
            if send_aqi:
                self._last_aqi_alert_at = now_iso
                self._save_str_state(self._STATE_KEY_LAST_AQI_ALERT, now_iso)

            logger.info(
                "Đã gửi cảnh báo thời tiết cho '%s' (mưa lớn=%s, AQI xấu=%s).",
                report.location.display_name,
                send_rain,
                send_aqi,
            )
        else:
            logger.info(
                "Kiểm tra cảnh báo thời tiết cho '%s': bình thường hoặc đang trong thời gian chờ.",
                report.location.display_name,
            )

        self._last_alert_check_at = now_iso
        self._save_str_state(self._STATE_KEY_LAST_ALERT_CHECK, now_iso)

    def _cmd_weather(self, ctx: CommandContext) -> str:
        query_city = ctx.args.strip()
        now = self._now_provider(self._get_timezone())

        if query_city:
            target_city = query_city
            use_coords = False
        else:
            target_city = self.get_city(refresh_from_github=True)
            use_coords = (
                self._config.latitude is not None
                and self._config.longitude is not None
                and target_city.strip().lower() == self._config.city.strip().lower()
            )

        try:
            report = self._fetch_for_city(target_city, use_configured_coords=use_coords)
        except LookupError:
            safe_city = html.escape(target_city)
            return (
                f"⚠️ Không tìm thấy thành phố <code>{safe_city}</code>.\n"
                "Hãy thử tên tiếng Việt hoặc tiếng Anh, ví dụ:\n"
                "• <code>/weather Hà Nội</code>\n"
                "• <code>/weather TP HCM</code>\n"
                "• <code>/weather Đà Nẵng</code>\n"
                "• <code>/weather Tokyo</code>"
            )
        except Exception:
            logger.exception("Lỗi khi tra cứu thời tiết cho '%s'", target_city)
            return "⚠️ Không thể tải dữ liệu thời tiết từ Open-Meteo lúc này. Vui lòng thử lại sau."

        alert_check = detect_weather_alerts(
            report,
            rain_lookahead_hours=self._config.rain_lookahead_hours,
            rain_alert_mm=self._config.rain_alert_mm,
            rain_alert_prob=self._config.rain_alert_prob,
            aqi_threshold=self._config.aqi_alert_threshold,
        )
        return self._format_forecast_message(
            report,
            now=now,
            is_morning_digest=False,
            alert_check=alert_check,
        )

    def _cmd_weather_city(self, ctx: CommandContext) -> str:
        query = ctx.args.strip()
        if not query:
            current_city = self.get_city(refresh_from_github=True)
            return (
                f"📍 <b>THÀNH PHỐ THỜI TIẾT MẶC ĐỊNH</b>\n\n"
                f"Hiện tại: <b>{html.escape(current_city)}</b>\n\n"
                f"Đổi thành phố bằng lệnh:\n"
                f"• <code>/weather_city Hà Nội</code>\n"
                f"• <code>/weather_city Đà Nẵng</code>\n"
                f"• <code>/weather_city TP HCM</code>"
            )

        resolved = self._geocoder(query)
        if resolved is None:
            return (
                f"⚠️ Không tìm thấy thành phố <code>{html.escape(query)}</code>.\n"
                "Vui lòng kiểm tra lại tên thành phố (VD: <code>/weather_city Hà Nội</code>)."
            )

        ok, sync_msg = self._persist_city(resolved.name)
        if not ok:
            return sync_msg

        return (
            f"✅ <b>Đã cập nhật thành phố mặc định!</b>\n\n"
            f"📍 Thành phố mới: <b>{html.escape(resolved.display_name)}</b>\n"
            f"🌐 Tọa độ: <code>{resolved.latitude:.4f}, {resolved.longitude:.4f}</code>\n"
            f"💾 Lưu tại: {sync_msg}\n\n"
            f"Gõ <code>/weather</code> để xem thời tiết ngay."
        )

    def _cmd_weather_status(self, _ctx: CommandContext) -> str:
        active_city = self.get_city(refresh_from_github=True)
        enabled_text = "Đang bật ✅" if self._config.enabled else "Đang tắt ⏸️"
        alerts_text = (
            f"Đang bật ✅ ({self._config.alert_start_hour:02d}:00–{self._config.alert_end_hour:02d}:59)"
            if self._config.alerts_enabled
            else "Đang tắt ⏸️"
        )
        schedule_text = (
            f"{self._config.send_hour:02d}:{self._config.send_minute:02d} "
            f"({html.escape(self._config.timezone)})"
        )
        last_forecast = (
            html.escape(self._last_sent_date) if self._last_sent_date else "Chưa ghi nhận"
        )
        source = (
            f"GitHub Variable (<code>{html.escape(self._github_vars.repository)}</code>)"
            if self._github_vars is not None
            else "File trạng thái cục bộ / biến môi trường"
        )

        return (
            f"🌤️ <b>TRẠNG THÁI THỜI TIẾT & CHẤT LƯỢNG KHÔNG KHÍ</b>\n\n"
            f"Trạng thái: <b>{enabled_text}</b>\n"
            f"Thành phố mặc định: <b>{html.escape(active_city)}</b>\n"
            f"Dự báo sáng sớm: <b>{schedule_text}</b>\n"
            f"Cảnh báo tự động: <b>{alerts_text}</b>\n"
            f"Ngưỡng mưa lớn: <b>≥ {self._config.rain_alert_mm:.1f} mm/h</b> hoặc "
            f"<b>≥ {self._config.rain_alert_prob}%</b> (trong {self._config.rain_lookahead_hours} giờ tới)\n"
            f"Ngưỡng AQI xấu: <b>US AQI ≥ {self._config.aqi_alert_threshold}</b>\n"
            f"Giãn cách cảnh báo: <b>{self._config.alert_cooldown_minutes} phút</b>\n"
            f"Dự báo sáng gần nhất: <b>{last_forecast}</b>\n"
            f"Nguồn dữ liệu: Open-Meteo (miễn phí, không cần API key)\n"
            f"Nguồn đồng bộ: {source}"
        )

    @classmethod
    def _format_forecast_message(
        cls,
        report: WeatherReport,
        *,
        now: datetime,
        is_morning_digest: bool,
        alert_check: WeatherAlertCheck | None = None,
    ) -> str:
        loc_label = html.escape(report.location.display_name)
        date_label = now.strftime("%d/%m/%Y")
        cur_icon, cur_desc = describe_weather_code(report.current_weather_code)

        if is_morning_digest:
            header = f"🌅 <b>DỰ BÁO THỜI TIẾT SÁNG SỚM — {date_label}</b>"
        else:
            header = f"{cur_icon} <b>THỜI TIẾT & KHÔNG KHÍ — {loc_label}</b>"

        lines = [header]
        if is_morning_digest:
            lines.append(f"📍 <b>{loc_label}</b>")
        elif report.current_time_iso:
            time_part = report.current_time_iso.replace("T", " ")[:16]
            lines.append(f"<i>Cập nhật: {html.escape(time_part)} ({html.escape(report.timezone)})</i>")
        lines.append("")

        # Current conditions
        temp_part = _fmt_temp(report.current_temp)
        feels_part = (
            f" (cảm giác {_fmt_temp(report.current_apparent_temp)})"
            if report.current_apparent_temp is not None
            else ""
        )
        lines.append(f"🌡️ <b>Hiện tại:</b> {cur_icon} {html.escape(cur_desc)} • <b>{temp_part}</b>{feels_part}")

        details: list[str] = []
        if report.current_humidity is not None:
            details.append(f"💧 Độ ẩm: <b>{report.current_humidity}%</b>")
        if report.current_wind_speed is not None:
            details.append(f"💨 Gió: <b>{report.current_wind_speed:.1f} km/h</b>")
        if details:
            lines.append(" • ".join(details))

        # Today's summary
        today = report.today
        if today is not None:
            today_icon, today_desc = describe_weather_code(today.weather_code)
            lines.append("")
            lines.append("📅 <b>Hôm nay:</b>")
            lines.append(
                f"• Nhiệt độ: <b>{_fmt_temp(today.temp_min)} – {_fmt_temp(today.temp_max)}</b> "
                f"({today_icon} {html.escape(today_desc)})"
            )
            rain_prob = (
                f"{today.precipitation_prob_max}%"
                if today.precipitation_prob_max is not None
                else "—"
            )
            rain_sum = (
                f"{today.precipitation_sum:.1f} mm"
                if today.precipitation_sum is not None
                else "0.0 mm"
            )
            lines.append(f"• Xác suất mưa: <b>{rain_prob}</b> (tổng lượng mưa ~<b>{rain_sum}</b>)")

            if today.uv_index_max is not None:
                uv_icon, uv_label, uv_advice = classify_uv(today.uv_index_max)
                cur_uv_suffix = (
                    f", hiện tại {report.current_uv:.1f}"
                    if report.current_uv is not None and report.current_uv > 0
                    else ""
                )
                advice_part = f" — <i>{html.escape(uv_advice)}</i>" if uv_advice else ""
                lines.append(
                    f"• Chỉ số UV tối đa: {uv_icon} <b>{today.uv_index_max:.1f} ({uv_label})</b>"
                    f"{cur_uv_suffix}{advice_part}"
                )

            if today.sunrise and today.sunset:
                lines.append(f"• Mặt trời: 🌅 {html.escape(today.sunrise)} • 🌇 {html.escape(today.sunset)}")

        # Air quality section
        aq = report.air_quality
        lines.append("")
        if aq is not None and aq.us_aqi is not None:
            aqi_icon, aqi_label, aqi_advice = classify_aqi(aq.us_aqi)
            aq_line = f"🌫️ <b>Chất lượng không khí (US AQI):</b> {aqi_icon} <b>{aq.us_aqi} — {aqi_label}</b>"
            if aq.peak_us_aqi_today is not None and aq.peak_us_aqi_today > aq.us_aqi + 10:
                aq_line += f" (đỉnh hôm nay ~<b>{aq.peak_us_aqi_today}</b>)"
            lines.append(aq_line)

            pollutants: list[str] = []
            if aq.pm2_5 is not None:
                pollutants.append(f"PM2.5: <b>{aq.pm2_5:.1f} µg/m³</b>")
            if aq.pm10 is not None:
                pollutants.append(f"PM10: <b>{aq.pm10:.1f} µg/m³</b>")
            if pollutants:
                lines.append("• " + " • ".join(pollutants))
            if aqi_advice:
                lines.append(f"• <i>{html.escape(aqi_advice)}</i>")
        else:
            lines.append("🌫️ <b>Chất lượng không khí (AQI):</b> <i>Chưa có dữ liệu lúc này</i>")

        # Raincoat / umbrella recommendation
        rain_icon, rain_advice = build_raincoat_advice(report)
        lines.append("")
        lines.append(f"{rain_icon} <b>Lời khuyên ra đường:</b> {rain_advice}")

        # Highlight imminent alerts if present
        if alert_check is not None and alert_check.has_alert:
            lines.append("")
            lines.append("⚠️ <b>LƯU Ý CẢNH BÁO HIỆN TẠI:</b>")
            if alert_check.heavy_rain is not None:
                hr = alert_check.heavy_rain
                _, hr_desc = describe_weather_code(hr.weather_code)
                lines.append(
                    f"• ⛈️ <b>Sắp có mưa lớn</b> lúc <b>{html.escape(hr.peak_hour_label)}</b> "
                    f"(~<b>{hr.max_precipitation_mm:.1f} mm/h</b>, xác suất <b>{hr.max_probability}%</b>, {html.escape(hr_desc)})"
                )
            if alert_check.bad_aqi is not None:
                ba = alert_check.bad_aqi
                lines.append(
                    f"• {ba.level_icon} <b>Không khí {ba.level_label.lower()}</b> "
                    f"(US AQI <b>{ba.us_aqi}</b>) — nhớ đeo khẩu trang chống bụi mịn!"
                )

        # Compact next 2 days outlook
        if report.next_days:
            lines.append("")
            lines.append("📆 <b>2 ngày tới:</b>")
            for day in report.next_days[:2]:
                lines.append(cls._format_outlook_line(day))

        lines.append("")
        lines.append("<i>Nguồn: Open-Meteo (miễn phí)</i>")
        return cls._truncate_message(lines)

    @staticmethod
    def _format_outlook_line(day: DailyForecast) -> str:
        icon, desc = describe_weather_code(day.weather_code)
        prob = f"{day.precipitation_prob_max}%" if day.precipitation_prob_max is not None else "—"
        return (
            f"• <b>{_format_day_short(day.date_iso)}</b>: {icon} {html.escape(desc)} • "
            f"{_fmt_temp(day.temp_min)}–{_fmt_temp(day.temp_max)} • 🌧️ {prob}"
        )

    @classmethod
    def _format_alert_message(
        cls,
        report: WeatherReport,
        *,
        heavy_rain: HeavyRainAlertInfo | None,
        bad_aqi: BadAqiAlertInfo | None,
        rain_lookahead_hours: int,
    ) -> str:
        loc_label = html.escape(report.location.display_name)
        if heavy_rain is not None and bad_aqi is not None:
            title = f"🚨 <b>CẢNH BÁO MƯA LỚN & AQI XẤU — {loc_label}</b>"
        elif heavy_rain is not None:
            title = f"⛈️ <b>CẢNH BÁO SẮP CÓ MƯA LỚN — {loc_label}</b>"
        else:
            title = f"😷 <b>CẢNH BÁO CHẤT LƯỢNG KHÔNG KHÍ XẤU — {loc_label}</b>"

        lines = [title, ""]

        if heavy_rain is not None:
            hr_icon, hr_desc = describe_weather_code(heavy_rain.weather_code)
            lines.append(f"🌧️ <b>Mưa lớn dự kiến trong {rain_lookahead_hours} giờ tới:</b>")
            lines.append(
                f"• Đỉnh mưa: lúc <b>{html.escape(heavy_rain.peak_hour_label)}</b> "
                f"({hr_icon} {html.escape(hr_desc)})"
            )
            lines.append(
                f"• Lượng mưa dự kiến: <b>{heavy_rain.max_precipitation_mm:.1f} mm/h</b> • "
                f"Xác suất: <b>{heavy_rain.max_probability}%</b>"
            )
            if len(heavy_rain.rain_hours) > 1:
                slot_summary = ", ".join(
                    f"{h.hour_label} ({(h.precipitation or 0.0):.1f} mm)"
                    for h in heavy_rain.rain_hours[:4]
                )
                lines.append(f"• Chi tiết theo giờ: {html.escape(slot_summary)}")
            lines.append("• ☔ <i>Hãy mang áo mưa/ô, cất đồ ngoài trời và cẩn thận đường ngập trơn trượt!</i>")
            if bad_aqi is not None:
                lines.append("")

        if bad_aqi is not None:
            lines.append(
                f"{bad_aqi.level_icon} <b>Chỉ số không khí US AQI: {bad_aqi.us_aqi} ({bad_aqi.level_label})</b>"
            )
            pollutants: list[str] = []
            if bad_aqi.pm2_5 is not None:
                pollutants.append(f"PM2.5: <b>{bad_aqi.pm2_5:.1f} µg/m³</b>")
            if bad_aqi.pm10 is not None:
                pollutants.append(f"PM10: <b>{bad_aqi.pm10:.1f} µg/m³</b>")
            if pollutants:
                lines.append("• " + " • ".join(pollutants))
            if bad_aqi.advice:
                lines.append(f"• 😷 <i>{html.escape(bad_aqi.advice)}</i>")

        lines.append("")
        lines.append("<i>Gõ /weather để xem chi tiết đầy đủ • Nguồn: Open-Meteo</i>")
        return cls._truncate_message(lines)

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
            output.append("\n<i>… (đã rút gọn do vượt giới hạn ký tự Telegram)</i>")
        return "\n".join(output).strip()
