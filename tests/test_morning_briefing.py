"""Unit tests for the consolidated Morning Briefing feature and duplicate suppression."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from bot.config import (
    DEFAULT_NEWS_RSS_URLS,
    GoldPriceConfig,
    MorningBriefingConfig,
    MorningNewsConfig,
    RemindersConfig,
    Settings,
    WeatherConfig,
)
from bot.core.commands import CommandRouter
from bot.core.state import StateStore
from bot.core.webhook import build_command_router
from bot.features import load_features
from bot.features.gold_price.scraper import GoldPrice
from bot.features.morning_briefing.feature import MorningBriefingFeature
from bot.features.morning_news.feature import MorningNewsFeature
from bot.features.morning_news.rss import NewsArticle
from bot.features.morning_news.summarizer import NewsDigest, SummarizedItem
from bot.features.reminders.feature import RemindersFeature
from bot.features.reminders.models import ReminderItem, serialize_reminders
from bot.features.weather.client import (
    AirQualityData,
    DailyForecast,
    GeoLocation,
    HourlyForecast,
    WeatherReport,
)
from bot.features.weather.feature import WeatherFeature

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _sample_weather(
    *,
    city_name: str = "TP. Hồ Chí Minh",
    current_time_iso: str = "2026-11-27T07:00",
    us_aqi: int = 62,
    hourly: tuple[HourlyForecast, ...] | None = None,
) -> WeatherReport:
    if hourly is None:
        hourly = (
            HourlyForecast("2026-11-27T07:00", 26.5, 25, 0.0, 0.0, 2, 1.5),
            HourlyForecast("2026-11-27T14:00", 32.0, 80, 3.5, 3.5, 80, 7.0),
        )
    return WeatherReport(
        location=GeoLocation(city_name, 10.8231, 106.6297, "Việt Nam", "", "Asia/Ho_Chi_Minh"),
        current_time_iso=current_time_iso,
        timezone="Asia/Ho_Chi_Minh",
        current_temp=26.5,
        current_apparent_temp=29.0,
        current_humidity=78,
        current_wind_speed=11.0,
        current_precipitation=0.0,
        current_weather_code=2,
        current_uv=1.5,
        today=DailyForecast(
            date_iso="2026-11-27",
            weather_code=80,
            temp_max=33.0,
            temp_min=25.0,
            precipitation_sum=10.5,
            precipitation_prob_max=80,
            uv_index_max=8.5,
            sunrise="05:52",
            sunset="17:27",
        ),
        next_days=(),
        hourly=hourly,
        air_quality=AirQualityData(
            us_aqi=us_aqi,
            european_aqi=35,
            pm2_5=17.3,
            pm10=28.0,
            peak_us_aqi_today=max(us_aqi, 75),
        ),
    )


def _sample_articles(count: int = 6) -> list[NewsArticle]:
    return [
        NewsArticle(
            title=f"Tin nổi bật <#{i}> & sáng nay",
            link=f"https://vnexpress.net/tin-{i}.html",
            summary=f"Tóm tắt ngắn gọn tin số {i}.",
            source="VnExpress" if i % 2 else "Tuổi Trẻ",
        )
        for i in range(1, count + 1)
    ]


def _sample_digest(articles: list[NewsArticle], *, used_ai: bool = True) -> NewsDigest:
    return NewsDigest(
        overview="Bức tranh thời sự <sáng nay> & điểm chính.",
        items=tuple(
            SummarizedItem(headline=a.title, summary=a.summary, article=a)
            for a in articles
        ),
        used_ai=used_ai,
    )


class TestMorningBriefingFeature(unittest.TestCase):
    def _make_configs(
        self,
        *,
        reminders_json: str = "",
        briefing_overrides: dict | None = None,
    ) -> tuple[
        MorningBriefingConfig,
        WeatherConfig,
        GoldPriceConfig,
        MorningNewsConfig,
        RemindersConfig,
    ]:
        briefing_defaults = dict(
            enabled=True,
            send_hour=7,
            send_minute=0,
            timezone="Asia/Ho_Chi_Minh",
            check_interval=60,
            max_news_items=5,
            include_weather=True,
            include_gold=True,
            include_news=True,
            include_reminders=True,
            suppress_individual=True,
        )
        if briefing_overrides:
            briefing_defaults.update(briefing_overrides)

        return (
            MorningBriefingConfig(**briefing_defaults),
            WeatherConfig(enabled=True, city="Ho Chi Minh City"),
            GoldPriceConfig(
                enabled=True,
                url="https://webgia.com/gia-vang/sjc/",
                target_price=13500000.0,
                check_interval=3600,
            ),
            MorningNewsConfig(enabled=True, rss_urls=DEFAULT_NEWS_RSS_URLS),
            RemindersConfig(enabled=True, reminders_json=reminders_json),
        )

    def test_schedule_content_deduplication_and_failure_safety(self) -> None:
        items = [
            ReminderItem(id=1, title="Họp <team> & review", event_date=date(2026, 11, 27), event_time="09:30"),
            ReminderItem(id=2, title="Đóng tiền nhà", event_date=date(2026, 11, 28)),
        ]
        briefing_cfg, weather_cfg, gold_cfg, news_cfg, rem_cfg = self._make_configs(
            reminders_json=serialize_reminders(items)
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"
            telegram = MagicMock()
            telegram.send_message.return_value = True
            now_box = [datetime(2026, 11, 27, 6, 45, tzinfo=VN_TZ)]

            def make_feature() -> MorningBriefingFeature:
                return MorningBriefingFeature(
                    telegram=telegram,
                    config=briefing_cfg,
                    weather_config=weather_cfg,
                    gold_config=gold_cfg,
                    news_config=news_cfg,
                    reminders_config=rem_cfg,
                    weather_fetcher=lambda city, **_kw: _sample_weather(city_name=city),
                    gold_fetcher=lambda _url: GoldPrice("SJC 1L - 10L", "13.200.000", 13450000.0, "13.450.000"),
                    news_fetcher=lambda _urls, _lim: _sample_articles(6),
                    news_summarizer=_sample_digest,
                    state_store=StateStore(state_path),
                    now_provider=lambda _tz: now_box[0],
                )

            # 06:45 -> before 07:00 -> skip
            make_feature().execute()
            telegram.send_message.assert_not_called()

            # 07:00 -> sends single consolidated message with all 4 sections
            now_box[0] = datetime(2026, 11, 27, 7, 0, tzinfo=VN_TZ)
            make_feature().execute()
            self.assertEqual(telegram.send_message.call_count, 1)
            sent = telegram.send_message.call_args.args[0]
            self.assertTrue(telegram.send_message.call_args.kwargs["disable_web_page_preview"])
            self.assertLessEqual(len(sent), 4096)

            self.assertIn("BẢN TIN TỔNG HỢP BUỔI SÁNG", sent)
            self.assertIn("Âm lịch:", sent)
            self.assertIn("THỜI TIẾT", sent)
            self.assertIn("US AQI", sent)
            self.assertIn("GIÁ VÀNG SJC", sent)
            self.assertIn("13.450.000", sent)
            self.assertIn("Đã đạt ngưỡng mục tiêu", sent)
            self.assertIn("NHẮC VIỆC", sent)
            self.assertIn("Họp &lt;team&gt; &amp; review", sent)
            self.assertIn("Đóng tiền nhà", sent)
            self.assertIn("TIN TỨC NỔI BẬT", sent)
            self.assertIn("Tin nổi bật &lt;#5&gt;", sent)
            # Capped at max_news_items=5
            self.assertNotIn("Tin nổi bật &lt;#6&gt;", sent)

            # 07:15 same day -> already sent -> skips
            now_box[0] = datetime(2026, 11, 27, 7, 15, tzinfo=VN_TZ)
            make_feature().execute()
            self.assertEqual(telegram.send_message.call_count, 1)

            # Next day 07:00 with Telegram error -> raises & does NOT mark sent
            now_box[0] = datetime(2026, 11, 28, 7, 0, tzinfo=VN_TZ)
            telegram.send_message.return_value = False
            with self.assertRaises(RuntimeError):
                make_feature().execute()
            self.assertEqual(
                StateStore(state_path).get("morning_briefing", "last_sent_date"),
                "2026-11-27",
            )

            # Retry next day 07:05 succeeds
            now_box[0] = datetime(2026, 11, 28, 7, 5, tzinfo=VN_TZ)
            telegram.send_message.return_value = True
            make_feature().execute()
            self.assertEqual(telegram.send_message.call_count, 3)  # 1 ok + 1 failed + 1 ok
            self.assertEqual(
                StateStore(state_path).get("morning_briefing", "last_sent_date"),
                "2026-11-28",
            )

    def test_partial_failure_still_sends_and_total_failure_raises(self) -> None:
        briefing_cfg, weather_cfg, gold_cfg, news_cfg, rem_cfg = self._make_configs()
        telegram = MagicMock()
        telegram.send_message.return_value = True

        # Weather raises & gold returns None, but reminders & news still succeed -> sends partial briefing
        partial = MorningBriefingFeature(
            telegram=telegram,
            config=briefing_cfg,
            weather_config=weather_cfg,
            gold_config=gold_cfg,
            news_config=news_cfg,
            reminders_config=rem_cfg,
            weather_fetcher=lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("weather down")),
            gold_fetcher=lambda _url: None,
            news_fetcher=lambda _urls, _lim: _sample_articles(2),
            news_summarizer=_sample_digest,
            now_provider=lambda _tz: datetime(2026, 11, 27, 7, 0, tzinfo=VN_TZ),
        )
        partial.execute()
        self.assertEqual(telegram.send_message.call_count, 1)
        sent = telegram.send_message.call_args.args[0]
        self.assertIn("Không thể tải dữ liệu thời tiết", sent)
        self.assertIn("Không thể lấy dữ liệu giá vàng", sent)
        self.assertIn("Hôm nay không có nhắc việc", sent)
        self.assertIn("Tin nổi bật", sent)

        # Total failure (only weather & gold enabled, both fail) -> raises and does NOT send
        only_two = MorningBriefingConfig(
            enabled=True,
            include_weather=True,
            include_gold=True,
            include_news=False,
            include_reminders=False,
        )
        telegram.reset_mock()
        total_fail = MorningBriefingFeature(
            telegram=telegram,
            config=only_two,
            weather_config=weather_cfg,
            gold_config=gold_cfg,
            weather_fetcher=lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")),
            gold_fetcher=lambda _url: None,
            now_provider=lambda _tz: datetime(2026, 11, 27, 7, 0, tzinfo=VN_TZ),
        )
        with self.assertRaises(RuntimeError):
            total_fail.execute()
        telegram.send_message.assert_not_called()

    def test_suppresses_individual_morning_digests_while_keeping_daytime_alerts(self) -> None:
        items = [
            ReminderItem(id=1, title="Uống thuốc sáng", event_date=date(2026, 11, 27), event_time="07:00"),
            ReminderItem(id=2, title="Họp chiều", event_date=date(2026, 11, 27), event_time="15:00"),
            ReminderItem(id=3, title="Đóng tiền nhà", event_date=date(2026, 11, 28)),
        ]
        briefing_cfg, weather_cfg, gold_cfg, news_cfg, rem_cfg = self._make_configs(
            reminders_json=serialize_reminders(items)
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"
            store = StateStore(state_path)
            telegram = MagicMock()
            telegram.send_message.return_value = True
            now_box = [datetime(2026, 11, 27, 7, 0, tzinfo=VN_TZ)]

            severe_afternoon = _sample_weather(
                current_time_iso="2026-11-27T14:00",
                us_aqi=175,
                hourly=(
                    HourlyForecast("2026-11-27T14:00", 30.0, 40, 0.2, 0.2, 3, 5.0),
                    HourlyForecast("2026-11-27T15:00", 28.0, 90, 9.0, 9.0, 95, 2.0),
                ),
            )
            weather_now = [_sample_weather()]

            briefing = MorningBriefingFeature(
                telegram=telegram,
                config=briefing_cfg,
                weather_config=weather_cfg,
                gold_config=gold_cfg,
                news_config=news_cfg,
                reminders_config=rem_cfg,
                weather_fetcher=lambda *_a, **_kw: weather_now[0],
                gold_fetcher=lambda _url: GoldPrice("SJC", "13.200.000", 13600000.0, "13.600.000"),
                news_fetcher=lambda _urls, _lim: _sample_articles(3),
                news_summarizer=_sample_digest,
                state_store=store,
                now_provider=lambda _tz: now_box[0],
            )
            news = MorningNewsFeature(
                telegram=telegram,
                config=news_cfg,
                fetcher=lambda _urls, _lim: _sample_articles(3),
                summarizer=_sample_digest,
                state_store=store,
                now_provider=lambda _tz: now_box[0],
                suppress_scheduled=True,
            )
            weather = WeatherFeature(
                telegram=telegram,
                config=weather_cfg,
                fetcher=lambda *_a, **_kw: weather_now[0],
                state_store=store,
                now_provider=lambda _tz: now_box[0],
                suppress_morning_digest=True,
            )
            reminders = RemindersFeature(
                telegram=telegram,
                config=rem_cfg,
                state_store=store,
                now_provider=lambda _tz: now_box[0],
                suppress_morning_digest=True,
            )

            # At 07:00: ONLY MorningBriefing sends 1 message; standalone news/weather/reminders do NOT duplicate
            briefing.execute()
            news.execute()
            weather.execute()
            reminders.execute()
            self.assertEqual(telegram.send_message.call_count, 1)
            msg_morning = telegram.send_message.call_args.args[0]
            self.assertIn("Uống thuốc sáng", msg_morning)
            self.assertIn("Họp chiều", msg_morning)
            self.assertIn("Đóng tiền nhà", msg_morning)

            # At 14:00: WeatherFeature STILL fires daytime severe weather / AQI alert!
            now_box[0] = datetime(2026, 11, 27, 14, 0, tzinfo=VN_TZ)
            weather_now[0] = severe_afternoon
            weather.execute()
            self.assertEqual(telegram.send_message.call_count, 2)
            self.assertIn("CẢNH BÁO MƯA LỚN &amp; AQI XẤU", telegram.send_message.call_args.args[0])

            # At 15:00: RemindersFeature STILL fires exact-time alert for "Họp chiều" (and NOT 07:00 item)
            now_box[0] = datetime(2026, 11, 27, 15, 0, tzinfo=VN_TZ)
            reminders.execute()
            self.assertEqual(telegram.send_message.call_count, 3)
            msg_timed = telegram.send_message.call_args.args[0]
            self.assertIn("ĐẾN GIỜ NHẮC VIỆC", msg_timed)
            self.assertIn("Họp chiều", msg_timed)
            self.assertNotIn("Uống thuốc sáng", msg_timed)
            self.assertNotIn("Đóng tiền nhà", msg_timed)

    def test_long_content_respects_telegram_4096_limit(self) -> None:
        long_reminders = [
            ReminderItem(id=i, title=f"Công việc quan trọng số {i} " + ("x" * 60), event_date=date(2026, 11, 27))
            for i in range(1, 25)
        ]
        long_articles = [
            NewsArticle(
                title=f"Tiêu đề rất dài số {i} " + ("A" * 120),
                link=f"https://vnexpress.net/bai-viet-rat-dai-{i}.html",
                summary="Tóm tắt rất dài " + ("B" * 200),
                source="VnExpress",
            )
            for i in range(1, 13)
        ]
        briefing_cfg, weather_cfg, gold_cfg, news_cfg, rem_cfg = self._make_configs(
            reminders_json=serialize_reminders(long_reminders),
            briefing_overrides={"max_news_items": 12},
        )
        telegram = MagicMock()
        telegram.send_message.return_value = True

        feature = MorningBriefingFeature(
            telegram=telegram,
            config=briefing_cfg,
            weather_config=weather_cfg,
            gold_config=gold_cfg,
            news_config=news_cfg,
            reminders_config=rem_cfg,
            weather_fetcher=lambda *_a, **_kw: _sample_weather(),
            gold_fetcher=lambda _url: GoldPrice("SJC", "13.200.000", 13450000.0, "13.450.000"),
            news_fetcher=lambda _urls, _lim: long_articles,
            news_summarizer=_sample_digest,
            now_provider=lambda _tz: datetime(2026, 11, 27, 7, 0, tzinfo=VN_TZ),
        )
        feature.execute()
        sent = telegram.send_message.call_args.args[0]
        self.assertLessEqual(len(sent), 4096)

    def test_slash_commands_and_github_vars_refresh(self) -> None:
        briefing_cfg, weather_cfg, gold_cfg, news_cfg, rem_cfg = self._make_configs()
        telegram = MagicMock()

        github_vars = MagicMock()
        github_vars.get_variable.side_effect = lambda key: {
            "WEATHER_CITY": "Hà Nội",
            "GOLD_TARGET_PRICE": "14000000",
            "REMINDERS_DATA": serialize_reminders(
                [ReminderItem(id=7, title="Bay đi công tác", event_date=date(2026, 11, 27), event_time="08:00")]
            ),
        }.get(key)

        seen_cities: list[str] = []

        def fake_weather(city: str, **_kw) -> WeatherReport:
            seen_cities.append(city)
            return _sample_weather(city_name=city)

        feature = MorningBriefingFeature(
            telegram=telegram,
            config=briefing_cfg,
            weather_config=weather_cfg,
            gold_config=gold_cfg,
            news_config=news_cfg,
            reminders_config=rem_cfg,
            weather_fetcher=fake_weather,
            gold_fetcher=lambda _url: GoldPrice("SJC", "13.500.000", 13800000.0, "13.800.000"),
            news_fetcher=lambda _urls, _lim: _sample_articles(2),
            news_summarizer=_sample_digest,
            github_vars=github_vars,
            now_provider=lambda _tz: datetime(2026, 11, 27, 5, 30, tzinfo=VN_TZ),
        )
        router = CommandRouter()
        feature.register_commands(router)

        # /briefing works at any hour and reads live GitHub Variables
        res = router.handle_update(
            {"message": {"chat": {"id": 123}, "text": "/briefing"}},
            allowed_chat_id="123",
        )
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(seen_cities[-1], "Hà Nội")
        self.assertIn("Hà Nội", res[1])
        self.assertIn("14,000,000", res[1])
        self.assertIn("Bay đi công tác", res[1])
        self.assertIn("Tin nổi bật", res[1])

        # /briefing_status displays schedule and active sections
        res_status = router.handle_update(
            {"message": {"chat": {"id": 123}, "text": "/briefing_status"}},
            allowed_chat_id="123",
        )
        self.assertIsNotNone(res_status)
        assert res_status is not None
        self.assertIn("07:00", res_status[1])
        self.assertIn("Gộp 1 tin duy nhất", res_status[1])
        self.assertIn("tối đa <b>5</b> tin", res_status[1])

    def test_settings_load_load_features_and_command_router(self) -> None:
        keys = [
            "TELEGRAM_TOKEN",
            "TELEGRAM_CHAT_ID",
            "BRIEFING_ENABLED",
            "BRIEFING_SEND_HOUR",
            "BRIEFING_SEND_MINUTE",
            "BRIEFING_MAX_NEWS",
            "BRIEFING_INCLUDE_WEATHER",
            "BRIEFING_INCLUDE_GOLD",
            "BRIEFING_INCLUDE_NEWS",
            "BRIEFING_INCLUDE_REMINDERS",
            "BRIEFING_SUPPRESS_INDIVIDUAL",
        ]
        backup = {k: os.environ.pop(k, None) for k in keys}
        try:
            os.environ["TELEGRAM_TOKEN"] = "tok"
            os.environ["TELEGRAM_CHAT_ID"] = "123"
            os.environ["BRIEFING_ENABLED"] = "true"
            os.environ["BRIEFING_SEND_HOUR"] = "7"
            os.environ["BRIEFING_SEND_MINUTE"] = "15"
            os.environ["BRIEFING_MAX_NEWS"] = "6"
            os.environ["BRIEFING_INCLUDE_WEATHER"] = "true"
            os.environ["BRIEFING_INCLUDE_GOLD"] = "true"
            os.environ["BRIEFING_INCLUDE_NEWS"] = "true"
            os.environ["BRIEFING_INCLUDE_REMINDERS"] = "true"
            os.environ["BRIEFING_SUPPRESS_INDIVIDUAL"] = "true"

            settings = Settings.load(env_file="nonexistent.env")
            self.assertTrue(settings.morning_briefing.enabled)
            self.assertEqual(settings.morning_briefing.send_hour, 7)
            self.assertEqual(settings.morning_briefing.send_minute, 15)
            self.assertEqual(settings.morning_briefing.max_news_items, 6)
            self.assertTrue(settings.morning_briefing.suppress_individual)

            # load_features puts morning_briefing first and passes suppression flags
            features = load_features(settings=settings, telegram=MagicMock())
            self.assertEqual(features[0].name, "morning_briefing")
            by_name = {f.name: f for f in features}
            self.assertTrue(by_name["morning_news"]._suppress_scheduled)  # type: ignore[attr-defined]
            self.assertTrue(by_name["weather"]._suppress_morning_digest)  # type: ignore[attr-defined]
            self.assertTrue(by_name["reminders"]._suppress_morning_digest)  # type: ignore[attr-defined]

            # CommandRouter exposes /briefing and /briefing_status
            router = build_command_router(settings=settings, telegram=MagicMock())
            cmd_names = {c["command"] for c in router.get_telegram_commands()}
            self.assertTrue({"briefing", "briefing_status"}.issubset(cmd_names))
        finally:
            for k, v in backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
