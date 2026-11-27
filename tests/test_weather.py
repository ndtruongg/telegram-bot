"""Unit tests for weather & air quality client, alerts, scheduling, and Telegram commands."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import requests

from bot.config import Settings, WeatherConfig
from bot.core.commands import CommandRouter
from bot.core.state import StateStore
from bot.core.webhook import build_command_router
from bot.features.weather.client import (
    AirQualityData,
    DailyForecast,
    GeoLocation,
    HourlyForecast,
    WeatherReport,
    build_raincoat_advice,
    classify_aqi,
    classify_uv,
    describe_weather_code,
    detect_weather_alerts,
    fetch_weather_report,
    geocode_city,
)
from bot.features.weather.feature import WeatherFeature

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _sample_report(
    *,
    city_name: str = "TP. Hồ Chí Minh",
    current_time_iso: str = "2026-11-27T06:30",
    current_temp: float = 26.5,
    rain_prob_max: int = 80,
    precip_sum: float = 12.4,
    uv_max: float = 8.5,
    us_aqi: int | None = 62,
    pm2_5: float | None = 17.3,
    hourly: tuple[HourlyForecast, ...] | None = None,
    today_code: int = 80,
) -> WeatherReport:
    if hourly is None:
        hourly = (
            HourlyForecast("2026-11-27T06:00", 26.0, 20, 0.0, 0.0, 2, 0.2),
            HourlyForecast("2026-11-27T07:00", 27.0, 25, 0.0, 0.0, 2, 1.5),
            HourlyForecast("2026-11-27T14:00", 32.0, 80, 3.2, 3.2, 80, 7.2),
            HourlyForecast("2026-11-27T15:00", 30.5, 85, 6.5, 6.5, 81, 4.1),
        )
    return WeatherReport(
        location=GeoLocation(city_name, 10.8231, 106.6297, "Việt Nam", "", "Asia/Ho_Chi_Minh"),
        current_time_iso=current_time_iso,
        timezone="Asia/Ho_Chi_Minh",
        current_temp=current_temp,
        current_apparent_temp=current_temp + 2.5,
        current_humidity=78,
        current_wind_speed=11.4,
        current_precipitation=0.0,
        current_weather_code=2,
        current_uv=0.5,
        today=DailyForecast(
            date_iso="2026-11-27",
            weather_code=today_code,
            temp_max=33.0,
            temp_min=25.0,
            apparent_max=37.0,
            apparent_min=27.0,
            precipitation_sum=precip_sum,
            precipitation_prob_max=rain_prob_max,
            uv_index_max=uv_max,
            wind_speed_max=18.0,
            sunrise="05:52",
            sunset="17:27",
        ),
        next_days=(
            DailyForecast("2026-11-28", 3, 32.0, 24.5, 36.0, 26.5, 1.2, 40, 7.8, 15.0),
            DailyForecast("2026-11-29", 1, 33.5, 25.0, 37.5, 27.0, 0.0, 15, 9.0, 14.0),
        ),
        hourly=hourly,
        air_quality=(
            AirQualityData(
                us_aqi=us_aqi,
                european_aqi=35,
                pm2_5=pm2_5,
                pm10=28.0,
                peak_us_aqi_today=max(us_aqi, 85),
            )
            if us_aqi is not None
            else None
        ),
    )


class TestWeatherClientAndAnalysis(unittest.TestCase):
    def test_vietnam_city_presets_resolve_without_network(self) -> None:
        session = MagicMock()
        for query, expected_name in [
            ("HCM", "TP. Hồ Chí Minh"),
            ("Sài Gòn", "TP. Hồ Chí Minh"),
            ("Hà Nội", "Hà Nội"),
            ("ha noi", "Hà Nội"),
            ("Đà Nẵng", "Đà Nẵng"),
            ("da lat", "Đà Lạt"),
        ]:
            loc = geocode_city(query, session=session)
            self.assertIsNotNone(loc)
            assert loc is not None
            self.assertEqual(loc.name, expected_name)
        session.get.assert_not_called()

    def test_geocode_city_falls_back_to_open_meteo_api(self) -> None:
        session = MagicMock()
        resp = MagicMock()
        resp.json.return_value = {
            "results": [
                {
                    "name": "Tokyo",
                    "latitude": 35.6895,
                    "longitude": 139.6917,
                    "country": "Japan",
                    "country_code": "JP",
                    "admin1": "Tokyo",
                    "timezone": "Asia/Tokyo",
                }
            ]
        }
        session.get.return_value = resp

        loc = geocode_city("Tokyo", session=session)
        self.assertIsNotNone(loc)
        assert loc is not None
        self.assertEqual(loc.name, "Tokyo")
        self.assertAlmostEqual(loc.latitude, 35.6895)
        self.assertEqual(loc.timezone, "Asia/Tokyo")
        session.get.assert_called_once()

    def test_fetch_weather_report_parses_forecast_and_aqi_and_handles_aq_failure(self) -> None:
        forecast_payload = {
            "timezone": "Asia/Ho_Chi_Minh",
            "current": {
                "time": "2026-11-27T07:00",
                "temperature_2m": 27.4,
                "relative_humidity_2m": 80,
                "apparent_temperature": 30.2,
                "precipitation": 0.0,
                "weather_code": 2,
                "wind_speed_10m": 9.5,
            },
            "hourly": {
                "time": ["2026-11-27T07:00", "2026-11-27T08:00"],
                "temperature_2m": [27.4, 29.0],
                "precipitation_probability": [20, 65],
                "precipitation": [0.0, 2.5],
                "rain": [0.0, 2.5],
                "weather_code": [2, 80],
                "uv_index": [1.8, 4.2],
            },
            "daily": {
                "time": ["2026-11-27", "2026-11-28"],
                "weather_code": [80, 1],
                "temperature_2m_max": [33.0, 34.0],
                "temperature_2m_min": [25.0, 25.5],
                "apparent_temperature_max": [37.0, 38.0],
                "apparent_temperature_min": [27.0, 27.5],
                "precipitation_sum": [8.4, 0.0],
                "precipitation_probability_max": [75, 10],
                "uv_index_max": [9.2, 9.5],
                "wind_speed_10m_max": [16.0, 12.0],
                "sunrise": ["2026-11-27T05:52", "2026-11-28T05:52"],
                "sunset": ["2026-11-27T17:27", "2026-11-28T17:27"],
            },
        }
        aq_payload = {
            "current": {
                "us_aqi": 118,
                "european_aqi": 52,
                "pm2_5": 42.6,
                "pm10": 61.0,
                "uv_index": 2.0,
            },
            "hourly": {
                "time": ["2026-11-27T07:00", "2026-11-27T08:00"],
                "us_aqi": [118, 134],
            },
        }

        session = MagicMock()
        forecast_resp = MagicMock()
        forecast_resp.json.return_value = forecast_payload
        aq_resp = MagicMock()
        aq_resp.json.return_value = aq_payload
        session.get.side_effect = [forecast_resp, aq_resp]

        report = fetch_weather_report("HCM", session=session)
        self.assertEqual(report.location.name, "TP. Hồ Chí Minh")
        self.assertAlmostEqual(report.current_temp or 0.0, 27.4)
        self.assertIsNotNone(report.today)
        assert report.today is not None
        self.assertEqual(report.today.precipitation_prob_max, 75)
        self.assertEqual(report.today.sunrise, "05:52")
        self.assertIsNotNone(report.air_quality)
        assert report.air_quality is not None
        self.assertEqual(report.air_quality.us_aqi, 118)
        self.assertEqual(report.air_quality.peak_us_aqi_today, 134)

        # If AQ request fails, report still succeeds with air_quality=None
        session_no_aq = MagicMock()
        session_no_aq.get.side_effect = [forecast_resp, requests.RequestException("AQ down")]
        report_no_aq = fetch_weather_report("HCM", session=session_no_aq)
        self.assertIsNone(report_no_aq.air_quality)
        self.assertAlmostEqual(report_no_aq.current_uv or 0.0, 1.8)

    def test_classification_raincoat_advice_and_alert_detection(self) -> None:
        self.assertEqual(describe_weather_code(95), ("⛈️", "Mưa giông"))
        self.assertEqual(classify_uv(8.5)[1], "Rất cao")
        self.assertEqual(classify_aqi(165)[1], "Xấu")

        # Rainy report advises raincoat & mentions time window
        rainy = _sample_report(rain_prob_max=85, precip_sum=14.0)
        icon, advice = build_raincoat_advice(rainy)
        self.assertEqual(icon, "☔")
        self.assertIn("mang áo mưa", advice.lower())
        self.assertIn("14:00–16:00", advice)

        # Dry report says no raincoat needed
        dry = _sample_report(
            rain_prob_max=10,
            precip_sum=0.0,
            today_code=1,
            hourly=(HourlyForecast("2026-11-27T07:00", 28.0, 10, 0.0, 0.0, 1, 2.0),),
        )
        icon_dry, advice_dry = build_raincoat_advice(dry)
        self.assertEqual(icon_dry, "☀️")
        self.assertIn("Không cần mang áo mưa", advice_dry)

        # Detect upcoming heavy rain & bad AQI within lookahead window
        storm_report = _sample_report(
            current_time_iso="2026-11-27T13:00",
            us_aqi=172,
            pm2_5=96.5,
            hourly=(
                HourlyForecast("2026-11-27T12:00", 31.0, 95, 12.0, 12.0, 95, 6.0),  # past hour
                HourlyForecast("2026-11-27T13:00", 30.0, 30, 0.2, 0.2, 3, 5.0),
                HourlyForecast("2026-11-27T14:00", 28.5, 85, 8.4, 8.4, 95, 3.0),  # imminent heavy rain
                HourlyForecast("2026-11-27T15:00", 27.0, 80, 6.0, 6.0, 82, 2.0),
            ),
        )
        alerts = detect_weather_alerts(
            storm_report,
            rain_lookahead_hours=3,
            rain_alert_mm=5.0,
            rain_alert_prob=75,
            aqi_threshold=151,
        )
        self.assertTrue(alerts.has_alert)
        self.assertIsNotNone(alerts.heavy_rain)
        self.assertIsNotNone(alerts.bad_aqi)
        assert alerts.heavy_rain is not None
        assert alerts.bad_aqi is not None
        self.assertEqual(alerts.heavy_rain.peak_hour_label, "14:00")
        self.assertAlmostEqual(alerts.heavy_rain.max_precipitation_mm, 8.4)
        self.assertEqual(alerts.bad_aqi.us_aqi, 172)


class TestWeatherFeature(unittest.TestCase):
    def _make_config(self, **overrides) -> WeatherConfig:
        defaults = dict(
            enabled=True,
            city="Ho Chi Minh City",
            send_hour=6,
            send_minute=30,
            timezone="Asia/Ho_Chi_Minh",
            check_interval=60,
            alerts_enabled=True,
            alert_check_interval=1800,
            alert_cooldown_minutes=180,
            alert_start_hour=6,
            alert_end_hour=22,
            rain_lookahead_hours=3,
            rain_alert_mm=5.0,
            rain_alert_prob=75,
            aqi_alert_threshold=151,
        )
        defaults.update(overrides)
        return WeatherConfig(**defaults)

    def _make_update(self, text: str, chat_id: str = "123456") -> dict:
        return {
            "update_id": 1,
            "message": {
                "chat": {"id": int(chat_id)},
                "text": text,
            },
        }

    def test_morning_forecast_schedule_deduplication_and_failure_safety(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"
            telegram = MagicMock()
            telegram.send_message.return_value = True
            fetch_calls = 0

            def fake_fetcher(city: str, **_kwargs) -> WeatherReport:
                nonlocal fetch_calls
                fetch_calls += 1
                return _sample_report(city_name=city)

            # 05:45 -> before 06:30 AND outside alert_start_hour=6 -> 0 fetches, 0 messages
            now_box = [datetime(2026, 11, 27, 5, 45, tzinfo=VN_TZ)]
            feature = WeatherFeature(
                telegram=telegram,
                config=self._make_config(),
                fetcher=fake_fetcher,
                state_store=StateStore(state_path),
                now_provider=lambda _tz: now_box[0],
            )
            feature.execute()
            self.assertEqual(fetch_calls, 0)
            telegram.send_message.assert_not_called()

            # 06:30 -> sends morning forecast with temp, rain prob, UV, AQI, raincoat advice
            now_box[0] = datetime(2026, 11, 27, 6, 30, tzinfo=VN_TZ)
            feature.execute()
            self.assertEqual(fetch_calls, 1)
            self.assertEqual(telegram.send_message.call_count, 1)
            sent_text = telegram.send_message.call_args.args[0]
            self.assertIn("DỰ BÁO THỜI TIẾT SÁNG SỚM", sent_text)
            self.assertIn("Xác suất mưa", sent_text)
            self.assertIn("Chỉ số UV", sent_text)
            self.assertIn("US AQI", sent_text)
            self.assertIn("mang áo mưa", sent_text.lower())

            # 06:35 -> already sent morning forecast & within alert_check_interval -> 0 fetches
            now_box[0] = datetime(2026, 11, 27, 6, 35, tzinfo=VN_TZ)
            restarted = WeatherFeature(
                telegram=telegram,
                config=self._make_config(),
                fetcher=fake_fetcher,
                state_store=StateStore(state_path),
                now_provider=lambda _tz: now_box[0],
            )
            restarted.execute()
            self.assertEqual(fetch_calls, 1)
            self.assertEqual(telegram.send_message.call_count, 1)

            # Next day 06:30 with Telegram failure -> raises and does NOT mark date as sent
            now_box[0] = datetime(2026, 11, 28, 6, 30, tzinfo=VN_TZ)
            telegram.send_message.return_value = False
            with self.assertRaises(RuntimeError):
                restarted.execute()
            self.assertEqual(StateStore(state_path).get("weather", "last_sent_date"), "2026-11-27")

    def test_periodic_heavy_rain_and_bad_aqi_alerts_respect_cooldown_and_quiet_hours(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"
            # Pretend morning forecast already sent today
            store = StateStore(state_path)
            store.set("weather", "last_sent_date", "2026-11-27")

            telegram = MagicMock()
            telegram.send_message.return_value = True

            severe_report = _sample_report(
                current_time_iso="2026-11-27T13:00",
                us_aqi=178,
                pm2_5=102.0,
                hourly=(
                    HourlyForecast("2026-11-27T13:00", 30.0, 40, 0.5, 0.5, 3, 5.0),
                    HourlyForecast("2026-11-27T14:00", 28.0, 90, 9.5, 9.5, 95, 2.0),
                ),
            )
            now_box = [datetime(2026, 11, 27, 13, 0, tzinfo=VN_TZ)]
            feature = WeatherFeature(
                telegram=telegram,
                config=self._make_config(alert_check_interval=1800, alert_cooldown_minutes=180),
                fetcher=lambda *_a, **_kw: severe_report,
                state_store=StateStore(state_path),
                now_provider=lambda _tz: now_box[0],
            )

            # First check at 13:00 -> fires combined heavy rain + bad AQI alert
            feature.execute()
            self.assertEqual(telegram.send_message.call_count, 1)
            alert_text = telegram.send_message.call_args.args[0]
            self.assertIn("CẢNH BÁO MƯA LỚN & AQI XẤU", alert_text)
            self.assertIn("9.5 mm/h", alert_text)
            self.assertIn("178", alert_text)

            # 13:40 (+40m > check_interval 30m, but < cooldown 180m) -> checked, NOT re-sent
            now_box[0] = datetime(2026, 11, 27, 13, 40, tzinfo=VN_TZ)
            feature.execute()
            self.assertEqual(telegram.send_message.call_count, 1)

            # 16:05 (+185m > cooldown 180m) -> re-alerts after cooldown expires
            now_box[0] = datetime(2026, 11, 27, 16, 5, tzinfo=VN_TZ)
            feature.execute()
            self.assertEqual(telegram.send_message.call_count, 2)

            # 23:30 (outside 06:00–22:59 quiet hours) -> no alert even after cooldown
            now_box[0] = datetime(2026, 11, 27, 23, 30, tzinfo=VN_TZ)
            feature.execute()
            self.assertEqual(telegram.send_message.call_count, 2)

    def test_slash_commands_weather_city_and_status(self) -> None:
        telegram = MagicMock()
        github_vars = MagicMock()
        github_vars.repository = "ndtruongg/telegram-bot"
        github_vars.get_variable.return_value = "Hà Nội"
        github_vars.set_variable.return_value = True

        seen_cities: list[str] = []

        def fake_fetcher(city: str, **_kwargs) -> WeatherReport:
            seen_cities.append(city)
            if city.lower() == "khongtontai":
                raise LookupError("not found")
            return _sample_report(city_name=city)

        feature = WeatherFeature(
            telegram=telegram,
            config=self._make_config(),
            fetcher=fake_fetcher,
            geocoder=lambda q: (
                GeoLocation("Đà Nẵng", 16.0544, 108.2022, "Việt Nam")
                if "nang" in q.lower() or "nẵng" in q.lower()
                else None
            ),
            github_vars=github_vars,
            now_provider=lambda _tz: datetime(2026, 11, 27, 9, 0, tzinfo=VN_TZ),
        )
        router = CommandRouter()
        feature.register_commands(router)

        # /weather (no args) uses GitHub Variable WEATHER_CITY ("Hà Nội")
        res = router.handle_update(self._make_update("/weather"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(seen_cities[-1], "Hà Nội")
        self.assertIn("Hà Nội", res[1])
        self.assertIn("US AQI", res[1])

        # /weather Đà Nẵng queries specified city directly
        res = router.handle_update(self._make_update("/weather Đà Nẵng"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(seen_cities[-1], "Đà Nẵng")
        self.assertIn("Đà Nẵng", res[1])

        # /weather Unknown city returns friendly error
        res = router.handle_update(self._make_update("/weather khongtontai"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("Không tìm thấy thành phố", res[1])

        # /weather_city Đà Nẵng validates and saves to GitHub Variable WEATHER_CITY
        res = router.handle_update(
            self._make_update("/weather_city Đà Nẵng"), allowed_chat_id="123456"
        )
        self.assertIsNotNone(res)
        assert res is not None
        github_vars.set_variable.assert_called_once_with("WEATHER_CITY", "Đà Nẵng")
        self.assertIn("Đã cập nhật thành phố mặc định", res[1])

        # /weather_status reflects config and active city
        github_vars.get_variable.return_value = "Đà Nẵng"
        res = router.handle_update(self._make_update("/weather_status"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("Đà Nẵng", res[1])
        self.assertIn("06:30", res[1])
        self.assertIn("Open-Meteo", res[1])

    def test_settings_load_and_command_router_integration(self) -> None:
        keys = [
            "TELEGRAM_TOKEN",
            "TELEGRAM_CHAT_ID",
            "WEATHER_ENABLED",
            "WEATHER_CITY",
            "WEATHER_LATITUDE",
            "WEATHER_LONGITUDE",
            "WEATHER_SEND_HOUR",
            "WEATHER_SEND_MINUTE",
            "WEATHER_AQI_THRESHOLD",
            "WEATHER_RAIN_ALERT_MM",
            "WEATHER_RAIN_ALERT_PROB",
        ]
        backup = {k: os.environ.pop(k, None) for k in keys}
        try:
            os.environ["TELEGRAM_TOKEN"] = "tok"
            os.environ["TELEGRAM_CHAT_ID"] = "123"
            os.environ["WEATHER_ENABLED"] = "true"
            os.environ["WEATHER_CITY"] = "Đà Nẵng"
            os.environ["WEATHER_LATITUDE"] = "16.0544"
            os.environ["WEATHER_LONGITUDE"] = "108.2022"
            os.environ["WEATHER_SEND_HOUR"] = "6"
            os.environ["WEATHER_SEND_MINUTE"] = "15"
            os.environ["WEATHER_AQI_THRESHOLD"] = "120"
            os.environ["WEATHER_RAIN_ALERT_MM"] = "4.5"
            os.environ["WEATHER_RAIN_ALERT_PROB"] = "70"

            settings = Settings.load(env_file="nonexistent.env")
            self.assertTrue(settings.weather.enabled)
            self.assertEqual(settings.weather.city, "Đà Nẵng")
            self.assertAlmostEqual(settings.weather.latitude or 0.0, 16.0544)
            self.assertAlmostEqual(settings.weather.longitude or 0.0, 108.2022)
            self.assertEqual(settings.weather.send_hour, 6)
            self.assertEqual(settings.weather.send_minute, 15)
            self.assertEqual(settings.weather.aqi_alert_threshold, 120)
            self.assertAlmostEqual(settings.weather.rain_alert_mm, 4.5)
            self.assertEqual(settings.weather.rain_alert_prob, 70)

            router = build_command_router(settings=settings, telegram=MagicMock())
            cmd_names = {c["command"] for c in router.get_telegram_commands()}
            self.assertTrue({"weather", "weather_city", "weather_status"}.issubset(cmd_names))
        finally:
            for k, v in backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
