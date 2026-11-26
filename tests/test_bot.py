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
        scheduler.run_once()

        self.assertEqual(ran, ["healthy"])


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
            ]
            backup = {k: os.environ.pop(k, None) for k in keys}
            try:
                settings = Settings.load(env_file=env_path)
                self.assertEqual(settings.telegram_token, "test_token")
                self.assertEqual(settings.telegram_chat_id, "123456")
                self.assertEqual(settings.gold_price.target_price, 14000000.0)
                self.assertEqual(settings.gold_price.check_interval, 120)
                self.assertFalse(settings.gold_price.enabled)
            finally:
                for k, v in backup.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
