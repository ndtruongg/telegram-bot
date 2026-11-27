"""Daily English vocabulary feature with morning/evening schedule and history deduplication."""

from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.config import DailyVocabConfig
from bot.core.commands import CommandContext, CommandRouter
from bot.core.feature import BaseFeature
from bot.core.github_vars import GitHubVariablesClient
from bot.core.state import StateStore
from bot.core.telegram_client import TelegramClient
from bot.features.daily_vocab.generator import generate_vocab_with_gemini
from bot.features.daily_vocab.models import (
    CONCRETE_TOPICS,
    TOPIC_BADGES,
    TOPIC_LABELS_VI,
    VALID_TOPICS,
    VocabBatch,
    VocabHistoryEntry,
    deserialize_history,
    merge_history,
    normalize_topic,
    serialize_history,
)

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_LEN = 4096
_DEFAULT_HISTORY_DISPLAY = 15
_MAX_HISTORY_DISPLAY = 40


class DailyVocabFeature(BaseFeature):
    """Sends 3–5 daily English words/phrases via Gemini AI with history-based deduplication."""

    _GITHUB_VAR_HISTORY = "VOCAB_HISTORY"
    _GITHUB_VAR_TOPIC = "VOCAB_TOPIC"
    _STATE_KEY_LAST_SENT_SLOT = "last_sent_slot"
    _STATE_KEY_LOCAL_HISTORY = "history"
    _STATE_KEY_LOCAL_TOPIC = "topic"

    def __init__(
        self,
        telegram: TelegramClient,
        config: DailyVocabConfig,
        generator: Callable[[str, int, tuple[str, ...]], VocabBatch] | None = None,
        state_store: StateStore | None = None,
        github_vars: GitHubVariablesClient | None = None,
        now_provider: Callable[[ZoneInfo], datetime] | None = None,
    ) -> None:
        super().__init__(telegram)
        self._config = config
        self._generator = generator or (
            lambda topic, count, exclude_terms: generate_vocab_with_gemini(
                topic=topic,
                count=count,
                exclude_terms=exclude_terms,
                api_key=self._config.gemini_api_key,
                model=self._config.gemini_model,
            )
        )
        self._state_store = state_store
        self._github_vars = github_vars
        self._now_provider = now_provider or (lambda tz: datetime.now(tz))

        self._last_sent_slot: str | None = (
            state_store.get(self.name, self._STATE_KEY_LAST_SENT_SLOT)
            if state_store is not None
            else None
        )
        self._topic: str = self._load_initial_topic()
        self._history: list[VocabHistoryEntry] = self._load_initial_history()

    @property
    def name(self) -> str:
        return "daily_vocab"

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

    def _load_initial_topic(self) -> str:
        if self._state_store is not None:
            saved_topic = self._state_store.get(self.name, self._STATE_KEY_LOCAL_TOPIC)
            if isinstance(saved_topic, str):
                normalized = normalize_topic(saved_topic)
                if normalized is not None:
                    return normalized
        return normalize_topic(self._config.topic) or "mixed"

    def _load_initial_history(self) -> list[VocabHistoryEntry]:
        from_env = deserialize_history(self._config.history_json)
        from_state: list[VocabHistoryEntry] = []
        if self._state_store is not None:
            saved = self._state_store.get(self.name, self._STATE_KEY_LOCAL_HISTORY)
            from_state = deserialize_history(saved)
        return merge_history(from_env, from_state, max_entries=self._config.max_history)

    def _set_last_sent_slot(self, slot_key: str) -> None:
        self._last_sent_slot = slot_key
        if self._state_store is not None:
            self._state_store.set(self.name, self._STATE_KEY_LAST_SENT_SLOT, slot_key)

    def get_topic(self, *, refresh_from_github: bool = False) -> str:
        """Return active default topic, optionally refreshing from GitHub Variable VOCAB_TOPIC."""
        if refresh_from_github and self._github_vars is not None:
            raw_val = self._github_vars.get_variable(self._GITHUB_VAR_TOPIC)
            if raw_val is not None:
                normalized = normalize_topic(raw_val)
                if normalized is not None:
                    self._topic = normalized
        return self._topic

    def _persist_topic(self, topic: str) -> tuple[bool, str]:
        if self._github_vars is not None:
            if not self._github_vars.set_variable(self._GITHUB_VAR_TOPIC, topic):
                return (
                    False,
                    "❌ Không thể cập nhật GitHub Variable <code>VOCAB_TOPIC</code>.\n"
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

        self._topic = topic
        if self._state_store is not None and self._github_vars is None:
            self._state_store.set(self.name, self._STATE_KEY_LOCAL_TOPIC, topic)
        return True, sync_note

    def get_history(self, *, refresh_from_github: bool = False) -> list[VocabHistoryEntry]:
        """Return learned vocabulary history, optionally refreshing from GitHub Variable VOCAB_HISTORY."""
        if refresh_from_github and self._github_vars is not None:
            raw_val = self._github_vars.get_variable(self._GITHUB_VAR_HISTORY)
            if raw_val is not None:
                self._history = deserialize_history(raw_val)[-self._config.max_history :]
        return list(self._history)

    def _persist_history(self, entries: list[VocabHistoryEntry]) -> tuple[bool, str]:
        trimmed = entries[-self._config.max_history :]
        serialized = serialize_history(trimmed)

        if self._github_vars is not None:
            if not self._github_vars.set_variable(self._GITHUB_VAR_HISTORY, serialized):
                logger.warning("Không thể đồng bộ lịch sử từ vựng lên GitHub Variable VOCAB_HISTORY.")
                self._history = list(trimmed)
                return (
                    False,
                    "Không thể đồng bộ GitHub Variable <code>VOCAB_HISTORY</code>",
                )
            sync_note = f"GitHub Variable (<code>{html.escape(self._github_vars.repository)}</code>)"
        else:
            sync_note = (
                "File trạng thái cục bộ"
                if self._state_store is not None
                else "Bộ nhớ tạm"
            )

        self._history = list(trimmed)
        if self._state_store is not None and self._github_vars is None:
            compact_list = [entry.to_compact_dict() for entry in trimmed]
            self._state_store.set(self.name, self._STATE_KEY_LOCAL_HISTORY, compact_list)
        return True, sync_note

    def _resolve_concrete_topic(
        self,
        requested_topic: str,
        history: list[VocabHistoryEntry],
    ) -> str:
        """Resolve 'mixed' into the next topic in rotation (it -> communication -> ielts)."""
        if requested_topic in CONCRETE_TOPICS:
            return requested_topic

        # Rotate based on the last recorded concrete topic in history
        for entry in reversed(history):
            if entry.topic in CONCRETE_TOPICS:
                idx = CONCRETE_TOPICS.index(entry.topic)
                return CONCRETE_TOPICS[(idx + 1) % len(CONCRETE_TOPICS)]
        return CONCRETE_TOPICS[0]

    def register_commands(self, router: CommandRouter) -> None:
        """Expose /vocab_now, /vocab_topic, /vocab_history, /vocab_reset, /vocab_status commands."""
        router.register(
            command="vocab_now",
            usage="/vocab_now [it|communication|ielts|mixed] [số từ]",
            description="Nhận ngay 3–5 từ vựng tiếng Anh mới kèm ví dụ AI",
            handler=self._cmd_vocab_now,
        )
        router.register(
            command="vocab_topic",
            usage="/vocab_topic [it|communication|ielts|mixed]",
            description="Xem hoặc đổi chủ đề từ vựng mặc định",
            handler=self._cmd_vocab_topic,
        )
        router.register(
            command="vocab_history",
            usage="/vocab_history [số lượng]",
            description="Xem lại danh sách từ vựng đã học gần đây",
            handler=self._cmd_vocab_history,
        )
        router.register(
            command="vocab_reset",
            description="Xoá lịch sử từ vựng đã học để học lại từ đầu",
            handler=self._cmd_vocab_reset,
        )
        router.register(
            command="vocab_status",
            description="Xem lịch gửi từ vựng, chủ đề và thống kê lịch sử",
            handler=self._cmd_vocab_status,
        )

    def execute(self) -> None:
        tz = self._get_timezone()
        now = self._now_provider(tz)

        due_slots = [
            (hour, minute)
            for hour, minute in self._config.send_times
            if (now.hour, now.minute) >= (hour, minute)
        ]
        if not due_slots:
            next_h, next_m = self._config.send_times[0]
            logger.info(
                "Chưa đến khung giờ gửi từ vựng (%02d:%02d %s, hiện tại %02d:%02d), bỏ qua.",
                next_h,
                next_m,
                self._config.timezone,
                now.hour,
                now.minute,
            )
            return

        slot_h, slot_m = due_slots[-1]
        slot_key = f"{now.date().isoformat()}:{slot_h:02d}:{slot_m:02d}"

        if self._last_sent_slot is not None and self._last_sent_slot >= slot_key:
            logger.info("Từ vựng khung giờ %s đã được gửi trước đó, bỏ qua.", slot_key)
            return

        active_topic = self.get_topic(refresh_from_github=True)
        history = self.get_history(refresh_from_github=True)
        concrete_topic = self._resolve_concrete_topic(active_topic, history)

        exclude_terms = tuple(entry.term for entry in history)
        batch = self._generator(concrete_topic, self._config.words_per_batch, exclude_terms)
        if not batch.items:
            raise RuntimeError("Không thể tạo danh sách từ vựng tiếng Anh.")

        new_entries = [
            VocabHistoryEntry(
                term=item.term,
                topic=item.topic or batch.topic,
                meaning_vi=item.meaning_vi,
                learned_date=now.date().isoformat(),
            )
            for item in batch.items
        ]
        updated_history = merge_history(
            history,
            new_entries,
            max_entries=self._config.max_history,
        )

        message = self._format_vocab_message(
            batch=batch,
            now=now,
            total_learned=len(updated_history),
            slot_hour=slot_h,
        )
        if not self.telegram.send_message(message, disable_web_page_preview=True):
            raise RuntimeError("Gửi từ vựng tiếng Anh qua Telegram thất bại.")

        self._persist_history(updated_history)
        self._set_last_sent_slot(slot_key)
        logger.info(
            "Đã gửi %d từ vựng chủ đề '%s' (khung %s) thành công.",
            len(batch.items),
            batch.topic,
            slot_key,
        )

    def _parse_vocab_now_args(self, raw_args: str) -> tuple[str | None, int | None, str | None]:
        """Parse optional [topic] and [count] tokens in any order."""
        tokens = raw_args.strip().split()
        if not tokens:
            return None, None, None

        chosen_topic: str | None = None
        chosen_count: int | None = None

        for tok in tokens:
            if tok.isdigit():
                val = int(tok)
                if not (1 <= val <= 8):
                    return (
                        None,
                        None,
                        "⚠️ Số lượng từ phải nằm trong khoảng <b>1–8</b> (khuyến nghị 3–5).\n"
                        "Ví dụ: <code>/vocab_now it 4</code>",
                    )
                chosen_count = val
                continue

            norm_topic = normalize_topic(tok)
            if norm_topic is not None:
                chosen_topic = norm_topic
                continue

            return (
                None,
                None,
                f"⚠️ Tham số <code>{html.escape(tok)}</code> không hợp lệ.\n"
                "Chủ đề hỗ trợ: <code>it</code>, <code>communication</code> (hoặc <code>giaotiep</code>), "
                "<code>ielts</code>, <code>mixed</code>.\n"
                "Ví dụ: <code>/vocab_now it 4</code> hoặc <code>/vocab_now ielts</code>",
            )

        return chosen_topic, chosen_count, None

    def _cmd_vocab_now(self, ctx: CommandContext) -> str:
        req_topic, req_count, err = self._parse_vocab_now_args(ctx.args)
        if err is not None:
            return err

        now = self._now_provider(self._get_timezone())
        active_topic = req_topic or self.get_topic(refresh_from_github=True)
        history = self.get_history(refresh_from_github=True)
        concrete_topic = self._resolve_concrete_topic(active_topic, history)
        count = req_count if req_count is not None else self._config.words_per_batch

        try:
            batch = self._generator(
                concrete_topic,
                count,
                tuple(entry.term for entry in history),
            )
            if not batch.items:
                return "⚠️ Không thể tạo từ vựng lúc này. Vui lòng thử lại sau."
        except Exception:
            logger.exception("Không thể tạo từ vựng qua /vocab_now")
            return "⚠️ Có lỗi khi tạo từ vựng với Gemini AI. Vui lòng thử lại sau."

        new_entries = [
            VocabHistoryEntry(
                term=item.term,
                topic=item.topic or batch.topic,
                meaning_vi=item.meaning_vi,
                learned_date=now.date().isoformat(),
            )
            for item in batch.items
        ]
        updated_history = merge_history(
            history,
            new_entries,
            max_entries=self._config.max_history,
        )
        self._persist_history(updated_history)

        return self._format_vocab_message(
            batch=batch,
            now=now,
            total_learned=len(updated_history),
            slot_hour=now.hour,
        )

    def _cmd_vocab_topic(self, ctx: CommandContext) -> str:
        if not ctx.args.strip():
            current = self.get_topic(refresh_from_github=True)
            options = "\n".join(
                f"• <code>{code}</code> — {html.escape(TOPIC_LABELS_VI[code])}"
                for code in VALID_TOPICS
            )
            return (
                f"🏷️ <b>CHỦ ĐỀ TỪ VỰNG HIỆN TẠI</b>\n\n"
                f"Đang chọn: <b>{html.escape(TOPIC_LABELS_VI.get(current, current))}</b> "
                f"(<code>{html.escape(current)}</code>)\n\n"
                f"<b>Các chủ đề hỗ trợ:</b>\n{options}\n\n"
                f"Đổi chủ đề bằng lệnh: <code>/vocab_topic it</code>"
            )

        parsed = normalize_topic(ctx.args.strip())
        if parsed is None:
            return (
                f"⚠️ Chủ đề <code>{html.escape(ctx.args.strip())}</code> không hợp lệ.\n"
                "Các lựa chọn hợp lệ: <code>it</code>, <code>communication</code>, "
                "<code>ielts</code>, <code>mixed</code>."
            )

        ok, sync_msg = self._persist_topic(parsed)
        if not ok:
            return sync_msg

        return (
            f"✅ <b>Đã đổi chủ đề từ vựng mặc định!</b>\n\n"
            f"🏷️ Chủ đề mới: <b>{html.escape(TOPIC_LABELS_VI[parsed])}</b> "
            f"(<code>{html.escape(parsed)}</code>)\n"
            f"💾 Lưu tại: {sync_msg}"
        )

    def _cmd_vocab_history(self, ctx: CommandContext) -> str:
        limit = _DEFAULT_HISTORY_DISPLAY
        if ctx.args.strip():
            arg = ctx.args.strip()
            if not arg.isdigit():
                return "⚠️ Vui lòng nhập số lượng hợp lệ. Ví dụ: <code>/vocab_history 20</code>"
            limit = max(1, min(_MAX_HISTORY_DISPLAY, int(arg)))

        history = self.get_history(refresh_from_github=True)
        if not history:
            return (
                "📖 <b>LỊCH SỬ TỪ VỰNG ĐÃ HỌC</b>\n\n"
                "Chưa có từ vựng nào được lưu.\n"
                "Gõ <code>/vocab_now</code> để nhận ngay bộ từ đầu tiên!"
            )

        recent = list(reversed(history[-limit:]))
        lines = [
            f"📖 <b>LỊCH SỬ TỪ VỰNG ĐÃ HỌC ({len(history)} từ)</b>",
            f"<i>Hiển thị {len(recent)} từ gần nhất:</i>",
            "",
        ]
        for idx, entry in enumerate(recent, start=1):
            badge = TOPIC_BADGES.get(entry.topic, "📚")
            safe_term = html.escape(entry.term)
            meaning_part = f" — {html.escape(entry.meaning_vi)}" if entry.meaning_vi else ""
            date_part = f" <i>({html.escape(entry.learned_date)})</i>" if entry.learned_date else ""
            line = f"{idx}. {badge} <b>{safe_term}</b>{meaning_part}{date_part}"

            candidate = "\n".join(lines + [line])
            if len(candidate) > _TELEGRAM_MAX_LEN - 80:
                lines.append("<i>… (đã rút gọn do giới hạn ký tự Telegram)</i>")
                break
            lines.append(line)

        return "\n".join(lines)

    def _cmd_vocab_reset(self, _ctx: CommandContext) -> str:
        history = self.get_history(refresh_from_github=True)
        prev_count = len(history)
        if prev_count == 0:
            return "ℹ️ Lịch sử từ vựng hiện đang trống."

        ok, sync_msg = self._persist_history([])
        if not ok:
            return f"❌ Không thể xoá lịch sử từ vựng: {sync_msg}"

        return (
            f"🗑️ <b>Đã xoá toàn bộ lịch sử từ vựng ({prev_count} từ)!</b>\n"
            f"Bot sẽ bắt đầu ghi nhận lịch sử mới từ lần gửi tiếp theo.\n"
            f"💾 Lưu tại: {sync_msg}"
        )

    def _cmd_vocab_status(self, _ctx: CommandContext) -> str:
        active_topic = self.get_topic(refresh_from_github=True)
        history = self.get_history(refresh_from_github=True)

        enabled_text = "Đang bật ✅" if self._config.enabled else "Đang tắt ⏸️"
        times_text = ", ".join(f"{h:02d}:{m:02d}" for h, m in self._config.send_times)
        schedule_text = f"{times_text} ({html.escape(self._config.timezone)})"
        ai_text = (
            f"Gemini (<code>{html.escape(self._config.gemini_model)}</code>) ✅"
            if self._config.gemini_api_key
            else "Kho từ vựng dự phòng (chưa có <code>GEMINI_API_KEY</code>) ⚙️"
        )
        topic_label = TOPIC_LABELS_VI.get(active_topic, active_topic)
        last_sent = html.escape(self._last_sent_slot) if self._last_sent_slot else "Chưa ghi nhận"
        source = (
            f"GitHub Variable (<code>{html.escape(self._github_vars.repository)}</code>)"
            if self._github_vars is not None
            else "File trạng thái cục bộ / biến môi trường"
        )

        return (
            f"📚 <b>TRẠNG THÁI TỪ VỰNG TIẾNG ANH MỖI NGÀY</b>\n\n"
            f"Trạng thái: <b>{enabled_text}</b>\n"
            f"Khung giờ gửi: <b>{schedule_text}</b>\n"
            f"Chủ đề mặc định: <b>{html.escape(topic_label)}</b> "
            f"(<code>{html.escape(active_topic)}</code>)\n"
            f"Số từ mỗi buổi: <b>{self._config.words_per_batch}</b> từ/cụm từ\n"
            f"Tạo nội dung AI: {ai_text}\n"
            f"Lịch sử chống trùng: <b>{len(history)} / {self._config.max_history}</b> từ\n"
            f"Lần gửi gần nhất: <b>{last_sent}</b>\n"
            f"Nguồn đồng bộ: {source}"
        )

    @staticmethod
    def _session_heading(hour: int) -> tuple[str, str]:
        if hour < 12:
            return "🌅", "BUỔI SÁNG"
        if hour < 18:
            return "☀️", "BUỔI CHIỀU"
        return "🌙", "BUỔI TỐI"

    @classmethod
    def _format_vocab_message(
        cls,
        *,
        batch: VocabBatch,
        now: datetime,
        total_learned: int,
        slot_hour: int,
    ) -> str:
        icon, session_label = cls._session_heading(slot_hour)
        date_label = now.strftime("%d/%m/%Y")
        topic_label = TOPIC_LABELS_VI.get(batch.topic, batch.topic)

        lines = [
            f"{icon} <b>TỪ VỰNG TIẾNG ANH {session_label} — {date_label}</b>",
            f"🏷️ Chủ đề: <b>{html.escape(topic_label)}</b> • <b>{len(batch.items)}</b> từ/cụm từ",
            "",
        ]

        for idx, item in enumerate(batch.items, start=1):
            safe_term = html.escape(item.term)
            pos_part = f" <i>({html.escape(item.part_of_speech)})</i>" if item.part_of_speech else ""
            phonetic_part = f" — <code>{html.escape(item.phonetic)}</code>" if item.phonetic else ""

            block_lines = [f"<b>{idx}. {safe_term}</b>{pos_part}{phonetic_part}"]
            block_lines.append(f"• Nghĩa: {html.escape(item.meaning_vi)}")
            block_lines.append(f"• Ví dụ: <i>{html.escape(item.example_en)}</i>")
            if item.example_vi:
                block_lines.append(f"• Dịch: {html.escape(item.example_vi)}")
            if item.usage_note:
                block_lines.append(f"• Mẹo dùng: {html.escape(item.usage_note)}")

            block = "\n".join(block_lines)
            candidate = "\n".join(lines + [block, ""])
            if len(candidate) > _TELEGRAM_MAX_LEN - 160:
                break
            lines.append(block)
            lines.append("")

        if batch.used_ai:
            lines.append(
                f"🤖 <i>Tạo bởi Gemini AI • Lịch sử đã lưu: {total_learned} từ (không trùng lặp)</i>"
            )
        else:
            note = (
                f" ({html.escape(batch.fallback_reason)})"
                if batch.fallback_reason
                else ""
            )
            lines.append(
                f"📘 <i>Kho từ vựng dự phòng{note} • Lịch sử đã lưu: {total_learned} từ</i>"
            )

        return "\n".join(lines).strip()
