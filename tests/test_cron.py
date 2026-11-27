"""Unit tests for HTTP /api/cron endpoint and GitHub-synced StateStore."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from bot.config import GoldPriceConfig, Settings
from bot.core.feature import BaseFeature
from bot.core.state import StateStore
from bot.core.webhook import process_cron_request, reset_cron_schedule_cache


class _DummyFeature(BaseFeature):
    def __init__(self, telegram, feature_name: str, interval: int, fail: bool = False) -> None:
        super().__init__(telegram)
        self._name = feature_name
        self._interval = interval
        self._fail = fail
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def interval_seconds(self) -> int:
        return self._interval

    def execute(self) -> None:
        self.calls += 1
        if self._fail:
            raise RuntimeError("boom")


class TestStateStoreGitHubSync(unittest.TestCase):
    def test_syncs_read_and_write_with_github_variable(self) -> None:
        stored: dict[str, str] = {
            "BOT_STATE_JSON": json.dumps({"morning_news": {"last_sent_date": "2026-11-27"}})
        }
        github_vars = MagicMock()
        github_vars.get_variable.side_effect = lambda name: stored.get(name)

        def _set_var(name: str, val: str) -> bool:
            stored[name] = val
            return True

        github_vars.set_variable.side_effect = _set_var

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"
            store = StateStore(state_path, github_vars=github_vars)
            self.assertEqual(store.get("morning_news", "last_sent_date"), "2026-11-27")

            # Setting identical value should not trigger GitHub write
            store.set("morning_news", "last_sent_date", "2026-11-27")
            github_vars.set_variable.assert_not_called()

            # Setting new value writes compact JSON to BOT_STATE_JSON
            store.set("reminders", "sent_notifications", {"1:2026-11-27:at_time": "2026-11-27"})
            github_vars.set_variable.assert_called_once()
            saved = json.loads(stored["BOT_STATE_JSON"])
            self.assertEqual(saved["morning_news"]["last_sent_date"], "2026-11-27")
            self.assertIn("1:2026-11-27:at_time", saved["reminders"]["sent_notifications"])

    def test_merges_initial_env_json_with_local_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "morning_news": {"last_sent_date": "2026-11-26"},
                        "reminders": {"sent_notifications": {"1:2026-11-26:d0": "2026-11-26"}},
                    }
                ),
                encoding="utf-8",
            )
            env_json = json.dumps(
                {
                    "morning_news": {"last_sent_date": "2026-11-27"},
                    "reminders": {"sent_notifications": {"2:2026-11-27:at_time": "2026-11-27"}},
                }
            )
            store = StateStore(state_path, initial_json=env_json)
            self.assertEqual(store.get("morning_news", "last_sent_date"), "2026-11-27")
            sent = store.get("reminders", "sent_notifications")
            self.assertIn("1:2026-11-26:d0", sent)
            self.assertIn("2:2026-11-27:at_time", sent)


class TestCronEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        reset_cron_schedule_cache()

    def _make_settings(self, *, cron_secret: str = "", webhook_secret: str = "sec123") -> Settings:
        return Settings(
            telegram_token="tok",
            telegram_chat_id="123",
            log_level="INFO",
            gold_price=GoldPriceConfig(
                enabled=True,
                url="https://example.com/gold",
                target_price=13500000,
                check_interval=3600,
            ),
            telegram_webhook_secret=webhook_secret,
            cron_secret=cron_secret,
        )

    def test_auth_checks_header_bearer_and_query_secret(self) -> None:
        settings = self._make_settings(webhook_secret="my-secret")
        telegram = MagicMock()
        feat = _DummyFeature(telegram, "reminders", interval=60)

        # Missing secret -> 403
        status, _ = process_cron_request("", {}, settings=settings, telegram=telegram, features=[feat])
        self.assertEqual(status, 403)

        # Wrong secret -> 403
        status, _ = process_cron_request(
            "secret=wrong", {}, settings=settings, telegram=telegram, features=[feat]
        )
        self.assertEqual(status, 403)

        # Valid query secret -> 200
        status, payload = process_cron_request(
            "secret=my-secret&force=1", {}, settings=settings, telegram=telegram, features=[feat]
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["ran"], ["reminders"])

        # Valid X-Cron-Secret header -> 200
        status, _ = process_cron_request(
            "force=1",
            {"X-Cron-Secret": "my-secret"},
            settings=settings,
            telegram=telegram,
            features=[feat],
        )
        self.assertEqual(status, 200)

        # Valid Bearer token -> 200
        status, _ = process_cron_request(
            "force=1",
            {"Authorization": "Bearer my-secret"},
            settings=settings,
            telegram=telegram,
            features=[feat],
        )
        self.assertEqual(status, 200)
        self.assertEqual(feat.calls, 3)

    def test_interval_throttling_and_explicit_feature_filter(self) -> None:
        settings = self._make_settings(cron_secret="cron-ok")
        telegram = MagicMock()
        reminders = _DummyFeature(telegram, "reminders", interval=60)
        gold = _DummyFeature(telegram, "gold_price", interval=3600)

        now_val = [1000.0]

        # Tick 1 at t=1000: both run
        status, payload = process_cron_request(
            "secret=cron-ok",
            {},
            settings=settings,
            telegram=telegram,
            features=[reminders, gold],
            monotonic_provider=lambda: now_val[0],
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["ran"], ["reminders", "gold_price"])
        self.assertEqual(payload["skipped"], [])

        # Tick 2 at t=1300 (+5 min): reminders runs again, gold_price is throttled
        now_val[0] = 1300.0
        status, payload = process_cron_request(
            "secret=cron-ok",
            {},
            settings=settings,
            telegram=telegram,
            features=[reminders, gold],
            monotonic_provider=lambda: now_val[0],
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["ran"], ["reminders"])
        self.assertEqual(payload["skipped"], ["gold_price"])

        # Explicit ?feature=gold_price bypasses throttling and runs immediately
        status, payload = process_cron_request(
            "secret=cron-ok&feature=gold_price",
            {},
            settings=settings,
            telegram=telegram,
            features=[reminders, gold],
            monotonic_provider=lambda: now_val[0],
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["ran"], ["gold_price"])
        self.assertEqual(gold.calls, 2)

        # Unknown feature returns 400
        status, payload = process_cron_request(
            "secret=cron-ok&feature=nonexistent",
            {},
            settings=settings,
            telegram=telegram,
            features=[reminders, gold],
        )
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])

    def test_failed_feature_reports_500_without_blocking_others(self) -> None:
        settings = self._make_settings(cron_secret="cron-ok")
        telegram = MagicMock()
        bad = _DummyFeature(telegram, "gold_price", interval=60, fail=True)
        good = _DummyFeature(telegram, "reminders", interval=60, fail=False)

        status, payload = process_cron_request(
            "secret=cron-ok",
            {},
            settings=settings,
            telegram=telegram,
            features=[bad, good],
        )
        self.assertEqual(status, 500)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failed"], ["gold_price"])
        self.assertEqual(payload["ran"], ["reminders"])


if __name__ == "__main__":
    unittest.main()
