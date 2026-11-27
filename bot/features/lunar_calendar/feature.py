"""Lunar calendar, auspicious days, and Vietnamese holidays reminder feature."""

from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.config import LunarCalendarConfig
from bot.core.commands import CommandContext, CommandRouter
from bot.core.feature import BaseFeature
from bot.core.state import StateStore
from bot.core.telegram_client import TelegramClient
from bot.features.lunar_calendar.calendar import (
    DayAuspiciousInfo,
    VIETNAM_TZ_OFFSET,
    format_solar_date_vi,
    get_day_auspicious_info,
    lunar_to_solar,
)
from bot.features.lunar_calendar.events import (
    CalendarEvent,
    format_countdown_vi,
    get_events_for_date,
    get_upcoming_events,
    get_upcoming_good_days,
)

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_LEN = 4096

_DMY_RE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{4}))?$")
_YMD_RE = re.compile(r"^(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})$")
_OFFSET_DAY_RE = re.compile(r"^([+-]\d+)[dn]?$", re.IGNORECASE)
_LUNAR_MARKERS = {"am", "al", "amlich", "âm", "âl", "l", "lunar"}
_LEAP_MARKERS = {"nhuan", "nhuận", "leap"}


class LunarCalendarFeature(BaseFeature):
    """Automatically reminds upcoming Rằm, Mùng 1, and Vietnamese holidays, plus lunar/good-day lookup commands."""

    _STATE_KEY_SENT = "sent_notifications"

    def __init__(
        self,
        telegram: TelegramClient,
        config: LunarCalendarConfig,
        state_store: StateStore | None = None,
        now_provider: Callable[[ZoneInfo], datetime] | None = None,
    ) -> None:
        super().__init__(telegram)
        self._config = config
        self._state_store = state_store
        self._now_provider = now_provider or (lambda tz: datetime.now(tz))
        self._sent_notifications: dict[str, str] = self._load_sent_notifications()

    @property
    def name(self) -> str:
        return "lunar_calendar"

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

    @staticmethod
    def _tz_offset_hours(now: datetime) -> float:
        offset = now.utcoffset()
        if offset is None:
            return VIETNAM_TZ_OFFSET
        return offset.total_seconds() / 3600.0

    def _load_sent_notifications(self) -> dict[str, str]:
        if self._state_store is None:
            return {}
        saved = self._state_store.get(self.name, self._STATE_KEY_SENT)
        if isinstance(saved, dict):
            return {str(k): str(v) for k, v in saved.items()}
        return {}

    def _save_sent_notifications(self, today: date) -> None:
        cutoff = (today - timedelta(days=14)).isoformat()
        pruned = {
            key: sent_day
            for key, sent_day in self._sent_notifications.items()
            if sent_day >= cutoff
        }
        self._sent_notifications = pruned
        if self._state_store is not None:
            self._state_store.set(self.name, self._STATE_KEY_SENT, dict(pruned))

    def register_commands(self, router: CommandRouter) -> None:
        """Expose /lunar, /lunar_upcoming, /lunar_good, and /lunar_status commands."""
        router.register(
            command="lunar",
            usage="/lunar [DD/MM[/YYYY] [am]]",
            description="Xem lịch âm dương, ngày tốt xấu & giờ hoàng đạo (hỗ trợ đổi ngày)",
            handler=self._cmd_lunar,
        )
        router.register(
            command="lunar_upcoming",
            usage="/lunar_upcoming [số ngày]",
            description="Xem ngày Rằm, Mùng 1 và ngày lễ Việt Nam sắp tới",
            handler=self._cmd_lunar_upcoming,
        )
        router.register(
            command="lunar_good",
            usage="/lunar_good [số ngày]",
            description="Xem các ngày Hoàng Đạo (ngày tốt) sắp tới",
            handler=self._cmd_lunar_good,
        )
        router.register(
            command="lunar_status",
            description="Xem cấu hình & trạng thái nhắc lịch âm / ngày lễ",
            handler=self._cmd_lunar_status,
        )

    def execute(self) -> None:
        tz = self._get_timezone()
        now = self._now_provider(tz)
        today = now.date()
        today_str = today.isoformat()

        if (now.hour, now.minute) < (self._config.send_hour, self._config.send_minute):
            logger.info(
                "Chưa đến giờ nhắc lịch âm (%02d:%02d %s, hiện tại %02d:%02d), bỏ qua.",
                self._config.send_hour,
                self._config.send_minute,
                self._config.timezone,
                now.hour,
                now.minute,
            )
            return

        tz_offset = self._tz_offset_hours(now)
        max_lead = max(
            (*self._config.lead_days, *self._config.moon_lead_days, 0),
        )
        upcoming = get_upcoming_events(today, days_ahead=max_lead, time_zone=tz_offset)

        due_alerts: list[tuple[int, CalendarEvent, str]] = []
        for days_left, event in upcoming:
            allowed_leads = (
                self._config.moon_lead_days
                if event.category == "moon"
                else self._config.lead_days
            )
            if days_left not in allowed_leads:
                continue

            notif_key = f"{event.event_id}:{event.solar_date.isoformat()}:d{days_left}"
            if notif_key not in self._sent_notifications:
                due_alerts.append((days_left, event, notif_key))

        if not due_alerts:
            logger.info("Không có ngày Rằm, Mùng 1 hay ngày lễ nào cần nhắc hôm nay (%s).", today_str)
            return

        due_alerts.sort(key=lambda item: (item[0], 0 if item[1].is_public_holiday else 1, item[1].event_id))
        today_info = get_day_auspicious_info(today, time_zone=tz_offset)
        message = self._format_alert_message(today_info, due_alerts)

        if not self.telegram.send_message(message, disable_web_page_preview=True):
            raise RuntimeError("Gửi thông báo lịch âm / ngày lễ qua Telegram thất bại.")

        for _, _, key in due_alerts:
            self._sent_notifications[key] = today_str
        self._save_sent_notifications(today)

        logger.info(
            "Đã gửi thông báo lịch âm ngày %s (%d sự kiện).",
            today_str,
            len(due_alerts),
        )

    def _cmd_lunar(self, ctx: CommandContext) -> str:
        tz = self._get_timezone()
        now = self._now_provider(tz)
        today = now.date()
        tz_offset = self._tz_offset_hours(now)

        if not ctx.args:
            target = today
            converted_from_lunar = False
        else:
            target, converted_from_lunar, error = self._parse_lunar_query(
                ctx.args,
                today,
                time_zone=tz_offset,
            )
            if error is not None or target is None:
                return error or "⚠️ Ngày không hợp lệ."

        info = get_day_auspicious_info(target, time_zone=tz_offset)
        day_events = get_events_for_date(target, lunar_date=info.lunar, time_zone=tz_offset)

        # Find nearest upcoming event if inspecting today
        nearest_next: tuple[int, CalendarEvent] | None = None
        if target == today:
            future_events = [
                (d, ev)
                for d, ev in get_upcoming_events(today, days_ahead=45, time_zone=tz_offset)
                if d > 0
            ]
            if future_events:
                nearest_next = future_events[0]

        return self._format_day_detail_message(
            info,
            day_events,
            today=today,
            converted_from_lunar=converted_from_lunar,
            nearest_next=nearest_next,
        )

    def _cmd_lunar_upcoming(self, ctx: CommandContext) -> str:
        tz = self._get_timezone()
        now = self._now_provider(tz)
        today = now.date()
        tz_offset = self._tz_offset_hours(now)

        days_ahead = 45
        if ctx.args:
            try:
                days_ahead = int(ctx.args.strip())
            except ValueError:
                return (
                    "⚠️ Số ngày không hợp lệ.\n"
                    "Ví dụ: <code>/lunar_upcoming</code> (45 ngày) hoặc <code>/lunar_upcoming 90</code>."
                )
            if not (1 <= days_ahead <= 365):
                return "⚠️ Vui lòng nhập số ngày trong khoảng <b>1</b> đến <b>365</b>."

        events = get_upcoming_events(today, days_ahead=days_ahead, time_zone=tz_offset)
        if not events:
            return (
                f"🌙 <b>SỰ KIỆN & NGÀY LỄ ({days_ahead} NGÀY TỚI)</b>\n\n"
                "Không có ngày Rằm, Mùng 1 hoặc ngày lễ nào trong khoảng thời gian này."
            )

        lines = [
            f"🌙 <b>RẰM, MÙNG 1 & NGÀY LỄ ({days_ahead} NGÀY TỚI)</b>",
            f"<i>Tính từ {format_solar_date_vi(today, include_weekday=True)} • {len(events)} sự kiện</i>",
            "",
        ]
        for idx, (days_left, ev) in enumerate(events):
            badge = format_countdown_vi(days_left)
            badge_fmt = f"<b>{badge}</b>" if days_left == 0 else f"<i>{badge}</i>"
            pub_tag = " • 🏖️ <b>Nghỉ lễ</b>" if ev.is_public_holiday else ""
            lines.append(
                f"{ev.icon} <b>{html.escape(ev.name)}</b> — {badge_fmt}\n"
                f"• {format_solar_date_vi(ev.solar_date, include_weekday=True)} • "
                f"ÂL: <b>{ev.lunar_date.format_short()}</b>{pub_tag}"
            )
            if idx < len(events) - 1:
                lines.append("")

        lines.append("")
        lines.append("<i>Gõ /lunar DD/MM để xem chi tiết ngày tốt xấu</i>")
        return self._truncate_message(lines)

    def _cmd_lunar_good(self, ctx: CommandContext) -> str:
        tz = self._get_timezone()
        now = self._now_provider(tz)
        today = now.date()
        tz_offset = self._tz_offset_hours(now)

        days_ahead = 14
        if ctx.args:
            try:
                days_ahead = int(ctx.args.strip())
            except ValueError:
                return (
                    "⚠️ Số ngày không hợp lệ.\n"
                    "Ví dụ: <code>/lunar_good</code> (14 ngày) hoặc <code>/lunar_good 30</code>."
                )
            if not (1 <= days_ahead <= 60):
                return "⚠️ Vui lòng nhập số ngày trong khoảng <b>1</b> đến <b>60</b>."

        good_days = get_upcoming_good_days(today, days_ahead=days_ahead, time_zone=tz_offset)
        if not good_days:
            return (
                f"✨ <b>NGÀY HOÀNG ĐẠO ({days_ahead} NGÀY TỚI)</b>\n\n"
                "Không tìm thấy ngày Hoàng Đạo nào trong khoảng thời gian này."
            )

        lines = [
            f"✨ <b>NGÀY TỐT / HOÀNG ĐẠO ({days_ahead} NGÀY TỚI)</b>",
            f"<i>Tính từ {format_solar_date_vi(today, include_weekday=True)} • Tìm thấy {len(good_days)} ngày Hoàng Đạo</i>",
            "",
        ]
        for idx, (days_left, info) in enumerate(good_days):
            badge = format_countdown_vi(days_left)
            badge_fmt = f"<b>{badge}</b>" if days_left == 0 else f"<i>{badge}</i>"
            hours_text = ", ".join(info.auspicious_hours[:4]) + "…"
            lines.append(
                f"🌟 <b>{format_solar_date_vi(info.solar, include_weekday=True)}</b> — {badge_fmt}\n"
                f"• Âm lịch: <b>{info.lunar.format_short()}</b> • Ngày <b>{info.day_can_chi}</b>\n"
                f"• Sao <b>{info.star_name}</b> • Trực <b>{info.truc_name}</b> — <i>{html.escape(info.truc_note)}</i>\n"
                f"• Giờ tốt: {hours_text}"
            )
            if idx < len(good_days) - 1:
                lines.append("")

        lines.append("")
        lines.append("<i>Gõ /lunar DD/MM để xem đầy đủ 6 giờ Hoàng Đạo & chi tiết ngày</i>")
        return self._truncate_message(lines)

    def _cmd_lunar_status(self, _ctx: CommandContext) -> str:
        tz = self._get_timezone()
        now = self._now_provider(tz)
        today = now.date()
        tz_offset = self._tz_offset_hours(now)
        info = get_day_auspicious_info(today, time_zone=tz_offset)

        enabled_text = "Đang bật ✅" if self._config.enabled else "Đang tắt ⏸️"
        schedule_text = (
            f"{self._config.send_hour:02d}:{self._config.send_minute:02d} "
            f"({html.escape(self._config.timezone)})"
        )
        holiday_leads = ", ".join(
            "đúng ngày" if d == 0 else f"trước {d} ngày"
            for d in self._config.lead_days
        )
        moon_leads = ", ".join(
            "đúng ngày" if d == 0 else f"trước {d} ngày"
            for d in self._config.moon_lead_days
        )

        upcoming = get_upcoming_events(today, days_ahead=60, time_zone=tz_offset)
        if upcoming:
            next_days, next_ev = upcoming[0]
            next_text = (
                f"{next_ev.icon} <b>{html.escape(next_ev.name)}</b> — "
                f"<i>{format_countdown_vi(next_days)}</i> ({format_solar_date_vi(next_ev.solar_date)})"
            )
        else:
            next_text = "<i>Không có trong 60 ngày tới</i>"

        return (
            f"🌙 <b>TRẠNG THÁI LỊCH ÂM & NGÀY LỄ</b>\n\n"
            f"Trạng thái: <b>{enabled_text}</b>\n"
            f"Hôm nay: <b>{format_solar_date_vi(today, include_weekday=True)}</b>\n"
            f"Âm lịch: <b>{info.lunar.format_vi()}</b>\n\n"
            f"⏰ <b>Cấu hình nhắc nhở:</b>\n"
            f"• Giờ nhắc hằng ngày: <b>{schedule_text}</b>\n"
            f"• Nhắc ngày lễ: <b>{holiday_leads}</b>\n"
            f"• Nhắc Rằm / Mùng 1: <b>{moon_leads}</b>\n\n"
            f"🔔 <b>Sự kiện gần nhất:</b>\n"
            f"• {next_text}"
        )

    @staticmethod
    def _parse_lunar_query(
        raw_args: str,
        today: date,
        *,
        time_zone: float = VIETNAM_TZ_OFFSET,
    ) -> tuple[date | None, bool, str | None]:
        """Parse user argument for /lunar into (solar_date, converted_from_lunar, error_msg)."""
        tokens = raw_args.strip().split()
        if not tokens:
            return today, False, None

        is_lunar_input = False
        is_leap_input = False
        date_tokens: list[str] = []

        for tok in tokens:
            low = tok.lower()
            if low in _LUNAR_MARKERS:
                is_lunar_input = True
            elif low in _LEAP_MARKERS:
                is_lunar_input = True
                is_leap_input = True
            else:
                date_tokens.append(tok)

        if len(date_tokens) != 1:
            return (
                None,
                False,
                "⚠️ Cú pháp chưa đúng.\n"
                "Ví dụ:\n"
                "• <code>/lunar</code> — Xem lịch hôm nay\n"
                "• <code>/lunar 30/04/2026</code> — Đổi Dương → Âm\n"
                "• <code>/lunar 15/08 am</code> — Đổi Âm → Dương\n"
                "• <code>/lunar ngaymai</code> hoặc <code>/lunar +7d</code>",
            )

        date_tok = date_tokens[0].strip().lower()

        if not is_lunar_input:
            if date_tok in {"homnay", "today"}:
                return today, False, None
            if date_tok in {"ngaymai", "mai", "tomorrow"}:
                return today + timedelta(days=1), False, None
            if date_tok in {"homqua", "qua", "yesterday"}:
                return today - timedelta(days=1), False, None
            off_match = _OFFSET_DAY_RE.match(date_tok)
            if off_match:
                delta = int(off_match.group(1))
                if abs(delta) > 36500:
                    return None, False, "⚠️ Khoảng lệch ngày quá lớn."
                return today + timedelta(days=delta), False, None

        dmy = _DMY_RE.match(date_tok)
        ymd = _YMD_RE.match(date_tok)
        if dmy:
            day = int(dmy.group(1))
            month = int(dmy.group(2))
            year = int(dmy.group(3)) if dmy.group(3) else today.year
        elif ymd:
            year = int(ymd.group(1))
            month = int(ymd.group(2))
            day = int(ymd.group(3))
        else:
            return (
                None,
                False,
                f"⚠️ Không nhận diện được ngày <code>{html.escape(date_tokens[0])}</code>.\n"
                "Dùng định dạng <code>DD/MM</code>, <code>DD/MM/YYYY</code> hoặc thêm <code>am</code> để đổi lịch âm.",
            )

        if not (1900 <= year <= 2100):
            return None, False, "⚠️ Hiện chỉ hỗ trợ tra cứu lịch trong giai đoạn năm <b>1900 – 2100</b>."

        if is_lunar_input:
            converted = lunar_to_solar(day, month, year, is_leap=is_leap_input, time_zone=time_zone)
            if converted is None:
                leap_txt = " nhuận" if is_leap_input else ""
                return (
                    None,
                    True,
                    f"⚠️ Ngày âm lịch <b>{day:02d}/{month:02d}{leap_txt}/{year}</b> không tồn tại "
                    "(tháng thiếu chỉ có 29 ngày hoặc năm này không nhuận tháng đó).",
                )
            return converted, True, None

        try:
            return date(year, month, day), False, None
        except ValueError:
            return (
                None,
                False,
                f"⚠️ Ngày dương lịch <b>{day:02d}/{month:02d}/{year}</b> không hợp lệ.",
            )

    @staticmethod
    def _format_day_detail_message(
        info: DayAuspiciousInfo,
        day_events: list[CalendarEvent],
        *,
        today: date,
        converted_from_lunar: bool = False,
        nearest_next: tuple[int, CalendarEvent] | None = None,
    ) -> str:
        status_icon = "🟢" if info.is_hoang_dao else "🔴"
        day_type = "Ngày Hoàng Đạo (Ngày tốt)" if info.is_hoang_dao else "Ngày Hắc Đạo"
        diff = (info.solar - today).days
        diff_label = format_countdown_vi(diff) if diff != 0 else "Hôm nay"
        date_short = format_solar_date_vi(info.solar)
        title = (
            f"🔄 <b>ĐỔI LỊCH ÂM → DƯƠNG — {date_short}</b>"
            if converted_from_lunar
            else f"🌙 <b>LỊCH VẠN NIÊN — {date_short}</b>"
        )

        lines = [
            title,
            f"<i>{diff_label}</i>",
            "",
            "📅 <b>Lịch Âm Dương:</b>",
            f"• Dương lịch: <b>{format_solar_date_vi(info.solar, include_weekday=True)}</b>",
            f"• Âm lịch: <b>{info.lunar.format_vi()}</b> ({info.lunar.format_short(include_year=True)})",
            f"• Can Chi: Ngày <b>{info.day_can_chi}</b> • Tháng <b>{info.month_can_chi}</b> • Năm <b>{info.year_can_chi}</b>",
            f"• Tiết khí: <b>{info.solar_term}</b>",
            "",
            "🧭 <b>Ngày tốt xấu & Giờ Hoàng Đạo:</b>",
            f"• Đánh giá: {status_icon} <b>{day_type}</b> — Sao <b>{info.star_name}</b>",
            f"• Trực <b>{info.truc_name}</b> — <i>{html.escape(info.truc_note)}</i>",
            f"• Giờ Hoàng Đạo: {', '.join(info.auspicious_hours)}",
        ]

        if day_events:
            lines.append("")
            lines.append("🎉 <b>Sự kiện / Ngày lễ trong ngày:</b>")
            for ev in day_events:
                pub_tag = " • 🏖️ <b>Nghỉ lễ</b>" if ev.is_public_holiday else ""
                note_part = f" — <i>{html.escape(ev.note)}</i>" if ev.note else ""
                lines.append(f"• {ev.icon} <b>{html.escape(ev.name)}</b>{pub_tag}{note_part}")

        if nearest_next is not None:
            next_days, next_ev = nearest_next
            lines.append("")
            lines.append("🔔 <b>Sự kiện sắp tới:</b>")
            lines.append(
                f"• {next_ev.icon} <b>{html.escape(next_ev.name)}</b> — "
                f"<i>{format_countdown_vi(next_days)}</i> ({format_solar_date_vi(next_ev.solar_date)})"
            )

        lines.append("")
        lines.append("<i>Gõ /lunar_upcoming hoặc /lunar_good để xem lịch sắp tới</i>")
        return "\n".join(lines)

    @staticmethod
    def _format_alert_message(
        today_info: DayAuspiciousInfo,
        due_alerts: list[tuple[int, CalendarEvent, str]],
    ) -> str:
        today_events = [(d, ev) for d, ev, _ in due_alerts if d == 0]
        upcoming_events = [(d, ev) for d, ev, _ in due_alerts if d > 0]

        status_icon = "🟢" if today_info.is_hoang_dao else "🔴"
        status_label = (
            f"{status_icon} Hoàng Đạo ({today_info.star_name})"
            if today_info.is_hoang_dao
            else f"{status_icon} Hắc Đạo ({today_info.star_name})"
        )
        lines = [
            f"🌙 <b>NHẮC LỊCH ÂM & NGÀY LỄ — {format_solar_date_vi(today_info.solar)}</b>",
            f"<i>ÂL hôm nay: {today_info.lunar.format_vi()} • Ngày {today_info.day_can_chi} • {status_label}</i>",
            "",
        ]

        if today_events:
            lines.append("✨ <b>DIỄN RA HÔM NAY:</b>")
            for _, ev in today_events:
                pub_tag = " • 🏖️ <b>Nghỉ lễ</b>" if ev.is_public_holiday else ""
                note_part = f" — <i>{html.escape(ev.note)}</i>" if ev.note else ""
                lines.append(f"• {ev.icon} <b>{html.escape(ev.name)}</b>{pub_tag}{note_part}")
            lines.append("")

        if upcoming_events:
            lines.append("📅 <b>SẮP DIỄN RA:</b>")
            for idx, (days_left, ev) in enumerate(upcoming_events):
                badge = format_countdown_vi(days_left)
                pub_tag = " • 🏖️ <b>Nghỉ lễ</b>" if ev.is_public_holiday else ""
                lines.append(
                    f"{ev.icon} <b>{html.escape(ev.name)}</b> — <i>{badge}</i>\n"
                    f"• {format_solar_date_vi(ev.solar_date, include_weekday=True)} • "
                    f"ÂL: <b>{ev.lunar_date.format_short()}</b>{pub_tag}"
                )
                if idx < len(upcoming_events) - 1:
                    lines.append("")
            lines.append("")

        lines.append("⏰ <b>Giờ Hoàng Đạo hôm nay:</b>")
        lines.append(f"• {', '.join(today_info.auspicious_hours)}")
        lines.append("")
        lines.append("<i>Gõ /lunar để xem chi tiết đầy đủ</i>")
        return LunarCalendarFeature._truncate_message(lines)

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
