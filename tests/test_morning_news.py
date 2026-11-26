"""Unit tests for morning news RSS parser, Gemini summarizer, feature, and config."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from bot.config import DEFAULT_NEWS_RSS_URLS, MorningNewsConfig, Settings
from bot.core.commands import CommandRouter
from bot.core.state import StateStore
from bot.features.morning_news.feature import MorningNewsFeature
from bot.features.morning_news.rss import NewsArticle, fetch_news_articles, parse_rss_xml
from bot.features.morning_news.summarizer import (
    NewsDigest,
    SummarizedItem,
    summarize_with_gemini,
)


class TestNewsRssParser(unittest.TestCase):
    def test_parse_vnexpress_rss_strips_html_description(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>Tin nổi bật - VnExpress RSS</title>
            <item>
              <title>Kinh tế Việt Nam tăng trưởng mạnh quý I &amp; triển vọng</title>
              <description><![CDATA[<a href="https://vnexpress.net/a"><img src="x.jpg"/></a></br>GDP đạt mức tăng cao nhờ xuất khẩu &amp; đầu tư công.]]></description>
              <pubDate>Thu, 26 Nov 2026 06:00:00 +0700</pubDate>
              <link>https://vnexpress.net/kinh-te-1.html</link>
            </item>
          </channel>
        </rss>
        """
        articles = parse_rss_xml(xml, feed_url="https://vnexpress.net/rss/tin-noi-bat.rss")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].source, "VnExpress")
        self.assertEqual(articles[0].title, "Kinh tế Việt Nam tăng trưởng mạnh quý I & triển vọng")
        self.assertEqual(articles[0].summary, "GDP đạt mức tăng cao nhờ xuất khẩu & đầu tư công.")
        self.assertEqual(articles[0].link, "https://vnexpress.net/kinh-te-1.html")

    def test_parse_atom_feed(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <title>Custom Feed - Daily</title>
          <entry>
            <title>Tin công nghệ nổi bật</title>
            <link href="https://example.com/tech-1" />
            <summary>AI mới hỗ trợ tiếng Việt tốt hơn.</summary>
          </entry>
        </feed>
        """
        articles = parse_rss_xml(xml, feed_url="https://example.com/feed.xml")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].source, "Custom Feed")
        self.assertEqual(articles[0].link, "https://example.com/tech-1")

    def test_parse_invalid_xml_returns_empty(self) -> None:
        self.assertEqual(parse_rss_xml("<rss><broken>"), [])

    def test_fetch_news_articles_interleaves_and_deduplicates(self) -> None:
        feed_a = """<rss><channel><title>VnExpress</title>
          <item><title>Tin A1</title><link>https://vnexpress.net/1</link><description>Desc A1</description></item>
          <item><title>Tin A2</title><link>https://vnexpress.net/2</link><description>Desc A2</description></item>
        </channel></rss>"""
        feed_b = """<rss><channel><title>Tuổi Trẻ</title>
          <item><title>Tin B1</title><link>https://tuoitre.vn/1</link><description>Desc B1</description></item>
          <item><title>Tin A1</title><link>https://tuoitre.vn/dup</link><description>Duplicate title</description></item>
        </channel></rss>"""

        session = MagicMock()
        resp_a = MagicMock(status_code=200, text=feed_a, encoding="utf-8")
        resp_b = MagicMock(status_code=200, text=feed_b, encoding="utf-8")
        session.get.side_effect = [resp_a, resp_b]

        articles = fetch_news_articles(
            [
                "https://vnexpress.net/rss/tin-noi-bat.rss",
                "https://tuoitre.vn/rss/tin-moi-nhat.rss",
            ],
            max_articles=5,
            session=session,
        )
        self.assertEqual([a.title for a in articles], ["Tin A1", "Tin B1", "Tin A2"])
        self.assertEqual([a.source for a in articles], ["VnExpress", "Tuổi Trẻ", "VnExpress"])


class TestGeminiSummarizer(unittest.TestCase):
    def _sample_articles(self) -> list[NewsArticle]:
        return [
            NewsArticle(
                title="Tin số 1",
                link="https://vnexpress.net/1",
                summary="Mô tả tin số 1",
                source="VnExpress",
            ),
            NewsArticle(
                title="Tin số 2",
                link="https://tuoitre.vn/2",
                summary="Mô tả tin số 2",
                source="Tuổi Trẻ",
            ),
        ]

    def test_fallback_when_api_key_missing(self) -> None:
        digest = summarize_with_gemini(self._sample_articles(), api_key="")
        self.assertFalse(digest.used_ai)
        self.assertEqual(len(digest.items), 2)
        self.assertIn("GEMINI_API_KEY", digest.fallback_reason)

    def test_gemini_structured_json_success(self) -> None:
        session = MagicMock()
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "overview": "Sáng nay nổi bật với 2 tin chính.",
                                        "items": [
                                            {
                                                "index": 2,
                                                "headline": "Điểm tin 2 ngắn gọn",
                                                "summary": "Tóm tắt súc tích tin số 2.",
                                            },
                                            {
                                                "index": 99,  # invalid index -> skipped
                                                "headline": "Bịa đặt",
                                                "summary": "Nên bị bỏ qua",
                                            },
                                            {
                                                "index": 1,
                                                "headline": "Điểm tin 1 ngắn gọn",
                                                "summary": "Tóm tắt súc tích tin số 1.",
                                            },
                                        ],
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }
        session.post.return_value = mock_resp

        digest = summarize_with_gemini(
            self._sample_articles(),
            api_key="test-key",
            model="gemini-2.5-flash",
            session=session,
        )
        self.assertTrue(digest.used_ai)
        self.assertEqual(digest.overview, "Sáng nay nổi bật với 2 tin chính.")
        self.assertEqual(len(digest.items), 2)
        self.assertEqual(digest.items[0].article.link, "https://tuoitre.vn/2")
        self.assertEqual(digest.items[1].article.link, "https://vnexpress.net/1")

        # API key passed via header, not query param
        call_kwargs = session.post.call_args.kwargs
        self.assertEqual(call_kwargs["headers"]["x-goog-api-key"], "test-key")

    def test_gemini_http_error_falls_back_to_rss(self) -> None:
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=429, text="Quota exceeded")

        digest = summarize_with_gemini(
            self._sample_articles(),
            api_key="test-key",
            session=session,
        )
        self.assertFalse(digest.used_ai)
        self.assertEqual(len(digest.items), 2)
        self.assertIn("429", digest.fallback_reason)


class TestMorningNewsFeature(unittest.TestCase):
    def _make_config(self, **overrides) -> MorningNewsConfig:
        defaults = dict(
            enabled=True,
            rss_urls=DEFAULT_NEWS_RSS_URLS,
            max_articles=10,
            max_highlights=5,
            send_hour=7,
            send_minute=0,
            timezone="Asia/Ho_Chi_Minh",
            check_interval=900,
            gemini_api_key="fake-key",
            gemini_model="gemini-2.5-flash",
        )
        defaults.update(overrides)
        return MorningNewsConfig(**defaults)

    def _sample_article(self) -> NewsArticle:
        return NewsArticle(
            title="Tin thời sự <nóng> & quan trọng",
            link="https://vnexpress.net/tin-nong.html",
            summary="Tóm tắt ngắn",
            source="VnExpress",
        )

    def _sample_digest(self, articles: list[NewsArticle]) -> NewsDigest:
        return NewsDigest(
            overview="Tổng quan <sáng nay> & điểm chính.",
            items=tuple(
                SummarizedItem(
                    headline=a.title,
                    summary=a.summary,
                    article=a,
                )
                for a in articles
            ),
            used_ai=True,
        )

    def test_skips_before_send_hour_and_sends_once_per_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / ".bot_state.json"
            telegram = MagicMock()
            telegram.send_message.return_value = True
            tz = ZoneInfo("Asia/Ho_Chi_Minh")
            article = self._sample_article()

            # 06:45 -> before 07:00 -> should skip
            feature_early = MorningNewsFeature(
                telegram=telegram,
                config=self._make_config(),
                fetcher=lambda _urls, _limit: [article],
                summarizer=self._sample_digest,
                state_store=StateStore(state_path),
                now_provider=lambda _tz: datetime(2026, 11, 26, 6, 45, tzinfo=tz),
            )
            feature_early.execute()
            telegram.send_message.assert_not_called()

            # 07:05 -> sends digest and persists today's date
            feature_morning = MorningNewsFeature(
                telegram=telegram,
                config=self._make_config(),
                fetcher=lambda _urls, _limit: [article],
                summarizer=self._sample_digest,
                state_store=StateStore(state_path),
                now_provider=lambda _tz: datetime(2026, 11, 26, 7, 5, tzinfo=tz),
            )
            feature_morning.execute()
            self.assertEqual(telegram.send_message.call_count, 1)
            sent_text = telegram.send_message.call_args.args[0]
            self.assertIn("26/11/2026", sent_text)
            self.assertIn("Tin thời sự &lt;nóng&gt; &amp; quan trọng", sent_text)
            self.assertTrue(telegram.send_message.call_args.kwargs["disable_web_page_preview"])

            # 08:17 same day (new run) -> already sent today -> skips
            feature_later = MorningNewsFeature(
                telegram=telegram,
                config=self._make_config(),
                fetcher=lambda _urls, _limit: [article],
                summarizer=self._sample_digest,
                state_store=StateStore(state_path),
                now_provider=lambda _tz: datetime(2026, 11, 26, 8, 17, tzinfo=tz),
            )
            feature_later.execute()
            self.assertEqual(telegram.send_message.call_count, 1)

            # Next day 07:00 -> sends again
            feature_next_day = MorningNewsFeature(
                telegram=telegram,
                config=self._make_config(),
                fetcher=lambda _urls, _limit: [article],
                summarizer=self._sample_digest,
                state_store=StateStore(state_path),
                now_provider=lambda _tz: datetime(2026, 11, 27, 7, 0, tzinfo=tz),
            )
            feature_next_day.execute()
            self.assertEqual(telegram.send_message.call_count, 2)

    def test_commands_news_now_and_news_status(self) -> None:
        telegram = MagicMock()
        tz = ZoneInfo("Asia/Ho_Chi_Minh")
        article = self._sample_article()

        feature = MorningNewsFeature(
            telegram=telegram,
            config=self._make_config(),
            fetcher=lambda _urls, _limit: [article],
            summarizer=self._sample_digest,
            now_provider=lambda _tz: datetime(2026, 11, 26, 5, 0, tzinfo=tz),
        )
        router = CommandRouter()
        feature.register_commands(router)

        # /news_now works even before 07:00
        update_now = {"message": {"chat": {"id": 123}, "text": "/news_now"}}
        res_now = router.handle_update(update_now, allowed_chat_id="123")
        self.assertIsNotNone(res_now)
        assert res_now is not None
        self.assertIn("BẢN TIN BUỔI SÁNG", res_now[1])
        self.assertIn("Gemini AI", res_now[1])

        # /news_status displays schedule and sources
        update_status = {"message": {"chat": {"id": 123}, "text": "/news_status"}}
        res_status = router.handle_update(update_status, allowed_chat_id="123")
        self.assertIsNotNone(res_status)
        assert res_status is not None
        self.assertIn("07:00", res_status[1])
        self.assertIn("gemini-2.5-flash", res_status[1])
        self.assertIn("vnexpress.net", res_status[1])

    def test_settings_loads_morning_news_config(self) -> None:
        keys = [
            "TELEGRAM_TOKEN",
            "TELEGRAM_CHAT_ID",
            "NEWS_ENABLED",
            "NEWS_RSS_URLS",
            "NEWS_SEND_HOUR",
            "NEWS_SEND_MINUTE",
            "NEWS_TIMEZONE",
            "NEWS_MAX_ARTICLES",
            "NEWS_MAX_HIGHLIGHTS",
            "GEMINI_API_KEY",
            "GEMINI_MODEL",
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
                            "NEWS_ENABLED=true",
                            "NEWS_RSS_URLS=https://vnexpress.net/rss/tin-noi-bat.rss",
                            "NEWS_SEND_HOUR=6",
                            "NEWS_SEND_MINUTE=30",
                            "GEMINI_API_KEY=my-gemini-key",
                            "GEMINI_MODEL=gemini-2.5-flash",
                        ]
                    ),
                    encoding="utf-8",
                )
                settings = Settings.load(env_file=env_path)
                self.assertTrue(settings.morning_news.enabled)
                self.assertEqual(
                    settings.morning_news.rss_urls,
                    ("https://vnexpress.net/rss/tin-noi-bat.rss",),
                )
                self.assertEqual(settings.morning_news.send_hour, 6)
                self.assertEqual(settings.morning_news.send_minute, 30)
                self.assertEqual(settings.morning_news.gemini_api_key, "my-gemini-key")
        finally:
            for k, v in backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
