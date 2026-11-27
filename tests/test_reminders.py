"""Unit tests for reminders & event countdown feature, parser, models, and config."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from bot.config import RemindersConfig, Settings
from bot.core.commands import CommandRouter
from bot.core.state import StateStore
from bot.core.webhook import build_command_router
from bot.features.reminders.feature import RemindersFeature
from bot.features.reminders.models import (
    ReminderItem,
    deserialize_reminders,
    format_countdown_badge,
    format_date_vi,
    serialize_reminders,
)
from bot.features.reminders.parser import (
    parse_date_token,
    parse_lead_days,
    parse_reminder_args,
    parse_time_token,
)


class TestReminderParser(unittest.TestCase):
    def setUp(self) -> None:
        self.today = date(2026, 11, 27)

    def test_parse_date_token_formats(self) -> None:
        self.assertEqual(parse_date_token("2026-12-25", self.today), date(2026, 12, 25))
        self.assertEqual(parse_date_token("25/12/2026", self.today), date(2026, 12, 25))
        self.assertEqual(parse_date_token("25-12-2026", self.today), date(2026, 12, 25))
        self.assertEqual(parse_date_token("25/12", self.today), date(2026, 12, 25))
        # Past DD/MM in current year rolls over to next year
        self.assertEqual(parse_date_token("15/05", self.today), date(2027, 5, 15))
        # Leap day rolls over to next leap year (2028)
        self.assertEqual(parse_date_token("29/02", self.today), date(2028, 2, 29))
        # Relative shortcuts
        self.assertEqual(parse_date_token("homnay", self.today), date(2026, 11, 27))
        self.assertEqual(parse_date_token("ngaymai", self.today), date(2026, 11, 28))
        self.assertEqual(parse_date_token("mai", self.today), date(2026, 11, 28))
        self.assertEqual(parse_date_token("+3d", self.today), date(2026, 11, 30))
        self.assertEqual(parse_date_token("+2w", self.today), date(2026, 12, 11))
        # Invalid
        self.assertIsNone(parse_date_token("31/02/2026", self.today))
        self.assertIsNone(parse_date_token("abc", self.today))
        self.assertIsNone(parse_date_token("+0d", self.today))

    def test_parse_time_token_formats(self) -> None:
        self.assertEqual(parse_time_token("09:30"), "09:30")
        self.assertEqual(parse_time_token("9:05"), "09:05")
        self.assertEqual(parse_time_token("14h30"), "14:30")
        self.assertEqual(parse_time_token("8H"), "08:00")
        self.assertIsNone(parse_time_token("25:00"))
        self.assertIsNone(parse_time_token("12:60"))
        self.assertIsNone(parse_time_token("abc"))

    def test_parse_reminder_args_full_and_errors(self) -> None:
        parsed, err = parse_reminder_args("25/12 09:30 Giáng sinh an lành #yearly", self.today)
        self.assertIsNone(err)
        assert parsed is not None
        self.assertEqual(parsed.event_date, date(2026, 12, 25))
        self.assertEqual(parsed.event_time, "09:30")
        self.assertEqual(parsed.title, "Giáng sinh an lành")
        self.assertEqual(parsed.repeat, "yearly")

        # Vietnamese repeat hashtag + no time
        parsed2, err2 = parse_reminder_args("10/12 Đóng tiền điện #hangthang", self.today)
        self.assertIsNone(err2)
        assert parsed2 is not None
        self.assertEqual(parsed2.event_date, date(2026, 12, 10))
        self.assertIsNone(parsed2.event_time)
        self.assertEqual(parsed2.title, "Đóng tiền điện")
        self.assertEqual(parsed2.repeat, "monthly")

        # Invalid date escapes HTML characters
        parsed_bad, err_bad = parse_reminder_args("<bad> Test", self.today)
        self.assertIsNone(parsed_bad)
        assert err_bad is not None
        self.assertIn("&lt;bad&gt;", err_bad)

        # Missing args
        parsed_empty, err_empty = parse_reminder_args("25/12", self.today)
        self.assertIsNone(parsed_empty)
        self.assertIsNotNone(err_empty)

    def test_parse_lead_days(self) -> None:
        self.assertEqual(parse_lead_days("7, 3, 1"), (7, 3, 1, 0))
        self.assertEqual(parse_lead_days(""), (7, 3, 1, 0))
        self.assertEqual(parse_lead_days("invalid"), (7, 3, 1, 0))


class TestReminderModels(unittest.TestCase):
    def test_recurrence_calculations(self) -> None:
        today = date(2026, 11, 27)

        # Non-repeating past item keeps past date (negative days_until)
        past_once = ReminderItem(id=1, title="Old", event_date=date(2026, 11, 20))
        self.assertEqual(past_once.next_occurrence(today), date(2026, 11, 20))
        self.assertEqual(past_once.days_until(today), -7)

        # Daily advances to today
        daily = ReminderItem(id=2, title="Vitamin", event_date=date(2026, 1, 1), repeat="daily")
        self.assertEqual(daily.next_occurrence(today), today)
        self.assertEqual(daily.effective_lead_days((7, 3, 1, 0)), (0,))

        # Weekly advances by 7-day multiples
        weekly = ReminderItem(id=3, title="Sync", event_date=date(2026, 11, 16), repeat="weekly")
        # 16 -> 23 -> 30
        self.assertEqual(weekly.next_occurrence(today), date(2026, 11, 30))
        self.assertEqual(weekly.effective_lead_days((7, 3, 1, 0)), (0, 1))

        # Monthly clamps month-end properly (Jan 31 -> Nov 30, Feb 28 in non-leap year)
        monthly = ReminderItem(id=4, title="Bill", event_date=date(2026, 1, 31), repeat="monthly")
        self.assertEqual(monthly.next_occurrence(date(2026, 2, 1)), date(2026, 2, 28))
        self.assertEqual(monthly.next_occurrence(date(2026, 3, 1)), date(2026, 3, 31))
        self.assertEqual(monthly.next_occurrence(date(2026, 12, 31)), date(2026, 12, 31))

        # Yearly advances to next year if passed, clamps Feb 29 on non-leap year
        yearly_bday = ReminderItem(id=5, title="Birthday", event_date=date(2020, 5, 10), repeat="yearly")
        self.assertEqual(yearly_bday.next_occurrence(today), date(2027, 5, 10))
        yearly_leap = ReminderItem(id=6, title="Leap", event_date=date(2024, 2, 29), repeat="yearly")
        self.assertEqual(yearly_leap.next_occurrence(date(2026, 1, 1)), date(2026, 2, 28))

    def test_serialization_roundtrip_and_resilience(self) -> None:
        items = [
            ReminderItem(id=1, title="Giáng sinh", event_date=date(2026, 12, 25), repeat="yearly"),
            ReminderItem(id=2, title="Họp", event_date=date(2026, 11, 28), event_time="09:30"),
        ]
        raw = serialize_reminders(items)
        restored = deserialize_reminders(raw)
        self.assertEqual(restored, items)

        # Invalid JSON and partial invalid items are skipped gracefully
        self.assertEqual(deserialize_reminders("{bad"), [])
        mixed = '[{"id":1,"title":"OK","date":"2026-12-01"},{"id":1,"title":"Dup","date":"2026-12-02"},{"id":"x"}]'
        self.assertEqual(len(deserialize_reminders(mixed)), 1)

    def test_formatting_helpers(self) -> None:
        self.assertEqual(format_date_vi(date(2026, 11, 27), include_weekday=True), "Thứ 6, 27/11/2026")
        self.assertIn("Hôm nay", format_countdown_badge(0))
        self.assertIn("Ngày mai", format_countdown_badge(1))
        self.assertIn("Còn 5 ngày", format_countdown_badge(5))
        self.assertIn("Đã qua 2 ngày", format_countdown_badge(-2))


class TestRemindersFeature(unittest.TestCase):
    def _make_config(self, **overrides) -> RemindersConfig:
        defaults = dict(
            enabled=True,
            send_hour=7,
            send_minute=0,
            timezone="Asia/Ho_Chi_Minh",
            lead_days=(7, 3, 1, 0),
            check_interval=900,
            reminders_json="",
        )
        defaults.update(overrides)
        return RemindersConfig(**defaults)

    def _make_update(self, text: str, chat_id: str = "123") -> dict:
        return {"message": {"chat": {"id": int(chat_id)}, "text": text}}

    def test_scheduled_execution_lead_days_timed_and_deduplication(self) -> None:
        tz = ZoneInfo("Asia/Ho_Chi_Minh")
        items = [
            ReminderItem(id=1, title="Deadline báo cáo", event_date=date(2026, 11, 27), event_time="15:00"),
            ReminderItem(id=2, title="Đóng tiền nhà", event_date=date(2026, 11, 28)),  # 1 day left
            ReminderItem(id=3, title="Kỷ niệm <công ty>", event_date=date(2026, 11, 30)),  # 3 days left
            ReminderItem(id=4, title="Sự kiện xa", event_date=date(2026, 12, 20)),  # 23 days -> skip
            ReminderItem(id=5, title="Việc đã qua", event_date=date(2026, 11, 20)),  # past -> skip
        ]
        raw_json = serialize_reminders(items)

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"
            telegram = MagicMock()
            telegram.send_message.return_value = True

            # 06:30 -> before 07:00 morning send hour & before 15:00 timed event -> no alert
            RemindersFeature(
                telegram=telegram,
                config=self._make_config(reminders_json=raw_json),
                state_store=StateStore(state_path),
                now_provider=lambda _tz: datetime(2026, 11, 27, 6, 30, tzinfo=tz),
            ).execute()
            telegram.send_message.assert_not_called()

            # 07:05 -> sends morning alerts for #1 (d0 heads-up), #2 (d1), #3 (d3)
            RemindersFeature(
                telegram=telegram,
                config=self._make_config(reminders_json=raw_json),
                state_store=StateStore(state_path),
                now_provider=lambda _tz: datetime(2026, 11, 27, 7, 5, tzinfo=tz),
            ).execute()
            self.assertEqual(telegram.send_message.call_count, 1)
            msg1 = telegram.send_message.call_args.args[0]
            self.assertIn("Deadline báo cáo", msg1)
            self.assertIn("Đóng tiền nhà", msg1)
            self.assertIn("Kỷ niệm &lt;công ty&gt;", msg1)
            self.assertNotIn("Sự kiện xa", msg1)
            self.assertNotIn("Việc đã qua", msg1)

            # 08:17 -> already sent morning alerts, not yet 15:00 -> no alert
            RemindersFeature(
                telegram=telegram,
                config=self._make_config(reminders_json=raw_json),
                state_store=StateStore(state_path),
                now_provider=lambda _tz: datetime(2026, 11, 27, 8, 17, tzinfo=tz),
            ).execute()
            self.assertEqual(telegram.send_message.call_count, 1)

            # 15:17 -> triggers exact-time alert for #1 (15:00)
            RemindersFeature(
                telegram=telegram,
                config=self._make_config(reminders_json=raw_json),
                state_store=StateStore(state_path),
                now_provider=lambda _tz: datetime(2026, 11, 27, 15, 17, tzinfo=tz),
            ).execute()
            self.assertEqual(telegram.send_message.call_count, 2)
            msg2 = telegram.send_message.call_args.args[0]
            self.assertIn("ĐẾN GIỜ NHẮC VIỆC", msg2)
            self.assertIn("15:00", msg2)
            self.assertIn("Deadline báo cáo", msg2)

            # 16:17 -> already sent at_time alert -> no duplicate
            RemindersFeature(
                telegram=telegram,
                config=self._make_config(reminders_json=raw_json),
                state_store=StateStore(state_path),
                now_provider=lambda _tz: datetime(2026, 11, 27, 16, 17, tzinfo=tz),
            ).execute()
            self.assertEqual(telegram.send_message.call_count, 2)

    def test_early_morning_timed_reminder_does_not_double_notify(self) -> None:
        tz = ZoneInfo("Asia/Ho_Chi_Minh")
        items = [ReminderItem(id=1, title="Uống thuốc", event_date=date(2026, 11, 27), event_time="07:00")]
        telegram = MagicMock()
        telegram.send_message.return_value = True

        feature = RemindersFeature(
            telegram=telegram,
            config=self._make_config(reminders_json=serialize_reminders(items)),
            now_provider=lambda _tz: datetime(2026, 11, 27, 7, 0, tzinfo=tz),
        )
        feature.execute()
        self.assertEqual(telegram.send_message.call_count, 1)
        msg = telegram.send_message.call_args.args[0]
        self.assertIn("ĐẾN GIỜ NHẮC VIỆC", msg)
        self.assertNotIn("SỰ KIỆN SẮP TỚI", msg)

    def test_reused_id_after_delete_still_fires_on_same_day(self) -> None:
        tz = ZoneInfo("Asia/Ho_Chi_Minh")
        telegram = MagicMock()
        telegram.send_message.return_value = True

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"

            # First reminder #2 at 07:30 fires at 07:30
            first_item = ReminderItem(
                id=2, title="Test 7h30", event_date=date(2026, 11, 27), event_time="07:30", tag="aaa111"
            )
            RemindersFeature(
                telegram=telegram,
                config=self._make_config(reminders_json=serialize_reminders([first_item])),
                state_store=StateStore(state_path),
                now_provider=lambda _tz: datetime(2026, 11, 27, 7, 30, tzinfo=tz),
            ).execute()
            self.assertEqual(telegram.send_message.call_count, 1)

            # User deletes #2 and adds a new #2 at 08:30 on the same day -> must still fire at 08:30
            second_item = ReminderItem(
                id=2, title="Chấm công đi", event_date=date(2026, 11, 27), event_time="08:30", tag="bbb222"
            )
            RemindersFeature(
                telegram=telegram,
                config=self._make_config(reminders_json=serialize_reminders([second_item])),
                state_store=StateStore(state_path),
                now_provider=lambda _tz: datetime(2026, 11, 27, 8, 30, tzinfo=tz),
            ).execute()
            self.assertEqual(telegram.send_message.call_count, 2)
            msg = telegram.send_message.call_args.args[0]
            self.assertIn("ĐẾN GIỜ NHẮC VIỆC", msg)
            self.assertIn("Chấm công đi", msg)
            self.assertNotIn("SỰ KIỆN SẮP TỚI", msg)

    def test_commands_add_list_countdown_delete_status_with_github_vars(self) -> None:
        tz = ZoneInfo("Asia/Ho_Chi_Minh")
        telegram = MagicMock()

        stored: dict[str, str] = {}
        github_vars = MagicMock()
        github_vars.repository = "ndtruongg/telegram-bot"
        github_vars.get_variable.side_effect = lambda name: stored.get(name)

        def _set_var(name: str, val: str) -> bool:
            stored[name] = val
            return True

        github_vars.set_variable.side_effect = _set_var

        feature = RemindersFeature(
            telegram=telegram,
            config=self._make_config(),
            github_vars=github_vars,
            now_provider=lambda _tz: datetime(2026, 11, 27, 9, 0, tzinfo=tz),
        )
        router = CommandRouter()
        feature.register_commands(router)

        # /remind_add #1 (future yearly event)
        res_add1 = router.handle_update(
            self._make_update("/remind_add 25/12 Giáng sinh #yearly"),
            allowed_chat_id="123",
        )
        assert res_add1 is not None
        self.assertIn("Đã thêm nhắc việc #1", res_add1[1])
        self.assertIn("Còn 28 ngày", res_add1[1])
        self.assertIn("Hằng năm", res_add1[1])
        self.assertIn("Trước <b>7, 3, 1</b> ngày &amp; <b>đúng ngày</b>", res_add1[1])
        self.assertIn("───────────────", res_add1[1])
        self.assertIn("REMINDERS_DATA", stored)

        # /remind_add #2 (timed tomorrow)
        res_add2 = router.handle_update(
            self._make_update("/remind_add ngaymai 14h30 Họp & Review"),
            allowed_chat_id="123",
        )
        assert res_add2 is not None
        self.assertIn("#2", res_add2[1])
        self.assertIn("14:30", res_add2[1])
        self.assertIn("Họp &amp; Review", res_add2[1])
        self.assertIn("báo lúc <b>14:30</b>", res_add2[1])

        # /remind_add #3 (past non-repeating event for testing expired cleanup)
        res_add3 = router.handle_update(
            self._make_update("/remind_add 2026-11-20 Việc cũ"),
            allowed_chat_id="123",
        )
        assert res_add3 is not None
        self.assertIn("#3", res_add3[1])
        self.assertIn("Đã qua hạn", res_add3[1])

        # /remind_list shows both upcoming and expired sections with clean formatting
        res_list = router.handle_update(self._make_update("/remind_list"), allowed_chat_id="123")
        assert res_list is not None
        self.assertIn("Sắp tới (2)", res_list[1])
        self.assertIn("Đã qua hạn (1)", res_list[1])
        self.assertIn("Giáng sinh", res_list[1])
        self.assertIn("Việc cũ", res_list[1])
        self.assertIn("───────────────", res_list[1])
        self.assertIn("/remind_del expired", res_list[1])

        # /countdown only shows upcoming events sorted by soonest first
        res_cd = router.handle_update(self._make_update("/countdown"), allowed_chat_id="123")
        assert res_cd is not None
        self.assertIn("ĐẾM NGƯỢC SỰ KIỆN", res_cd[1])
        self.assertNotIn("Việc cũ", res_cd[1])
        self.assertLess(res_cd[1].index("Họp &amp; Review"), res_cd[1].index("Giáng sinh"))

        # /remind_status summarizes counts and sync source
        res_status = router.handle_update(self._make_update("/remind_status"), allowed_chat_id="123")
        assert res_status is not None
        self.assertIn("<b>3</b>", res_status[1])
        self.assertIn("<b>2</b> sắp tới", res_status[1])
        self.assertIn("<b>1</b> đã qua", res_status[1])

        # /remind_del expired removes only past non-repeating item (#3)
        res_clean = router.handle_update(
            self._make_update("/remind_del expired"),
            allowed_chat_id="123",
        )
        assert res_clean is not None
        self.assertIn("Đã dọn 1 nhắc việc", res_clean[1])
        self.assertEqual([i.id for i in deserialize_reminders(stored["REMINDERS_DATA"])], [1, 2])

        # /remind_del 1 removes item #1
        res_del = router.handle_update(self._make_update("/remind_del 1"), allowed_chat_id="123")
        assert res_del is not None
        self.assertIn("Đã xoá 1 nhắc việc", res_del[1])
        self.assertEqual([i.id for i in deserialize_reminders(stored["REMINDERS_DATA"])], [2])

    def test_settings_and_command_router_integration(self) -> None:
        keys = [
            "TELEGRAM_TOKEN",
            "TELEGRAM_CHAT_ID",
            "REMINDERS_ENABLED",
            "REMINDERS_SEND_HOUR",
            "REMINDERS_SEND_MINUTE",
            "REMINDERS_TIMEZONE",
            "REMINDERS_LEAD_DAYS",
            "REMINDERS_DATA",
        ]
        backup = {k: os.environ.pop(k, None) for k in keys}
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                env_path = Path(tmpdir) / ".env"
                env_path.write_text(
                    "\n".join(
                        [
                            "TELEGRAM_TOKEN=tok",
                            "TELEGRAM_CHAT_ID=123",
                            "REMINDERS_ENABLED=true",
                            "REMINDERS_SEND_HOUR=8",
                            "REMINDERS_SEND_MINUTE=15",
                            "REMINDERS_LEAD_DAYS=10,3,1",
                            'REMINDERS_DATA=[{"id":1,"title":"Tet","date":"2027-02-06"}]',
                        ]
                    ),
                    encoding="utf-8",
                )
                settings = Settings.load(env_file=env_path)
                self.assertTrue(settings.reminders.enabled)
                self.assertEqual(settings.reminders.send_hour, 8)
                self.assertEqual(settings.reminders.send_minute, 15)
                self.assertEqual(settings.reminders.lead_days, (10, 3, 1, 0))
                self.assertIn("Tet", settings.reminders.reminders_json)

                router = build_command_router(settings=settings, telegram=MagicMock())
                cmd_names = {c["command"] for c in router.get_telegram_commands()}
                self.assertTrue(
                    {"remind_add", "remind_list", "countdown", "remind_del", "remind_status"}.issubset(
                        cmd_names
                    )
                )
        finally:
            for k, v in backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
