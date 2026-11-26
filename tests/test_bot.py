"""Unit tests for parsers, config, scraper, scheduler, and gold price feature."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from bot.config import GoldPriceConfig, Settings
from bot.core.commands import CommandRouter
from bot.core.feature import BaseFeature
from bot.core.github_vars import GitHubVariablesClient
from bot.core.scheduler import BotScheduler
from bot.core.state import StateStore
from bot.core.webhook import process_webhook_request
from bot.features.gold_price.feature import GoldPriceFeature
from bot.features.gold_price.scraper import GoldPrice, parse_sjc_gold_html
from bot.utils.parsers import format_price, parse_price, parse_user_price, serialize_price


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
        self.assertEqual(format_price(84.5), "84.5")
        self.assertEqual(format_price(84.55), "84.55")

    def test_parse_user_price_and_serialize(self) -> None:
        self.assertEqual(parse_user_price("13450000"), 13450000.0)
        self.assertEqual(parse_user_price("13.450.000"), 13450000.0)
        self.assertEqual(parse_user_price("13,450,000"), 13450000.0)
        self.assertEqual(parse_user_price("84.5"), 84.5)
        self.assertEqual(parse_user_price("84,5"), 84.5)
        self.assertEqual(parse_user_price("84.55"), 84.55)
        self.assertEqual(parse_user_price("13450000đ"), 13450000.0)
        self.assertIsNone(parse_user_price("0"))
        self.assertIsNone(parse_user_price("-100"))
        self.assertIsNone(parse_user_price("abc"))

        self.assertEqual(serialize_price(13450000.0), "13450000")
        self.assertEqual(serialize_price(84.5), "84.5")


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

    def test_target_change_between_runs_resets_spam_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"
            telegram = MagicMock()
            telegram.send_message.return_value = True
            price_low = GoldPrice("SJC 1L", "13.100.000", 13400000.0, "13.400.000")

            # Run 1 with target 13,500,000 -> alerts
            GoldPriceFeature(
                telegram=telegram,
                config=self._make_config(target_price=13500000),
                fetcher=lambda _url: price_low,
                state_store=StateStore(state_path),
            ).execute()
            self.assertEqual(telegram.send_message.call_count, 1)

            # Run 2 with updated target 14,000,000 -> resets dedupe and alerts again
            GoldPriceFeature(
                telegram=telegram,
                config=self._make_config(target_price=14000000),
                fetcher=lambda _url: price_low,
                state_store=StateStore(state_path),
            ).execute()
            self.assertEqual(telegram.send_message.call_count, 2)


class TestCommandsAndWebhook(unittest.TestCase):
    def _make_config(self, target_price: float = 13500000) -> GoldPriceConfig:
        return GoldPriceConfig(
            enabled=True,
            url="https://example.com/gold",
            target_price=target_price,
            check_interval=3600,
        )

    def _make_update(self, text: str, chat_id: str = "123456") -> dict:
        return {
            "update_id": 1,
            "message": {
                "chat": {"id": int(chat_id)},
                "text": text,
            },
        }

    def test_help_and_gold_commands_with_github_vars(self) -> None:
        telegram = MagicMock()
        github_vars = MagicMock()
        github_vars.repository = "ndtruongg/telegram-bot"
        github_vars.get_variable.return_value = "13500000"
        github_vars.set_variable.return_value = True

        feature = GoldPriceFeature(
            telegram=telegram,
            config=self._make_config(target_price=13500000),
            fetcher=lambda _url: GoldPrice("SJC 1L", "13.100.000", 13400000.0, "13.400.000"),
            github_vars=github_vars,
        )
        router = CommandRouter()
        feature.register_commands(router)

        # /start & /help list all commands and escape angle brackets for Telegram HTML
        res = router.handle_update(self._make_update("/help"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("/gold_check", res[1])
        self.assertIn("/gold_set &lt;số tiền&gt;", res[1])
        self.assertNotIn("<số tiền>", res[1])
        self.assertIn("/gold_status", res[1])

        # /gold-check (hyphen normalized to /gold_check)
        res = router.handle_update(self._make_update("/gold-check"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("13.400.000", res[1])
        self.assertIn("Đã đạt ngưỡng mục tiêu", res[1])

        # /gold_set updates GitHub Variable
        res = router.handle_update(self._make_update("/gold_set 84.5"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        github_vars.set_variable.assert_called_once_with("GOLD_TARGET_PRICE", "84.5")
        self.assertIn("84.5", res[1])

        # /gold_status reads latest GitHub Variable
        github_vars.get_variable.return_value = "84.5"
        res = router.handle_update(self._make_update("/gold_status"), allowed_chat_id="123456")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("84.5", res[1])

        # Unauthorized chat_id is ignored
        self.assertIsNone(
            router.handle_update(self._make_update("/gold_set 100"), allowed_chat_id="999999")
        )

    def test_webhook_secret_validation_and_dispatch(self) -> None:
        settings = Settings(
            telegram_token="tok",
            telegram_chat_id="123456",
            log_level="INFO",
            gold_price=self._make_config(),
            telegram_webhook_secret="my-secret",
        )
        telegram = MagicMock()
        router = CommandRouter()

        body = json.dumps(self._make_update("/start")).encode("utf-8")

        # Wrong secret -> 403
        status, _ = process_webhook_request(
            body,
            {"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            settings=settings,
            telegram=telegram,
            router=router,
        )
        self.assertEqual(status, 403)
        telegram.send_message.assert_not_called()

        # Valid secret -> 200 and sends reply
        status, payload = process_webhook_request(
            body,
            {"x-telegram-bot-api-secret-token": "my-secret"},
            settings=settings,
            telegram=telegram,
            router=router,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        telegram.send_message.assert_called_once()


class TestGitHubVariablesClient(unittest.TestCase):
    def test_set_variable_creates_when_missing(self) -> None:
        session = MagicMock()
        patch_resp = MagicMock(status_code=404, text="Not Found")
        post_resp = MagicMock(status_code=201, text="")
        session.patch.return_value = patch_resp
        session.post.return_value = post_resp

        client = GitHubVariablesClient("ghp_test", "ndtruongg/telegram-bot", session=session)
        self.assertTrue(client.set_variable("GOLD_TARGET_PRICE", "13450000"))
        session.patch.assert_called_once()
        session.post.assert_called_once()

    def test_get_variable_returns_value_or_none(self) -> None:
        session = MagicMock()
        get_resp = MagicMock(status_code=200)
        get_resp.json.return_value = {"name": "GOLD_TARGET_PRICE", "value": "84.5"}
        session.get.return_value = get_resp

        client = GitHubVariablesClient("ghp_test", "ndtruongg/telegram-bot", session=session)
        self.assertEqual(client.get_variable("GOLD_TARGET_PRICE"), "84.5")


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
