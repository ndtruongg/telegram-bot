"""Unit tests for the lunar_calendar feature (Vietnamese lunar algorithm, events, and commands)."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from bot.config import GoldPriceConfig, LunarCalendarConfig, Settings
from bot.core.commands import CommandRouter
from bot.core.state import StateStore
from bot.core.webhook import build_command_router
from bot.features.lunar_calendar.calendar import (
    get_auspicious_hours,
    get_day_auspicious_info,
    get_day_can_chi,
    get_year_can_chi,
    lunar_to_solar,
    solar_to_lunar,
)
from bot.features.lunar_calendar.events import (
    get_events_for_date,
    get_upcoming_events,
    get_upcoming_good_days,
)
from bot.features.lunar_calendar.feature import LunarCalendarFeature

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


class TestVietnameseLunarCalendar(unittest.TestCase):
    def test_known_tet_and_holiday_conversions(self) -> None:
        """Verify known Vietnamese Tết Nguyên Đán & holiday dates (UTC+7)."""
        cases = [
            # (solar_date, expected_lunar_day, month, year, is_leap)
            (date(2020, 1, 25), 1, 1, 2020, False),  # Tết Canh Tý
            (date(2021, 2, 12), 1, 1, 2021, False),  # Tết Tân Sửu
            (date(2022, 2, 1), 1, 1, 2022, False),   # Tết Nhâm Dần
            (date(2023, 1, 22), 1, 1, 2023, False),  # Tết Quý Mão
            (date(2024, 2, 10), 1, 1, 2024, False),  # Tết Giáp Thìn
            (date(2025, 1, 29), 1, 1, 2025, False),  # Tết Ất Tỵ
            (date(2026, 2, 17), 1, 1, 2026, False),  # Tết Bính Ngọ
            (date(2027, 2, 6), 1, 1, 2027, False),   # Tết Đinh Mùi
            (date(2026, 4, 26), 10, 3, 2026, False), # Giỗ Tổ Hùng Vương 2026
            (date(2026, 9, 25), 15, 8, 2026, False), # Tết Trung Thu 2026
        ]
        for solar_d, exp_d, exp_m, exp_y, exp_leap in cases:
            with self.subTest(solar=solar_d):
                lunar = solar_to_lunar(solar_d)
                self.assertEqual((lunar.day, lunar.month, lunar.year, lunar.is_leap), (exp_d, exp_m, exp_y, exp_leap))
                roundtrip = lunar_to_solar(exp_d, exp_m, exp_y, is_leap=exp_leap)
                self.assertEqual(roundtrip, solar_d)

    def test_leap_month_and_short_chap_month_handling(self) -> None:
        # 2025 has leap month 6 (tháng 6 nhuận starts 2025-07-25)
        leap_start = solar_to_lunar(date(2025, 7, 25))
        self.assertEqual((leap_start.day, leap_start.month, leap_start.year, leap_start.is_leap), (1, 6, 2025, True))
        self.assertEqual(lunar_to_solar(1, 6, 2025, is_leap=True), date(2025, 7, 25))

        # Non-leap year rejects leap month lookup
        self.assertIsNone(lunar_to_solar(1, 6, 2026, is_leap=True))

        # Lunar year 2024 month 12 (Chạp) has only 29 days: Giao thừa is 29/12 (2025-01-28)
        giao_thua_2025 = solar_to_lunar(date(2025, 1, 28))
        self.assertEqual((giao_thua_2025.day, giao_thua_2025.month, giao_thua_2025.year), (29, 12, 2024))
        self.assertEqual(giao_thua_2025.month_length, 29)
        self.assertIsNone(lunar_to_solar(30, 12, 2024))

    def test_can_chi_hoang_dao_and_auspicious_hours(self) -> None:
        self.assertEqual(get_year_can_chi(2024), "Giáp Thìn")
        self.assertEqual(get_year_can_chi(2025), "Ất Tỵ")
        self.assertEqual(get_year_can_chi(2026), "Bính Ngọ")

        # 2026-02-17 is Mùng 1 Tết Bính Ngọ
        info = get_day_auspicious_info(date(2026, 2, 17))
        self.assertEqual(info.year_can_chi, "Bính Ngọ")
        self.assertEqual(info.month_can_chi, "Canh Dần")
        self.assertEqual(info.day_can_chi, get_day_can_chi(date(2026, 2, 17)))
        self.assertEqual(len(info.auspicious_hours), 6)
        self.assertEqual(info.auspicious_hours, get_auspicious_hours(date(2026, 2, 17)))
        self.assertTrue(bool(info.solar_term))
        self.assertTrue(bool(info.truc_name))

        # Over any 12 consecutive days in the same lunar month, exactly 6 are Hoàng Đạo
        hoang_dao_count = sum(
            1 for offset in range(12) if get_day_auspicious_info(date(2026, 3, 20) + __import__("datetime").timedelta(days=offset)).is_hoang_dao
        )
        self.assertEqual(hoang_dao_count, 6)


class TestVietnameseEvents(unittest.TestCase):
    def test_special_ram_and_tet_do_not_duplicate_generic_moon_events(self) -> None:
        # Mùng 1 Tết (1/1 ÂL) should only emit Tết Nguyên Đán, not an extra generic Mùng 1
        tet_events = get_events_for_date(date(2026, 2, 17))
        tet_ids = [ev.event_id for ev in tet_events]
        self.assertIn("lunar_tet_1", tet_ids)
        self.assertFalse(any(eid.startswith("moon_") for eid in tet_ids))

        # Tết Trung Thu (15/8 ÂL = 2026-09-25) should only emit lunar_trung_thu
        trung_thu_events = get_events_for_date(date(2026, 9, 25))
        trung_thu_ids = [ev.event_id for ev in trung_thu_events]
        self.assertEqual(trung_thu_ids, ["lunar_trung_thu"])

        # Giao thừa in 29-day Chạp month (2025-01-28) is detected automatically
        gt_events = get_events_for_date(date(2025, 1, 28))
        self.assertEqual([ev.event_id for ev in gt_events], ["lunar_giao_thua"])
        self.assertIn("29 Tết", gt_events[0].name)

    def test_regular_mung_1_and_ram_and_leap_month_moon_events(self) -> None:
        # 2026-03-19 is 01/02/2026 ÂL (regular Mùng 1 tháng 2)
        m1 = get_events_for_date(date(2026, 3, 19))
        self.assertEqual([ev.event_id for ev in m1], ["moon_m1_m2"])
        self.assertEqual(m1[0].category, "moon")

        # 2025-07-25 is 01/06 nhuận 2025 -> labeled nhuận and no duplicate lunar holiday
        leap_m1 = get_events_for_date(date(2025, 7, 25))
        self.assertEqual([ev.event_id for ev in leap_m1], ["moon_m1_m6_leap"])
        self.assertIn("nhuận", leap_m1[0].name)

        # 2026-04-30 is Reunification Day
        solar_hol = get_events_for_date(date(2026, 4, 30))
        self.assertTrue(any(ev.event_id == "solar_reunification" and ev.is_public_holiday for ev in solar_hol))

        # 2026-11-24 is Vietnam Culture Day (public holiday)
        culture_day = get_events_for_date(date(2026, 11, 24))
        self.assertTrue(
            any(
                ev.event_id == "solar_vn_culture_day"
                and ev.is_public_holiday
                and "Ngày Văn hóa Việt Nam" in ev.name
                for ev in culture_day
            )
        )

    def test_upcoming_events_and_good_days(self) -> None:
        upcoming = get_upcoming_events(date(2026, 4, 25), days_ahead=10)
        ids = {ev.event_id for _, ev in upcoming}
        self.assertIn("lunar_gio_to", ids)         # 26/04/2026 (10/03 ÂL)
        self.assertIn("solar_reunification", ids)  # 30/04/2026
        self.assertIn("solar_labor_day", ids)      # 01/05/2026

        good_days = get_upcoming_good_days(date(2026, 4, 25), days_ahead=11)
        self.assertGreaterEqual(len(good_days), 5)
        self.assertTrue(all(info.is_hoang_dao for _, info in good_days))


class TestLunarCalendarFeature(unittest.TestCase):
    def _make_config(
        self,
        *,
        send_hour: int = 7,
        send_minute: int = 0,
        lead_days: tuple[int, ...] = (3, 1, 0),
        moon_lead_days: tuple[int, ...] = (1, 0),
    ) -> LunarCalendarConfig:
        return LunarCalendarConfig(
            enabled=True,
            send_hour=send_hour,
            send_minute=send_minute,
            timezone="Asia/Ho_Chi_Minh",
            lead_days=lead_days,
            moon_lead_days=moon_lead_days,
            check_interval=60,
        )

    def _make_update(self, text: str, chat_id: str = "123456") -> dict:
        return {
            "update_id": 1,
            "message": {
                "chat": {"id": int(chat_id)},
                "text": text,
            },
        }

    def test_scheduled_execution_respects_hour_lead_days_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"
            telegram = MagicMock()
            telegram.send_message.return_value = True

            # 2026-04-29 is 1 day before 30/04 and 2 days before 01/05 (and 2 days before Rằm 15/3 = 01/05)
            # Before 07:00 -> should skip
            now_early = datetime(2026, 4, 29, 6, 45, tzinfo=VN_TZ)
            feature_early = LunarCalendarFeature(
                telegram=telegram,
                config=self._make_config(),
                state_store=StateStore(state_path),
                now_provider=lambda _tz: now_early,
            )
            feature_early.execute()
            telegram.send_message.assert_not_called()

            # At 07:05 -> sends reminder for 30/04 (1 day left)
            now_morning = datetime(2026, 4, 29, 7, 5, tzinfo=VN_TZ)
            feature_run1 = LunarCalendarFeature(
                telegram=telegram,
                config=self._make_config(),
                state_store=StateStore(state_path),
                now_provider=lambda _tz: now_morning,
            )
            feature_run1.execute()
            self.assertEqual(telegram.send_message.call_count, 1)
            msg = telegram.send_message.call_args.args[0]
            self.assertIn("Ngày Giải phóng miền Nam 30/4", msg)
            self.assertIn("Ngày mai", msg)
            self.assertIn("Giờ Hoàng Đạo hôm nay", msg)

            # Second run same day (new instance loading same StateStore) -> deduplicated
            feature_run2 = LunarCalendarFeature(
                telegram=telegram,
                config=self._make_config(),
                state_store=StateStore(state_path),
                now_provider=lambda _tz: datetime(2026, 4, 29, 8, 17, tzinfo=VN_TZ),
            )
            feature_run2.execute()
            self.assertEqual(telegram.send_message.call_count, 1)

            # Next day (2026-04-30 at 07:00): reminds 30/04 (Hôm nay) + 01/05 (Ngày mai) + Rằm tháng 3 (Ngày mai)
            feature_next_day = LunarCalendarFeature(
                telegram=telegram,
                config=self._make_config(),
                state_store=StateStore(state_path),
                now_provider=lambda _tz: datetime(2026, 4, 30, 7, 0, tzinfo=VN_TZ),
            )
            feature_next_day.execute()
            self.assertEqual(telegram.send_message.call_count, 2)
            msg2 = telegram.send_message.call_args.args[0]
            self.assertIn("DIỄN RA HÔM NAY", msg2)
            self.assertIn("Ngày Giải phóng miền Nam 30/4", msg2)
            self.assertIn("Ngày Quốc tế Lao động 1/5", msg2)
            self.assertIn("Rằm tháng 3 Âm lịch", msg2)

    def test_failed_telegram_send_does_not_persist_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"
            telegram = MagicMock()
            telegram.send_message.return_value = False

            now = datetime(2026, 4, 30, 7, 10, tzinfo=VN_TZ)
            feature = LunarCalendarFeature(
                telegram=telegram,
                config=self._make_config(),
                state_store=StateStore(state_path),
                now_provider=lambda _tz: now,
            )
            with self.assertRaises(RuntimeError):
                feature.execute()

            # Retry with healthy Telegram should succeed
            telegram.send_message.return_value = True
            feature_retry = LunarCalendarFeature(
                telegram=telegram,
                config=self._make_config(),
                state_store=StateStore(state_path),
                now_provider=lambda _tz: now,
            )
            feature_retry.execute()
            self.assertEqual(telegram.send_message.call_count, 2)

    def test_slash_commands_lunar_upcoming_good_status(self) -> None:
        telegram = MagicMock()
        now = datetime(2026, 4, 26, 9, 0, tzinfo=VN_TZ)  # 10/03/2026 ÂL — Giỗ Tổ Hùng Vương
        feature = LunarCalendarFeature(
            telegram=telegram,
            config=self._make_config(),
            now_provider=lambda _tz: now,
        )
        router = CommandRouter()
        feature.register_commands(router)

        # /lunar (today)
        res = router.handle_update(self._make_update("/lunar"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("26/04/2026", res[1])
        self.assertIn("10/03/2026", res[1])
        self.assertIn("Giỗ Tổ Hùng Vương", res[1])
        self.assertIn("Giờ Hoàng Đạo", res[1])

        # /lunar DD/MM/YYYY (solar -> lunar)
        res = router.handle_update(self._make_update("/lunar 17/02/2026"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("01/01/2026", res[1])
        self.assertIn("Tết Nguyên Đán", res[1])

        # /lunar DD/MM/YYYY am (lunar -> solar)
        res = router.handle_update(self._make_update("/lunar 15/08/2026 am"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("ĐỔI LỊCH ÂM → DƯƠNG", res[1])
        self.assertIn("25/09/2026", res[1])
        self.assertIn("Tết Trung Thu", res[1])

        # /lunar with invalid lunar leap month
        res = router.handle_update(self._make_update("/lunar 15/06/2026 am nhuan"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("không tồn tại", res[1])

        # /lunar_upcoming 10
        res = router.handle_update(self._make_update("/lunar_upcoming 10"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("Giỗ Tổ Hùng Vương", res[1])
        self.assertIn("Ngày Giải phóng miền Nam 30/4", res[1])

        # /lunar_good 14
        res = router.handle_update(self._make_update("/lunar_good 14"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("NGÀY TỐT / HOÀNG ĐẠO", res[1])
        self.assertIn("Hoàng Đạo", res[1])

        # /lunar_status
        res = router.handle_update(self._make_update("/lunar_status"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("TRẠNG THÁI LỊCH ÂM", res[1])
        self.assertIn("07:00", res[1])

    def test_settings_load_and_command_router_integration(self) -> None:
        keys = [
            "TELEGRAM_TOKEN",
            "TELEGRAM_CHAT_ID",
            "LUNAR_ENABLED",
            "LUNAR_SEND_HOUR",
            "LUNAR_SEND_MINUTE",
            "LUNAR_TIMEZONE",
            "LUNAR_LEAD_DAYS",
            "LUNAR_MOON_LEAD_DAYS",
            "LUNAR_CHECK_INTERVAL",
        ]
        backup = {k: os.environ.pop(k, None) for k in keys}
        try:
            os.environ["TELEGRAM_TOKEN"] = "tok"
            os.environ["TELEGRAM_CHAT_ID"] = "123456"
            os.environ["LUNAR_ENABLED"] = "true"
            os.environ["LUNAR_SEND_HOUR"] = "6"
            os.environ["LUNAR_SEND_MINUTE"] = "30"
            os.environ["LUNAR_LEAD_DAYS"] = "5,2,0"
            os.environ["LUNAR_MOON_LEAD_DAYS"] = "2,1,0"
            os.environ["LUNAR_CHECK_INTERVAL"] = "120"

            settings = Settings.load(env_file="nonexistent.env")
            self.assertTrue(settings.lunar_calendar.enabled)
            self.assertEqual(settings.lunar_calendar.send_hour, 6)
            self.assertEqual(settings.lunar_calendar.send_minute, 30)
            self.assertEqual(settings.lunar_calendar.lead_days, (5, 2, 0))
            self.assertEqual(settings.lunar_calendar.moon_lead_days, (2, 1, 0))
            self.assertEqual(settings.lunar_calendar.check_interval, 120)

            router = build_command_router(settings=settings, telegram=MagicMock())
            cmd_names = {c["command"] for c in router.get_telegram_commands()}
            self.assertTrue(
                {"lunar", "lunar_upcoming", "lunar_good", "lunar_status"}.issubset(cmd_names)
            )
        finally:
            for k, v in backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
