"""Unit tests for parsers, config, scraper, scheduler, and gold price feature."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from bot.config import GoldPriceConfig, Settings
from bot.core.feature import BaseFeature
from bot.core.scheduler import BotScheduler
from bot.core.state import StateStore
from bot.features.gold_price.feature import GoldPriceFeature
from bot.features.gold_price.scraper import GoldPrice, parse_sjc_gold_html
from bot.utils.parsers import format_price, parse_price


class TestParsers(unittest.TestCase):
    def test_parse_price_formats(self) -> None:
        self.assertEqual(parse_price("13.500.000"), 13500000.0)
        self.assertEqual(parse_price("13,500,000"), 13500000.0)
        self.assertEqual(parse_price(" 85000000 "), 85000000.0)
        self.assertIsNone(parse_price("-"))
        self.assertIsNone(parse_price("N/A"))
        self.assertIsNone(parse_price(""))

    def test_format_price(self) -> None:
        self.assertEqual(format_price(13500000), "13,500,000")


class TestGoldScraper(unittest.TestCase):
    def test_parse_sjc_gold_html_success(self) -> None:
        html = """
        <html><body>
          <table>
            <tr><th>Loại vàng</th><th>Mua vào</th><th>Bán ra</th></tr>
            <tr><td>SJC 1L - 10L</td><td>13.200.000</td><td>13.450.000</td></tr>
          </table>
        </body></html>
        """
        result = parse_sjc_gold_html(html)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.name, "SJC 1L - 10L")
        self.assertEqual(result.buy_raw, "13.200.000")
        self.assertEqual(result.sell, 13450000.0)
        self.assertEqual(result.sell_raw, "13.450.000")

    def test_parse_sjc_gold_html_skips_invalid_rows(self) -> None:
        html = """
        <html><body>
          <table>
            <tr><th>Loại vàng</th><th>Mua vào</th><th>Bán ra</th></tr>
            <tr><td>Header row</td><td>-</td><td>-</td></tr>
            <tr><td>SJC Nhẫn</td><td>13.000.000</td><td>13.100.000</td></tr>
          </table>
        </body></html>
        """
        result = parse_sjc_gold_html(html)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.name, "SJC Nhẫn")
        self.assertEqual(result.sell, 13100000.0)

    def test_parse_sjc_gold_html_no_table(self) -> None:
        self.assertIsNone(parse_sjc_gold_html("<html><body><p>Empty</p></body></html>"))


class TestGoldPriceFeature(unittest.TestCase):
    def _make_config(self, target_price: float = 13500000) -> GoldPriceConfig:
        return GoldPriceConfig(
            enabled=True,
            url="https://example.com/gold",
            target_price=target_price,
            check_interval=60,
        )

    def test_sends_alert_once_for_same_price_and_resets_when_price_rises(self) -> None:
        telegram = MagicMock()
        telegram.send_message.return_value = True

        prices = iter(
            [
                GoldPrice("SJC 1L", "13.100.000", 13400000.0, "13.400.000"),
                GoldPrice("SJC 1L", "13.100.000", 13400000.0, "13.400.000"),  # duplicate
                GoldPrice("SJC 1L", "13.500.000", 13800000.0, "13.800.000"),  # above target
                GoldPrice("SJC 1L", "13.100.000", 13400000.0, "13.400.000"),  # below again
            ]
        )
        feature = GoldPriceFeature(
            telegram=telegram,
            config=self._make_config(target_price=13500000),
            fetcher=lambda _url: next(prices),
        )

        feature.execute()
        feature.execute()
        self.assertEqual(telegram.send_message.call_count, 1)

        feature.execute()
        self.assertEqual(telegram.send_message.call_count, 1)

        feature.execute()
        self.assertEqual(telegram.send_message.call_count, 2)

    def test_persists_last_alerted_price_across_separate_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"
            telegram = MagicMock()
            telegram.send_message.return_value = True
            price_low = GoldPrice("SJC 1L", "13.100.000", 13400000.0, "13.400.000")
            price_high = GoldPrice("SJC 1L", "13.600.000", 13900000.0, "13.900.000")

            # Run 1: sends alert and persists price to disk
            feature_run_1 = GoldPriceFeature(
                telegram=telegram,
                config=self._make_config(target_price=13500000),
                fetcher=lambda _url: price_low,
                state_store=StateStore(state_path),
            )
            feature_run_1.execute()
            self.assertEqual(telegram.send_message.call_count, 1)

            # Run 2 (new process/runner): same price should NOT re-alert
            feature_run_2 = GoldPriceFeature(
                telegram=telegram,
                config=self._make_config(target_price=13500000),
                fetcher=lambda _url: price_low,
                state_store=StateStore(state_path),
            )
            feature_run_2.execute()
            self.assertEqual(telegram.send_message.call_count, 1)

            # Run 3: price rises above target -> clears persisted alert price
            feature_run_3 = GoldPriceFeature(
                telegram=telegram,
                config=self._make_config(target_price=13500000),
                fetcher=lambda _url: price_high,
                state_store=StateStore(state_path),
            )
            feature_run_3.execute()

            # Run 4: price drops back -> alerts again
            feature_run_4 = GoldPriceFeature(
                telegram=telegram,
                config=self._make_config(target_price=13500000),
                fetcher=lambda _url: price_low,
                state_store=StateStore(state_path),
            )
            feature_run_4.execute()
            self.assertEqual(telegram.send_message.call_count, 2)

    def test_raises_when_fetcher_returns_none(self) -> None:
        telegram = MagicMock()
        feature = GoldPriceFeature(
            telegram=telegram,
            config=self._make_config(target_price=13500000),
            fetcher=lambda _url: None,
        )
        with self.assertRaises(RuntimeError):
            feature.execute()


class TestScheduler(unittest.TestCase):
    def test_error_in_one_feature_does_not_block_other_features(self) -> None:
        telegram = MagicMock()
        ran: list[str] = []

        class FailingFeature(BaseFeature):
            @property
            def name(self) -> str:
                return "failing"

            @property
            def interval_seconds(self) -> int:
                return 10

            def execute(self) -> None:
                raise RuntimeError("boom")

        class HealthyFeature(BaseFeature):
            @property
            def name(self) -> str:
                return "healthy"

            @property
            def interval_seconds(self) -> int:
                return 10

            def execute(self) -> None:
                ran.append("healthy")

        scheduler = BotScheduler()
        scheduler.register(FailingFeature(telegram))
        scheduler.register(HealthyFeature(telegram))
        all_ok = scheduler.run_once()

        self.assertFalse(all_ok)
        self.assertEqual(ran, ["healthy"])

    def test_run_once_returns_true_when_all_features_succeed(self) -> None:
        telegram = MagicMock()

        class OkFeature(BaseFeature):
            @property
            def name(self) -> str:
                return "ok"

            @property
            def interval_seconds(self) -> int:
                return 10

            def execute(self) -> None:
                return None

        scheduler = BotScheduler()
        scheduler.register(OkFeature(telegram))
        self.assertTrue(scheduler.run_once())


class TestSettings(unittest.TestCase):
    def test_load_from_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TELEGRAM_TOKEN=test_token",
                        "TELEGRAM_CHAT_ID=123456",
                        "GOLD_TARGET_PRICE=14000000",
                        "GOLD_CHECK_INTERVAL=120",
                        "GOLD_ENABLED=false",
                    ]
                ),
                encoding="utf-8",
            )
            keys = [
                "TELEGRAM_TOKEN",
                "TELEGRAM_CHAT_ID",
                "GOLD_TARGET_PRICE",
                "GOLD_CHECK_INTERVAL",
                "GOLD_ENABLED",
                "STATE_FILE",
            ]
            backup = {k: os.environ.pop(k, None) for k in keys}
            try:
                settings = Settings.load(env_file=env_path)
                self.assertEqual(settings.telegram_token, "test_token")
                self.assertEqual(settings.telegram_chat_id, "123456")
                self.assertEqual(settings.gold_price.target_price, 14000000.0)
                self.assertEqual(settings.gold_price.check_interval, 120)
                self.assertFalse(settings.gold_price.enabled)
                self.assertEqual(settings.state_file, ".bot_state.json")
            finally:
                for k, v in backup.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_blank_env_vars_fall_back_to_defaults(self) -> None:
        keys = [
            "TELEGRAM_TOKEN",
            "TELEGRAM_CHAT_ID",
            "GOLD_TARGET_PRICE",
            "GOLD_CHECK_INTERVAL",
            "GOLD_ENABLED",
            "GOLD_URL",
            "LOG_LEVEL",
            "STATE_FILE",
        ]
        backup = {k: os.environ.pop(k, None) for k in keys}
        try:
            os.environ["TELEGRAM_TOKEN"] = "tok"
            os.environ["TELEGRAM_CHAT_ID"] = "999"
            os.environ["GOLD_TARGET_PRICE"] = ""
            os.environ["GOLD_CHECK_INTERVAL"] = ""
            os.environ["GOLD_ENABLED"] = ""
            os.environ["GOLD_URL"] = ""
            os.environ["LOG_LEVEL"] = ""
            os.environ["STATE_FILE"] = ""

            settings = Settings.load(env_file="nonexistent.env")
            self.assertEqual(settings.gold_price.target_price, 13500000.0)
            self.assertEqual(settings.gold_price.check_interval, 3600)
            self.assertTrue(settings.gold_price.enabled)
            self.assertEqual(settings.gold_price.url, "https://webgia.com/gia-vang/sjc/")
            self.assertEqual(settings.log_level, "INFO")
            self.assertEqual(settings.state_file, ".bot_state.json")
        finally:
            for k, v in backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
