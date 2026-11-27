"""Unit tests for the daily English vocabulary (daily_vocab) feature."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import requests

from bot.config import DailyVocabConfig, GoldPriceConfig, Settings, _parse_send_times
from bot.core.commands import CommandRouter
from bot.core.state import StateStore
from bot.core.webhook import build_command_router
from bot.features.daily_vocab.feature import DailyVocabFeature
from bot.features.daily_vocab.generator import (
    FALLBACK_VOCAB_BANK,
    build_fallback_batch,
    generate_vocab_with_gemini,
)
from bot.features.daily_vocab.models import (
    VocabBatch,
    VocabHistoryEntry,
    VocabItem,
    deserialize_history,
    merge_history,
    normalize_term,
    normalize_topic,
    serialize_history,
)


def _sample_item(term: str, topic: str = "it") -> VocabItem:
    return VocabItem(
        term=term,
        phonetic="/test/",
        part_of_speech="n",
        meaning_vi=f"Nghĩa của {term}",
        example_en=f"This is an example with {term}.",
        example_vi=f"Đây là ví dụ với {term}.",
        usage_note="Ghi chú dùng thử.",
        topic=topic,
    )


class TestDailyVocabModels(unittest.TestCase):
    def test_topic_and_term_normalization(self) -> None:
        self.assertEqual(normalize_topic("IT"), "it")
        self.assertEqual(normalize_topic("giao-tiep"), "communication")
        self.assertEqual(normalize_topic("giaotiep"), "communication")
        self.assertEqual(normalize_topic("IELTS"), "ielts")
        self.assertEqual(normalize_topic("all"), "mixed")
        self.assertIsNone(normalize_topic("cooking"))

        self.assertEqual(normalize_term("  Touch   Base! "), "touch base")
        self.assertEqual(normalize_term('"Bottleneck."'), "bottleneck")

    def test_history_serialization_merge_and_byte_cap(self) -> None:
        raw_json = json.dumps(
            [
                {"t": "bottleneck", "c": "it", "m": "Điểm nghẽn", "d": "2026-11-25"},
                {"term": "BOTTLENECK", "topic": "it", "meaning": "Duplicate"},
                "touch base",
                {"invalid": True},
            ]
        )
        parsed = deserialize_history(raw_json)
        self.assertEqual([e.term for e in parsed], ["bottleneck", "touch base"])
        self.assertEqual(parsed[0].topic, "it")
        self.assertEqual(parsed[0].meaning_vi, "Điểm nghẽn")

        # Malformed JSON returns empty list without crashing
        self.assertEqual(deserialize_history("{bad json"), [])

        # Merge deduplicates and caps max_entries
        merged = merge_history(
            parsed,
            [
                VocabHistoryEntry(term="Touch Base!", topic="communication", meaning_vi="Liên hệ"),
                VocabHistoryEntry(term="mitigate", topic="ielts", meaning_vi="Giảm nhẹ"),
                VocabHistoryEntry(term="rollback", topic="it", meaning_vi="Khôi phục"),
            ],
            max_entries=3,
        )
        self.assertEqual([e.term for e in merged], ["touch base", "mitigate", "rollback"])

        # Byte cap drops oldest entries cleanly
        many = [
            VocabHistoryEntry(
                term=f"word_{i}",
                topic="it",
                meaning_vi="Một mô tả nghĩa tiếng Việt khá dài để kiểm tra giới hạn byte",
                learned_date="2026-11-27",
            )
            for i in range(60)
        ]
        compact = serialize_history(many, max_bytes=1200)
        self.assertLessEqual(len(compact.encode("utf-8")), 1200)
        reloaded = deserialize_history(compact)
        self.assertTrue(len(reloaded) < 60)
        self.assertEqual(reloaded[-1].term, "word_59")


class TestDailyVocabGenerator(unittest.TestCase):
    def test_fallback_when_api_key_missing_respects_history(self) -> None:
        first_two = [item.term for item in FALLBACK_VOCAB_BANK["it"][:2]]
        batch = generate_vocab_with_gemini(
            topic="it",
            count=4,
            exclude_terms=first_two,
            api_key="",
        )
        self.assertFalse(batch.used_ai)
        self.assertEqual(batch.topic, "it")
        self.assertEqual(len(batch.items), 4)
        returned_terms = {normalize_term(i.term) for i in batch.items}
        for excluded in first_two:
            self.assertNotIn(normalize_term(excluded), returned_terms)

    def test_gemini_structured_json_filters_duplicates_and_history(self) -> None:
        gemini_json = {
            "items": [
                {
                    "term": "bottleneck",  # already in history -> must be filtered out
                    "phonetic": "/ˈbɒt.əl.nek/",
                    "part_of_speech": "n",
                    "meaning_vi": "Điểm nghẽn",
                    "example_en": "That query is a bottleneck.",
                    "example_vi": "Truy vấn đó là điểm nghẽn.",
                },
                {
                    "term": "circuit breaker",
                    "phonetic": "/ˈsɜːr.kɪt ˈbreɪ.kər/",
                    "part_of_speech": "n phrase",
                    "meaning_vi": "Bộ ngắt mạch bảo vệ dịch vụ khỏi lỗi dây chuyền",
                    "example_en": "A circuit breaker stops requests to a failing payment service.",
                    "example_vi": "Bộ ngắt mạch chặn yêu cầu tới dịch vụ thanh toán đang lỗi.",
                    "usage_note": "Mẫu thiết kế quen thuộc trong microservices.",
                },
                {
                    "term": "Circuit Breaker",  # duplicate within same AI batch -> filtered
                    "phonetic": "/ˈsɜːr.kɪt ˈbreɪ.kər/",
                    "part_of_speech": "n phrase",
                    "meaning_vi": "Trùng lặp",
                    "example_en": "Duplicate.",
                    "example_vi": "Trùng.",
                },
                {
                    "term": "observability",
                    "phonetic": "/əbˌzɜːr.vəˈbɪl.ə.ti/",
                    "part_of_speech": "n",
                    "meaning_vi": "Khả năng quan sát trạng thái bên trong hệ thống qua log/metric/trace",
                    "example_en": "Good observability reduces the time needed to diagnose outages.",
                    "example_vi": "Khả năng quan sát tốt giúp giảm thời gian chẩn đoán sự cố.",
                    "usage_note": "3 trụ cột: logs, metrics, traces.",
                },
                {
                    "term": "backpressure",
                    "phonetic": "/ˈbækˌpreʃ.ər/",
                    "part_of_speech": "n",
                    "meaning_vi": "Áp lực ngược khi bên nhận không xử lý kịp tốc độ bên gửi",
                    "example_en": "The stream consumer applies backpressure when its buffer fills up.",
                    "example_vi": "Bên tiêu thụ luồng áp dụng áp lực ngược khi bộ đệm đầy.",
                    "usage_note": "Hay gặp trong hệ thống streaming / Kafka.",
                },
            ]
        }
        session = MagicMock()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(gemini_json)}]}}]
        }
        session.post.return_value = resp

        batch = generate_vocab_with_gemini(
            topic="it",
            count=3,
            exclude_terms=("bottleneck",),
            api_key="test-key",
            session=session,
        )
        self.assertTrue(batch.used_ai)
        self.assertEqual([i.term for i in batch.items], ["circuit breaker", "observability", "backpressure"])
        # Verify excluded term was passed in prompt
        sent_body = session.post.call_args.kwargs["json"]
        prompt_text = sent_body["contents"][0]["parts"][0]["text"]
        self.assertIn("bottleneck", prompt_text)

    def test_gemini_http_error_or_network_error_falls_back_cleanly(self) -> None:
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=429, text="Quota exceeded")

        batch = generate_vocab_with_gemini(
            topic="ielts",
            count=4,
            exclude_terms=("mitigate",),
            api_key="test-key",
            session=session,
        )
        self.assertFalse(batch.used_ai)
        self.assertIn("429", batch.fallback_reason)
        self.assertEqual(len(batch.items), 4)
        self.assertNotIn("mitigate", [normalize_term(i.term) for i in batch.items])

        # Network exception
        session.post.side_effect = requests.ConnectionError("offline")
        batch_net = generate_vocab_with_gemini(
            topic="communication",
            count=3,
            api_key="test-key",
            session=session,
        )
        self.assertFalse(batch_net.used_ai)
        self.assertEqual(len(batch_net.items), 3)

    def test_gemini_404_auto_falls_back_to_active_model(self) -> None:
        resp_404 = MagicMock(status_code=404, text="model no longer available")
        resp_ok = MagicMock(status_code=200)
        resp_ok.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "items": [
                                            {
                                                "term": "refactor",
                                                "phonetic": "/ˌriːˈfæk.tər/",
                                                "part_of_speech": "v",
                                                "meaning_vi": "Tái cấu trúc mã nguồn",
                                                "example_en": "We should refactor this module.",
                                                "example_vi": "Chúng ta nên tái cấu trúc mô-đun này.",
                                            }
                                        ]
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }
        session = MagicMock()
        session.post.side_effect = [resp_404, resp_ok]

        batch = generate_vocab_with_gemini(
            topic="it",
            count=1,
            api_key="test-key",
            model="gemini-2.5-flash",
            session=session,
        )
        self.assertTrue(batch.used_ai)
        self.assertEqual([i.term for i in batch.items], ["refactor"])
        self.assertEqual(session.post.call_count, 2)
        self.assertIn("gemini-2.5-flash", session.post.call_args_list[0].args[0])
        self.assertIn("gemini-3.5-flash-lite", session.post.call_args_list[1].args[0])

    def test_fallback_handles_exhausted_topic_bank(self) -> None:
        all_it = [item.term for item in FALLBACK_VOCAB_BANK["it"]]
        batch = build_fallback_batch("it", count=4, exclude_terms=all_it)
        self.assertEqual(len(batch.items), 4)
        # Should pick unseen words from other topics instead of crashing
        for item in batch.items:
            self.assertNotIn(normalize_term(item.term), {normalize_term(t) for t in all_it})


class TestDailyVocabFeature(unittest.TestCase):
    def _make_config(
        self,
        *,
        topic: str = "mixed",
        words_per_batch: int = 3,
        send_times: tuple[tuple[int, int], ...] = ((8, 0), (20, 0)),
        gemini_api_key: str = "fake-key",
    ) -> DailyVocabConfig:
        return DailyVocabConfig(
            enabled=True,
            topic=topic,
            words_per_batch=words_per_batch,
            send_times=send_times,
            timezone="Asia/Ho_Chi_Minh",
            check_interval=60,
            max_history=300,
            gemini_api_key=gemini_api_key,
            gemini_model="gemini-2.5-flash",
        )

    def _make_update(self, text: str, chat_id: str = "123456") -> dict:
        return {
            "update_id": 1,
            "message": {"chat": {"id": int(chat_id)}, "text": text},
        }

    def test_morning_and_evening_schedule_deduplication_and_mixed_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"
            telegram = MagicMock()
            telegram.send_message.return_value = True

            calls: list[tuple[str, int, tuple[str, ...]]] = []
            counter = [0]

            def fake_generator(topic: str, count: int, excludes: tuple[str, ...]) -> VocabBatch:
                calls.append((topic, count, excludes))
                items = []
                for _ in range(count):
                    counter[0] += 1
                    items.append(_sample_item(f"{topic}_word_{counter[0]}", topic=topic))
                return VocabBatch(topic=topic, items=tuple(items), used_ai=True)

            tz = ZoneInfo("Asia/Ho_Chi_Minh")
            current_now = [datetime(2026, 11, 27, 7, 30, tzinfo=tz)]

            feature = DailyVocabFeature(
                telegram=telegram,
                config=self._make_config(topic="mixed", words_per_batch=3),
                generator=fake_generator,
                state_store=StateStore(state_path),
                now_provider=lambda _tz: current_now[0],
            )

            # 07:30 -> before 08:00 -> skipped
            feature.execute()
            self.assertEqual(telegram.send_message.call_count, 0)

            # 08:05 -> sends morning batch (topic rotates starting with 'it')
            current_now[0] = datetime(2026, 11, 27, 8, 5, tzinfo=tz)
            feature.execute()
            self.assertEqual(telegram.send_message.call_count, 1)
            self.assertEqual(calls[0][0], "it")
            self.assertEqual(calls[0][2], ())
            morning_msg = telegram.send_message.call_args.args[0]
            self.assertIn("BUỔI SÁNG", morning_msg)
            self.assertIn("IT &amp; Công nghệ", morning_msg)

            # 09:17 -> morning slot already sent -> skipped
            current_now[0] = datetime(2026, 11, 27, 9, 17, tzinfo=tz)
            feature.execute()
            self.assertEqual(telegram.send_message.call_count, 1)

            # 20:05 (new process reloading StateStore) -> sends evening batch (topic rotates to 'communication')
            current_now[0] = datetime(2026, 11, 27, 20, 5, tzinfo=tz)
            feature_evening = DailyVocabFeature(
                telegram=telegram,
                config=self._make_config(topic="mixed", words_per_batch=3),
                generator=fake_generator,
                state_store=StateStore(state_path),
                now_provider=lambda _tz: current_now[0],
            )
            feature_evening.execute()
            self.assertEqual(telegram.send_message.call_count, 2)
            self.assertEqual(calls[1][0], "communication")
            # History from morning was passed as exclude_terms!
            self.assertEqual(calls[1][2], ("it_word_1", "it_word_2", "it_word_3"))
            evening_msg = telegram.send_message.call_args.args[0]
            self.assertIn("BUỔI TỐI", evening_msg)

            # 21:00 -> evening slot already sent -> skipped
            current_now[0] = datetime(2026, 11, 27, 21, 0, tzinfo=tz)
            feature_evening.execute()
            self.assertEqual(telegram.send_message.call_count, 2)

            # Next day 08:00 -> sends morning batch (topic rotates to 'ielts')
            current_now[0] = datetime(2026, 11, 28, 8, 0, tzinfo=tz)
            feature_evening.execute()
            self.assertEqual(telegram.send_message.call_count, 3)
            self.assertEqual(calls[2][0], "ielts")
            self.assertEqual(len(calls[2][2]), 6)

    def test_first_run_in_evening_only_sends_once(self) -> None:
        telegram = MagicMock()
        telegram.send_message.return_value = True
        tz = ZoneInfo("Asia/Ho_Chi_Minh")

        feature = DailyVocabFeature(
            telegram=telegram,
            config=self._make_config(topic="it", words_per_batch=3),
            generator=lambda topic, count, _ex: VocabBatch(
                topic=topic,
                items=tuple(_sample_item(f"w{i}", topic) for i in range(count)),
                used_ai=True,
            ),
            now_provider=lambda _tz: datetime(2026, 11, 27, 20, 15, tzinfo=tz),
        )
        feature.execute()
        feature.execute()
        self.assertEqual(telegram.send_message.call_count, 1)
        self.assertIn("BUỔI TỐI", telegram.send_message.call_args.args[0])

    def test_failed_telegram_send_does_not_record_history_or_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"
            telegram = MagicMock()
            telegram.send_message.return_value = False
            tz = ZoneInfo("Asia/Ho_Chi_Minh")

            feature = DailyVocabFeature(
                telegram=telegram,
                config=self._make_config(topic="it", words_per_batch=3),
                generator=lambda topic, count, _ex: VocabBatch(
                    topic=topic,
                    items=tuple(_sample_item(f"w{i}", topic) for i in range(count)),
                    used_ai=True,
                ),
                state_store=StateStore(state_path),
                now_provider=lambda _tz: datetime(2026, 11, 27, 8, 10, tzinfo=tz),
            )
            with self.assertRaises(RuntimeError):
                feature.execute()

            self.assertEqual(feature.get_history(), [])
            self.assertIsNone(StateStore(state_path).get("daily_vocab", "last_sent_slot"))

    def test_slash_commands_with_github_vars_sync(self) -> None:
        telegram = MagicMock()
        stored: dict[str, str] = {}
        github_vars = MagicMock()
        github_vars.repository = "ndtruongg/telegram-bot"
        github_vars.get_variable.side_effect = lambda name: stored.get(name)

        def _set_var(name: str, val: str) -> bool:
            stored[name] = val
            return True

        github_vars.set_variable.side_effect = _set_var

        tz = ZoneInfo("Asia/Ho_Chi_Minh")
        gen_calls: list[tuple[str, int, tuple[str, ...]]] = []

        def fake_generator(topic: str, count: int, excludes: tuple[str, ...]) -> VocabBatch:
            gen_calls.append((topic, count, excludes))
            base = len(gen_calls) * 10
            return VocabBatch(
                topic=topic,
                items=tuple(_sample_item(f"{topic}_{base + i}", topic) for i in range(count)),
                used_ai=True,
            )

        feature = DailyVocabFeature(
            telegram=telegram,
            config=self._make_config(topic="mixed", words_per_batch=4),
            generator=fake_generator,
            github_vars=github_vars,
            now_provider=lambda _tz: datetime(2026, 11, 27, 20, 0, tzinfo=tz),
        )
        router = CommandRouter()
        feature.register_commands(router)

        # /vocab_topic ielts -> persists VOCAB_TOPIC
        res = router.handle_update(self._make_update("/vocab_topic ielts"), allowed_chat_id="123456")
        assert res is not None
        self.assertEqual(stored.get("VOCAB_TOPIC"), "ielts")
        self.assertIn("IELTS", res[1])

        # /vocab_topic invalid -> returns error
        res = router.handle_update(self._make_update("/vocab_topic invalid"), allowed_chat_id="123456")
        assert res is not None
        self.assertIn("không hợp lệ", res[1])

        # /vocab_now -> uses active topic 'ielts' with 4 words, persists VOCAB_HISTORY
        res = router.handle_update(self._make_update("/vocab_now"), allowed_chat_id="123456")
        assert res is not None
        self.assertEqual(gen_calls[-1][0], "ielts")
        self.assertEqual(gen_calls[-1][1], 4)
        self.assertIn("VOCAB_HISTORY", stored)
        self.assertEqual(len(deserialize_history(stored["VOCAB_HISTORY"])), 4)

        # /vocab_now giaotiep 3 -> overrides topic & count, passes previous 4 words as excludes
        res = router.handle_update(self._make_update("/vocab_now giaotiep 3"), allowed_chat_id="123456")
        assert res is not None
        self.assertEqual(gen_calls[-1][0], "communication")
        self.assertEqual(gen_calls[-1][1], 3)
        self.assertEqual(len(gen_calls[-1][2]), 4)
        self.assertEqual(len(deserialize_history(stored["VOCAB_HISTORY"])), 7)

        # /vocab_history 5 -> shows recent learned words
        res = router.handle_update(self._make_update("/vocab_history 5"), allowed_chat_id="123456")
        assert res is not None
        self.assertIn("7 từ", res[1])
        self.assertIn("communication_22", res[1])

        # /vocab_status -> shows config and history stats
        res = router.handle_update(self._make_update("/vocab_status"), allowed_chat_id="123456")
        assert res is not None
        self.assertIn("08:00, 20:00", res[1])
        self.assertIn("7 / 300", res[1])

        # /vocab_reset -> clears VOCAB_HISTORY
        res = router.handle_update(self._make_update("/vocab_reset"), allowed_chat_id="123456")
        assert res is not None
        self.assertIn("7 từ", res[1])
        self.assertEqual(deserialize_history(stored["VOCAB_HISTORY"]), [])

    def test_settings_load_and_command_router_integration(self) -> None:
        self.assertEqual(_parse_send_times("20:30, 8:00, 08:00"), ((8, 0), (20, 30)))
        self.assertEqual(_parse_send_times("7h15, 21h00"), ((7, 15), (21, 0)))
        self.assertEqual(_parse_send_times("bad"), ((8, 0), (20, 0)))

        keys = [
            "TELEGRAM_TOKEN",
            "TELEGRAM_CHAT_ID",
            "VOCAB_ENABLED",
            "VOCAB_TOPIC",
            "VOCAB_WORDS_PER_BATCH",
            "VOCAB_SEND_TIMES",
            "VOCAB_TIMEZONE",
            "VOCAB_MAX_HISTORY",
            "GEMINI_API_KEY",
            "GEMINI_MODEL",
        ]
        backup = {k: os.environ.pop(k, None) for k in keys}
        try:
            os.environ["TELEGRAM_TOKEN"] = "tok"
            os.environ["TELEGRAM_CHAT_ID"] = "123"
            os.environ["VOCAB_ENABLED"] = "true"
            os.environ["VOCAB_TOPIC"] = "giaotiep"
            os.environ["VOCAB_WORDS_PER_BATCH"] = "5"
            os.environ["VOCAB_SEND_TIMES"] = "07:30,21:15"
            os.environ["VOCAB_MAX_HISTORY"] = "250"
            os.environ["GEMINI_API_KEY"] = "key-xyz"

            settings = Settings.load(env_file="nonexistent.env")
            self.assertTrue(settings.daily_vocab.enabled)
            self.assertEqual(settings.daily_vocab.topic, "communication")
            self.assertEqual(settings.daily_vocab.words_per_batch, 5)
            self.assertEqual(settings.daily_vocab.send_times, ((7, 30), (21, 15)))
            self.assertEqual(settings.daily_vocab.max_history, 250)
            self.assertEqual(settings.daily_vocab.gemini_api_key, "key-xyz")

            router = build_command_router(settings=settings, telegram=MagicMock())
            cmd_names = {c["command"] for c in router.get_telegram_commands()}
            self.assertTrue(
                {"vocab_now", "vocab_topic", "vocab_history", "vocab_reset", "vocab_status"}
                <= cmd_names
            )
        finally:
            for k, v in backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
