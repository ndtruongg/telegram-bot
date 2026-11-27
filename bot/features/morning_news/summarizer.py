"""Gemini API summarizer for morning news digests with structured JSON output."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import requests

from bot.features.morning_news.rss import NewsArticle

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
FALLBACK_GEMINI_MODELS: tuple[str, ...] = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
)
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


@dataclass(frozen=True)
class SummarizedItem:
    """Single synthesized news takeaway linked back to a verified RSS article."""

    headline: str
    summary: str
    article: NewsArticle


@dataclass(frozen=True)
class NewsDigest:
    """Complete morning news digest ready to be rendered for Telegram."""

    overview: str
    items: tuple[SummarizedItem, ...]
    used_ai: bool
    fallback_reason: str = ""


def _build_prompt(articles: list[NewsArticle], max_highlights: int) -> str:
    article_lines: list[str] = []
    for idx, article in enumerate(articles, start=1):
        snippet = article.summary or "(Không có mô tả ngắn)"
        article_lines.append(
            f"[{idx}] Nguồn: {article.source}\n"
            f"Tiêu đề: {article.title}\n"
            f"Mô tả: {snippet}"
        )

    joined = "\n\n".join(article_lines)
    return (
        "Bạn là biên tập viên bản tin buổi sáng tiếng Việt chuyên nghiệp.\n"
        f"Hãy đọc danh sách {len(articles)} bài báo bên dưới và chọn ra tối đa {max_highlights} tin "
        "đáng chú ý nhất (ưu tiên tin thời sự, xã hội, kinh tế, đời sống có giá trị thông tin cao; "
        "tránh gộp lặp các tin trùng nội dung).\n\n"
        "Trả về DUY NHẤT một JSON object hợp lệ với cấu trúc:\n"
        "{\n"
        '  "overview": "1-2 câu ngắn gọn khái quát bức tranh tin tức nổi bật sáng nay.",\n'
        '  "items": [\n'
        "    {\n"
        '      "index": 1,\n'
        '      "headline": "Tiêu đề ngắn gọn, súc tích bằng tiếng Việt (tối đa ~15 từ)",\n'
        '      "summary": "1 câu ngắn gọn (tối đa ~25 từ) tóm tắt ý chính dễ hiểu, khách quan, không thêm thông tin bịa đặt."\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Quy tắc bắt buộc:\n"
        "- Trường `index` phải là số nguyên đúng với mã [index] của bài báo gốc trong danh sách.\n"
        "- Không dùng thẻ HTML hay Markdown trong giá trị JSON.\n"
        "- Viết hoàn toàn bằng tiếng Việt tự nhiên, súc tích.\n\n"
        f"DANH SÁCH TIN TỨC:\n{joined}"
    )


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    content = candidates[0].get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    chunks = [
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return "\n".join(chunks).strip()


def _parse_digest_json(raw_text: str, articles: list[NewsArticle]) -> NewsDigest | None:
    cleaned = _CODE_FENCE_RE.sub("", raw_text.strip()).strip()
    if not cleaned:
        return None

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Attempt to locate outermost JSON object if extra prose surrounded it
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None

    if not isinstance(data, dict):
        return None

    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return None

    parsed_items: list[SummarizedItem] = []
    seen_indices: set[int] = set()
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("index")
        if not isinstance(idx, int) or idx < 1 or idx > len(articles) or idx in seen_indices:
            continue
        article = articles[idx - 1]
        headline = str(entry.get("headline") or article.title).strip()
        summary = str(entry.get("summary") or article.summary).strip()
        if not headline:
            continue
        seen_indices.add(idx)
        parsed_items.append(
            SummarizedItem(headline=headline, summary=summary, article=article)
        )

    if not parsed_items:
        return None

    overview = str(data.get("overview") or "").strip()
    return NewsDigest(
        overview=overview,
        items=tuple(parsed_items),
        used_ai=True,
    )


def build_rss_fallback_digest(
    articles: list[NewsArticle],
    max_highlights: int = 12,
    reason: str = "",
) -> NewsDigest:
    """Create a clean digest directly from RSS headlines and descriptions without AI."""
    snippet_limit = 130 if max_highlights >= 10 else 180
    items = tuple(
        SummarizedItem(
            headline=article.title,
            summary=(
                article.summary[:snippet_limit]
                + ("..." if len(article.summary) > snippet_limit else "")
            ),
            article=article,
        )
        for article in articles[:max_highlights]
    )
    return NewsDigest(
        overview="",
        items=items,
        used_ai=False,
        fallback_reason=reason,
    )


def _normalize_model_candidates(model: str) -> list[str]:
    primary = model.strip().removeprefix("models/").strip() or DEFAULT_GEMINI_MODEL
    candidates: list[str] = [primary]
    for fallback in FALLBACK_GEMINI_MODELS:
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates


def summarize_with_gemini(
    articles: list[NewsArticle],
    *,
    api_key: str,
    model: str = DEFAULT_GEMINI_MODEL,
    max_highlights: int = 12,
    timeout: int = 25,
    session: requests.Session | None = None,
) -> NewsDigest:
    """Summarize news articles using Gemini REST API, falling back to RSS on failure."""
    if not articles:
        raise ValueError("Danh sách bài báo trống, không thể tóm tắt.")

    if not api_key.strip():
        logger.info("Chưa cấu hình GEMINI_API_KEY, dùng chế độ tóm tắt nhanh từ RSS.")
        return build_rss_fallback_digest(
            articles,
            max_highlights=max_highlights,
            reason="Chưa cấu hình GEMINI_API_KEY",
        )

    prompt = _build_prompt(articles, max_highlights=min(max_highlights, len(articles)))
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "responseMimeType": "application/json",
        },
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key.strip(),
    }

    client = session or requests
    candidates = _normalize_model_candidates(model)
    last_http_status: int | None = None

    for idx, candidate_model in enumerate(candidates):
        endpoint = f"{GEMINI_API_BASE}/{candidate_model}:generateContent"
        try:
            response = client.post(endpoint, headers=headers, json=body, timeout=timeout)
            if response.status_code == 404 and idx + 1 < len(candidates):
                logger.warning(
                    "Model Gemini '%s' trả về HTTP 404 (đã ngừng hỗ trợ hoặc không tồn tại), "
                    "tự động thử sang '%s'.",
                    candidate_model,
                    candidates[idx + 1],
                )
                last_http_status = 404
                continue

            if response.status_code != 200:
                logger.warning(
                    "Gemini API trả về lỗi (%s) với model '%s': %s",
                    response.status_code,
                    candidate_model,
                    response.text[:300],
                )
                return build_rss_fallback_digest(
                    articles,
                    max_highlights=max_highlights,
                    reason=f"Gemini API lỗi HTTP {response.status_code}",
                )

            payload = response.json()
            raw_text = _extract_gemini_text(payload)
            digest = _parse_digest_json(raw_text, articles)
            if digest is not None:
                return digest

            logger.warning("Không thể phân tích JSON từ Gemini API, chuyển sang tóm tắt RSS.")
            return build_rss_fallback_digest(
                articles,
                max_highlights=max_highlights,
                reason="Phản hồi AI không đúng định dạng",
            )
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Lỗi gọi Gemini API (%s), chuyển sang tóm tắt RSS.", exc)
            return build_rss_fallback_digest(
                articles,
                max_highlights=max_highlights,
                reason="Không kết nối được Gemini API",
            )

    return build_rss_fallback_digest(
        articles,
        max_highlights=max_highlights,
        reason=f"Gemini API lỗi HTTP {last_http_status or 404}",
    )
