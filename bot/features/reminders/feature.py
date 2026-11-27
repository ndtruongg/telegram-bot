"""Reminders and event countdown feature implementation."""

from __future__ import annotations

import html
import logging
import secrets
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.config import RemindersConfig
from bot.core.commands import CommandContext, CommandRouter
from bot.core.feature import BaseFeature
from bot.core.github_vars import GitHubVariablesClient
from bot.core.state import StateStore
from bot.core.telegram_client import TelegramClient
from bot.features.reminders.models import (
    REPEAT_LABELS_VI,
    ReminderItem,
    deserialize_reminders,
    format_countdown_badge,
    format_date_vi,
    serialize_reminders,
)
from bot.features.reminders.parser import parse_reminder_args

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_LEN = 4096
_MAX_GITHUB_VAR_BYTES = 45000
_SECTION_DIVIDER = "───────────────"
_WEEKDAY_SHORT_VI: tuple[str, ...] = ("T2", "T3", "T4", "T5", "T6", "T7", "CN")


class RemindersFeature(BaseFeature):
    """Manages personal reminders and event countdowns with scheduled alerts and slash commands."""

    _GITHUB_VAR_DATA = "REMINDERS_DATA"
    _STATE_KEY_SENT = "sent_notifications"
    _STATE_KEY_LOCAL_ITEMS = "local_reminders_json"

    def __init__(
        self,
        telegram: TelegramClient,
        config: RemindersConfig,
        state_store: StateStore | None = None,
        github_vars: GitHubVariablesClient | None = None,
        now_provider: Callable[[ZoneInfo], datetime] | None = None,
        suppress_morning_digest: bool = False,
    ) -> None:
        super().__init__(telegram)
        self._config = config
        self._state_store = state_store
        self._github_vars = github_vars
        self._now_provider = now_provider or (lambda tz: datetime.now(tz))
        self._suppress_morning_digest = suppress_morning_digest

        self._items: list[ReminderItem] = self._load_initial_items()
        self._sent_notifications: dict[str, str] = self._load_sent_notifications()

    @property
    def name(self) -> str:
        return "reminders"

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

    def _load_initial_items(self) -> list[ReminderItem]:
        if self._config.reminders_json:
            return deserialize_reminders(self._config.reminders_json)
        if self._state_store is not None:
            saved_raw = self._state_store.get(self.name, self._STATE_KEY_LOCAL_ITEMS)
            if isinstance(saved_raw, str) and saved_raw.strip():
                return deserialize_reminders(saved_raw)
        return []

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

    def get_reminders(self, *, refresh_from_github: bool = False) -> list[ReminderItem]:
        """Return active reminders, optionally fetching the latest list from GitHub Variables."""
        if refresh_from_github and self._github_vars is not None:
            raw_val = self._github_vars.get_variable(self._GITHUB_VAR_DATA)
            if raw_val is not None:
                self._items = deserialize_reminders(raw_val)
        return list(self._items)

    def _persist_reminders(self, items: list[ReminderItem]) -> tuple[bool, str]:
        """Save reminders to GitHub Variable (preferred) or local state store."""
        serialized = serialize_reminders(items)
        if len(serialized.encode("utf-8")) > _MAX_GITHUB_VAR_BYTES:
            return (
                False,
                "❌ Danh sách nhắc việc đã đạt giới hạn dung lượng lưu trữ. "
                "Vui lòng xoá bớt mục cũ bằng <code>/remind_del</code>.",
            )

        if self._github_vars is not None:
            if not self._github_vars.set_variable(self._GITHUB_VAR_DATA, serialized):
                return (
                    False,
                    "❌ Không thể cập nhật GitHub Variable <code>REMINDERS_DATA</code>.\n"
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

        self._items = list(items)
        if self._state_store is not None and self._github_vars is None:
            self._state_store.set(self.name, self._STATE_KEY_LOCAL_ITEMS, serialized)
        return True, sync_note

    def register_commands(self, router: CommandRouter) -> None:
        """Expose reminder and countdown Telegram slash commands."""
        router.register(
            command="remind_add",
            usage="/remind_add <ngày> [giờ] <nội dung> [#yearly|#monthly|#weekly|#daily]",
            description="Thêm nhắc việc hoặc sự kiện đếm ngược",
            handler=self._cmd_remind_add,
        )
        router.register(
            command="remind_list",
            description="Xem toàn bộ danh sách nhắc việc & sự kiện",
            handler=self._cmd_remind_list,
        )
        router.register(
            command="countdown",
            description="Xem đếm ngược các sự kiện sắp tới",
            handler=self._cmd_countdown,
        )
        router.register(
            command="remind_del",
            usage="/remind_del <ID | expired>",
            description="Xoá nhắc việc theo ID (hoặc 'expired' để dọn mục đã qua)",
            handler=self._cmd_remind_del,
        )
        router.register(
            command="remind_status",
            description="Xem cấu hình & trạng thái tính năng nhắc việc",
            handler=self._cmd_remind_status,
        )

    def execute(self) -> None:
        tz = self._get_timezone()
        now = self._now_provider(tz)
        today = now.date()
        today_str = today.isoformat()

        items = self.get_reminders(refresh_from_github=True)
        if not items:
            logger.info("Chưa có nhắc việc hoặc sự kiện nào được cấu hình.")
            return

        # Re-read shared sent notifications in case morning_briefing recorded them in the same run
        if self._state_store is not None:
            self._sent_notifications = self._load_sent_notifications()

        send_morning_now = (
            not self._suppress_morning_digest
            and (now.hour, now.minute) >= (self._config.send_hour, self._config.send_minute)
        )

        due_timed: list[tuple[ReminderItem, date, str]] = []
        upcoming_alerts: list[tuple[int, ReminderItem, date, str]] = []

        for item in items:
            occ_date = item.next_occurrence(today)
            days_left = (occ_date - today).days
            if days_left < 0:
                continue

            parsed_time = item.parsed_time()
            token = item.notification_token()

            # 1. Exact time alert on the event day (for timed reminders)
            if days_left == 0 and parsed_time is not None:
                if (now.hour, now.minute) >= parsed_time:
                    at_time_key = f"{item.id}:{occ_date.isoformat()}:{token}:at_time"
                    if at_time_key not in self._sent_notifications:
                        due_timed.append((item, occ_date, at_time_key))

            # 2. Morning countdown / daily reminder alerts
            if send_morning_now:
                lead_days = item.effective_lead_days(self._config.lead_days)
                if days_left in lead_days:
                    # Skip d0 morning heads-up if the timed event is already at/past due time
                    # or scheduled at/before the morning digest hour (handled by at_time alert)
                    if (
                        days_left == 0
                        and parsed_time is not None
                        and (
                            parsed_time <= (self._config.send_hour, self._config.send_minute)
                            or (now.hour, now.minute) >= parsed_time
                        )
                    ):
                        continue

                    day_key = f"{item.id}:{occ_date.isoformat()}:{token}:d{days_left}"
                    if day_key not in self._sent_notifications:
                        upcoming_alerts.append((days_left, item, occ_date, day_key))

        if not due_timed and not upcoming_alerts:
            logger.info("Không có nhắc việc hoặc mốc đếm ngược nào cần gửi lúc này.")
            return

        upcoming_alerts.sort(key=lambda entry: (entry[0], entry[1].event_time or "99:99", entry[1].id))
        due_timed.sort(key=lambda entry: (entry[0].event_time or "00:00", entry[0].id))

        message = self._format_alert_message(now, due_timed, upcoming_alerts)
        if not self.telegram.send_message(message, disable_web_page_preview=True):
            raise RuntimeError("Gửi thông báo nhắc việc qua Telegram thất bại.")

        for _, _, key in due_timed:
            self._sent_notifications[key] = today_str
        for _, _, _, key in upcoming_alerts:
            self._sent_notifications[key] = today_str
        self._save_sent_notifications(today)

        logger.info(
            "Đã gửi thông báo nhắc việc (%d đến giờ, %d đếm ngược).",
            len(due_timed),
            len(upcoming_alerts),
        )

    def _cmd_remind_add(self, ctx: CommandContext) -> str:
        now = self._now_provider(self._get_timezone())
        today = now.date()

        parsed, error = parse_reminder_args(ctx.args, today)
        if error is not None or parsed is None:
            return error or "⚠️ Cú pháp không hợp lệ."

        items = self.get_reminders(refresh_from_github=True)
        next_id = max((item.id for item in items), default=0) + 1
        new_item = ReminderItem(
            id=next_id,
            title=parsed.title,
            event_date=parsed.event_date,
            event_time=parsed.event_time,
            repeat=parsed.repeat,
            tag=secrets.token_hex(3),
        )
        items.append(new_item)

        ok, sync_msg = self._persist_reminders(items)
        if not ok:
            return sync_msg

        occ_date = new_item.next_occurrence(today)
        days_left = (occ_date - today).days
        time_part = f" lúc <b>{new_item.event_time}</b>" if new_item.event_time else ""
        repeat_label = REPEAT_LABELS_VI.get(new_item.repeat, "Một lần")
        lead_line = (
            "<i>Đã qua hạn (không tự nhắc)</i>"
            if days_left < 0
            else self._format_lead_schedule(new_item, self._config.lead_days)
        )

        lines = [
            f"✅ <b>Đã thêm nhắc việc #{new_item.id}</b>",
            f"<i>{format_countdown_badge(days_left)}</i>",
            "",
            _SECTION_DIVIDER,
            f"📌 <b>{html.escape(new_item.title)}</b>",
            f"• Thời điểm: <b>{format_date_vi(occ_date, include_weekday=True)}</b>{time_part}",
            f"• Lặp lại: <b>{repeat_label}</b>",
            f"• Mốc nhắc: {lead_line}",
            "",
            _SECTION_DIVIDER,
            f"<i>💾 {sync_msg}</i>",
            f"<i>Gõ /remind_list để xem danh sách • /remind_del {new_item.id} để xoá</i>",
        ]
        return "\n".join(lines)

    def _cmd_remind_list(self, _ctx: CommandContext) -> str:
        now = self._now_provider(self._get_timezone())
        today = now.date()
        items = self.get_reminders(refresh_from_github=True)
        today_label = format_date_vi(today, include_weekday=True)

        if not items:
            return "\n".join(
                [
                    "📋 <b>DANH SÁCH NHẮC VIỆC</b>",
                    f"<i>{today_label} • Chưa có mục nào</i>",
                    "",
                    _SECTION_DIVIDER,
                    "<i>Hiện chưa có nhắc việc hay sự kiện nào.</i>",
                    "",
                    "<b>Ví dụ thêm nhanh:</b>",
                    "• <code>/remind_add 25/12 Giáng sinh #yearly</code>",
                    "• <code>/remind_add ngaymai 09:30 Họp dự án</code>",
                    "• <code>/remind_add +3d Nộp báo cáo</code>",
                ]
            )

        upcoming: list[tuple[int, ReminderItem, date]] = []
        expired: list[tuple[int, ReminderItem, date]] = []
        for item in items:
            occ = item.next_occurrence(today)
            days_left = (occ - today).days
            if days_left >= 0:
                upcoming.append((days_left, item, occ))
            else:
                expired.append((days_left, item, occ))

        upcoming.sort(key=lambda e: (e[0], e[1].event_time or "99:99", e[1].id))
        expired.sort(key=lambda e: (-e[0], e[1].id))

        summary_parts: list[str] = [today_label]
        if upcoming:
            summary_parts.append(f"{len(upcoming)} sắp tới")
        if expired:
            summary_parts.append(f"{len(expired)} đã qua hạn")

        lines = [
            "📋 <b>DANH SÁCH NHẮC VIỆC</b>",
            f"<i>{' • '.join(summary_parts)}</i>",
        ]

        if upcoming:
            lines.extend(["", _SECTION_DIVIDER, f"📌 <b>Sắp tới ({len(upcoming)})</b>", ""])
            for idx, (days_left, item, occ) in enumerate(upcoming):
                lines.append(self._format_list_line(item, occ, days_left, today=today))
                if idx < len(upcoming) - 1:
                    lines.append("")

        if expired:
            lines.extend(["", _SECTION_DIVIDER, f"⌛ <b>Đã qua hạn ({len(expired)})</b>", ""])
            for days_left, item, occ in expired:
                lines.append(self._format_expired_line(item, occ, days_left, today=today))

        footer_hint = (
            "Gõ /remind_del &lt;ID&gt; để xoá • /remind_del expired để dọn mục cũ"
            if expired
            else "Gõ /remind_add để thêm • /remind_del &lt;ID&gt; để xoá"
        )
        lines.extend(["", _SECTION_DIVIDER, f"<i>{footer_hint}</i>"])
        return self._truncate_message(lines)

    def _cmd_countdown(self, _ctx: CommandContext) -> str:
        now = self._now_provider(self._get_timezone())
        today = now.date()
        items = self.get_reminders(refresh_from_github=True)

        upcoming: list[tuple[int, ReminderItem, date]] = []
        for item in items:
            occ = item.next_occurrence(today)
            days_left = (occ - today).days
            if days_left >= 0:
                upcoming.append((days_left, item, occ))

        if not upcoming:
            return (
                "⏳ <b>ĐẾM NGƯỢC SỰ KIỆN</b>\n\n"
                "Hiện chưa có sự kiện nào sắp diễn ra.\n"
                "Thêm sự kiện bằng lệnh:\n"
                "<code>/remind_add 25/12 Giáng sinh #yearly</code>"
            )

        upcoming.sort(key=lambda e: (e[0], e[1].event_time or "99:99", e[1].id))
        lines = [f"⏳ <b>ĐẾM NGƯỢC SỰ KIỆN — {format_date_vi(today)}</b>", ""]

        for days_left, item, occ in upcoming:
            badge = format_countdown_badge(days_left)
            time_str = f" • {item.event_time}" if item.event_time else ""
            repeat_str = (
                f" • 🔁 {REPEAT_LABELS_VI[item.repeat]}"
                if item.repeat != "none"
                else ""
            )
            lines.append(
                f"• <b>{html.escape(item.title)}</b> (<code>#{item.id}</code>)\n"
                f"  {badge} — {format_date_vi(occ, include_weekday=True)}{time_str}{repeat_str}"
            )

        return self._truncate_message(lines)

    def _cmd_remind_del(self, ctx: CommandContext) -> str:
        if not ctx.args:
            return (
                "⚠️ Vui lòng nhập ID nhắc việc cần xoá.\n"
                "Ví dụ:\n"
                "• <code>/remind_del 2</code>\n"
                "• <code>/remind_del 1 3</code> (xoá nhiều mục)\n"
                "• <code>/remind_del expired</code> (xoá toàn bộ mục đã qua hạn)"
            )

        now = self._now_provider(self._get_timezone())
        today = now.date()
        items = self.get_reminders(refresh_from_github=True)

        if ctx.args.strip().lower() in {"expired", "past", "cu", "daqua"}:
            removed = [item for item in items if item.days_until(today) < 0]
            if not removed:
                return "ℹ️ Không có nhắc việc nào đã qua hạn để xoá."
            remaining = [item for item in items if item.days_until(today) >= 0]
            ok, sync_msg = self._persist_reminders(remaining)
            if not ok:
                return sync_msg
            return (
                f"🗑️ <b>Đã dọn {len(removed)} nhắc việc đã qua hạn!</b>\n"
                f"Còn lại: <b>{len(remaining)}</b> mục.\n"
                f"💾 Lưu tại: {sync_msg}"
            )

        target_ids: set[int] = set()
        for tok in ctx.args.replace(",", " ").split():
            cleaned = tok.lstrip("#").strip()
            if not cleaned:
                continue
            try:
                target_ids.add(int(cleaned))
            except ValueError:
                return (
                    f"⚠️ ID <code>{html.escape(tok)}</code> không hợp lệ.\n"
                    "Vui lòng nhập số ID từ <code>/remind_list</code> (VD: <code>/remind_del 1</code>)."
                )

        if not target_ids:
            return "⚠️ Vui lòng nhập ít nhất một ID hợp lệ."

        removed_items = [item for item in items if item.id in target_ids]
        if not removed_items:
            id_text = ", ".join(f"#{i}" for i in sorted(target_ids))
            return f"❓ Không tìm thấy nhắc việc có ID <code>{id_text}</code>."

        remaining = [item for item in items if item.id not in target_ids]
        ok, sync_msg = self._persist_reminders(remaining)
        if not ok:
            return sync_msg

        removed_lines = "\n".join(
            f"• <code>#{item.id}</code> {html.escape(item.title)}" for item in removed_items
        )
        return (
            f"🗑️ <b>Đã xoá {len(removed_items)} nhắc việc:</b>\n"
            f"{removed_lines}\n\n"
            f"💾 Lưu tại: {sync_msg}"
        )

    def _cmd_remind_status(self, _ctx: CommandContext) -> str:
        now = self._now_provider(self._get_timezone())
        today = now.date()
        items = self.get_reminders(refresh_from_github=True)
        upcoming_count = sum(1 for item in items if item.days_until(today) >= 0)
        expired_count = len(items) - upcoming_count

        enabled_text = "Đang bật ✅" if self._config.enabled else "Đang tắt ⏸️"
        schedule_text = (
            f"{self._config.send_hour:02d}:{self._config.send_minute:02d} "
            f"({html.escape(self._config.timezone)})"
        )
        lead_text = ", ".join(
            "đúng ngày" if d == 0 else f"trước {d} ngày"
            for d in self._config.lead_days
        )
        source = (
            f"GitHub Variable (<code>{html.escape(self._github_vars.repository)}</code>)"
            if self._github_vars is not None
            else "Cấu hình cục bộ / biến môi trường"
        )

        return (
            f"🗓️ <b>TRẠNG THÁI NHẮC VIỆC & ĐẾM NGƯỢC</b>\n\n"
            f"Trạng thái: <b>{enabled_text}</b>\n"
            f"Giờ hiện tại của bot: <b>{now.strftime('%H:%M %d/%m/%Y')}</b>\n"
            f"Giờ nhắc buổi sáng: <b>{schedule_text}</b>\n"
            f"Mốc nhắc trước: <b>{lead_text}</b>\n"
            f"Tổng số mục: <b>{len(items)}</b> "
            f"(<b>{upcoming_count}</b> sắp tới, <b>{expired_count}</b> đã qua)\n"
            f"Nguồn đồng bộ: {source}"
        )

    @staticmethod
    def _format_short_date(target_date: date, today: date | None = None) -> str:
        weekday = _WEEKDAY_SHORT_VI[target_date.weekday()]
        if today is not None and target_date.year == today.year:
            date_part = target_date.strftime("%d/%m")
        else:
            date_part = target_date.strftime("%d/%m/%Y")
        return f"{weekday}, {date_part}"

    @staticmethod
    def _format_lead_schedule(item: ReminderItem, default_lead_days: tuple[int, ...]) -> str:
        effective = sorted(item.effective_lead_days(default_lead_days), reverse=True)
        advance_days = [d for d in effective if d > 0]
        if advance_days:
            days_csv = ", ".join(str(d) for d in advance_days)
            base = f"Trước <b>{days_csv}</b> ngày &amp; <b>đúng ngày</b>"
        else:
            base = "<b>Đúng ngày</b>"
        if item.event_time:
            return f"{base} • báo lúc <b>{item.event_time}</b>"
        return base

    @classmethod
    def _format_list_line(
        cls,
        item: ReminderItem,
        occ: date,
        days_left: int,
        *,
        today: date | None = None,
    ) -> str:
        if days_left == 0:
            icon, status_label = "🔥", "<b>Hôm nay</b>"
        elif days_left == 1:
            icon, status_label = "⏰", "<b>Ngày mai</b>"
        else:
            icon, status_label = "⏳", f"<b>Còn {days_left} ngày</b>"

        meta_parts = [status_label, cls._format_short_date(occ, today)]
        if item.event_time:
            meta_parts.append(f"<b>{item.event_time}</b>")
        if item.repeat != "none":
            meta_parts.append(f"🔁 {REPEAT_LABELS_VI[item.repeat]}")

        return (
            f"{icon} <b>{html.escape(item.title)}</b> (<code>#{item.id}</code>)\n"
            f"• {' • '.join(meta_parts)}"
        )

    @classmethod
    def _format_expired_line(
        cls,
        item: ReminderItem,
        occ: date,
        days_left: int,
        *,
        today: date | None = None,
    ) -> str:
        date_str = cls._format_short_date(occ, today)
        time_str = f" {item.event_time}" if item.event_time else ""
        return (
            f"• <b>{html.escape(item.title)}</b> (<code>#{item.id}</code>) — "
            f"<i>Đã qua {abs(days_left)} ngày ({date_str}{time_str})</i>"
        )

    @staticmethod
    def _format_alert_message(
        now: datetime,
        due_timed: list[tuple[ReminderItem, date, str]],
        upcoming_alerts: list[tuple[int, ReminderItem, date, str]],
    ) -> str:
        lines = [f"🔔 <b>NHẮC VIỆC & ĐẾM NGƯỢC — {format_date_vi(now.date())}</b>", ""]

        if due_timed:
            lines.append("⏰ <b>ĐẾN GIỜ NHẮC VIỆC:</b>")
            for item, occ, _ in due_timed:
                repeat_str = (
                    f" (🔁 {REPEAT_LABELS_VI[item.repeat]})"
                    if item.repeat != "none"
                    else ""
                )
                lines.append(
                    f"• <b>{item.event_time}</b> — <b>{html.escape(item.title)}</b>"
                    f"{repeat_str} (<code>#{item.id}</code>)"
                )
            lines.append("")

        if upcoming_alerts:
            lines.append("📅 <b>SỰ KIỆN SẮP TỚI:</b>")
            for days_left, item, occ, _ in upcoming_alerts:
                badge = format_countdown_badge(days_left)
                time_str = f" lúc {item.event_time}" if item.event_time else ""
                repeat_str = (
                    f" • 🔁 {REPEAT_LABELS_VI[item.repeat]}"
                    if item.repeat != "none"
                    else ""
                )
                lines.append(
                    f"• <b>{html.escape(item.title)}</b> (<code>#{item.id}</code>)\n"
                    f"  {badge} — {format_date_vi(occ, include_weekday=True)}{time_str}{repeat_str}"
                )

        return RemindersFeature._truncate_message(lines)

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
            while output and (not output[-1] or output[-1] == _SECTION_DIVIDER):
                output.pop()
            output.append("")
            output.append("<i>… (đã rút gọn do vượt giới hạn ký tự Telegram)</i>")
        return "\n".join(output).strip()
