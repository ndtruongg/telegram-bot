"""RSS feed fetcher and parser for Vietnamese morning news sources."""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
}

_SOURCE_NAME_MAP = {
    "vnexpress.net": "VnExpress",
    "tuoitre.vn": "Tuổi Trẻ",
    "thanhnien.vn": "Thanh Niên",
    "dantri.com.vn": "Dân Trí",
    "vietnamnet.vn": "VietNamNet",
    "cafef.vn": "CafeF",
    "vneconomy.vn": "VnEconomy",
}

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class NewsArticle:
    """Normalized article entry extracted from an RSS/Atom feed."""

    title: str
    link: str
    summary: str
    source: str
    published: str = ""


def _clean_text(raw: str) -> str:
    """Strip HTML markup, decode entities, and collapse whitespace."""
    if not raw:
        return ""
    unescaped = html.unescape(raw)
    if "<" in unescaped and ">" in unescaped:
        text = BeautifulSoup(unescaped, "html.parser").get_text(" ", strip=True)
    else:
        text = unescaped
    return _WHITESPACE_RE.sub(" ", text).strip()


def _infer_source_name(feed_url: str, channel_title: str = "") -> str:
    """Resolve a concise Vietnamese display name for the news source."""
    host = urlparse(feed_url).netloc.lower()
    for domain, label in _SOURCE_NAME_MAP.items():
        if host == domain or host.endswith(f".{domain}"):
            return label

    cleaned_title = _clean_text(channel_title)
    if cleaned_title:
        # Take concise prefix before separators like " - " or " | "
        short = re.split(r"\s+[-|–]\s+", cleaned_title, maxsplit=1)[0].strip()
        return short[:40] if short else cleaned_title[:40]
    return host or "RSS"


def _local_tag(tag: str) -> str:
    """Return XML tag name without namespace prefix."""
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    return tag.lower()


def _child_text(element: ET.Element, *names: str) -> str:
    target = {name.lower() for name in names}
    for child in element:
        if _local_tag(child.tag) in target and child.text:
            return child.text.strip()
    return ""


def parse_rss_xml(xml_content: str, feed_url: str = "") -> list[NewsArticle]:
    """Parse RSS 2.0 or Atom XML string into a list of NewsArticle objects."""
    if not xml_content or not xml_content.strip():
        return []

    try:
        root = ET.fromstring(xml_content.strip())
    except ET.ParseError as exc:
        logger.warning("Không thể phân tích XML RSS từ %s: %s", feed_url or "<inline>", exc)
        return []

    articles: list[NewsArticle] = []
    root_name = _local_tag(root.tag)

    # RSS 2.0 (<rss><channel><item>...)
    if root_name == "rss" or any(_local_tag(c.tag) == "channel" for c in root):
        channel = next((c for c in root if _local_tag(c.tag) == "channel"), root)
        channel_title = _child_text(channel, "title")
        source = _infer_source_name(feed_url, channel_title)

        for item in channel:
            if _local_tag(item.tag) != "item":
                continue
            title = _clean_text(_child_text(item, "title"))
            link = _child_text(item, "link")
            description = _clean_text(_child_text(item, "description", "summary"))
            published = _child_text(item, "pubdate", "published", "updated")
            if not title or not link:
                continue
            articles.append(
                NewsArticle(
                    title=title,
                    link=link,
                    summary=description[:350],
                    source=source,
                    published=published,
                )
            )
        return articles

    # Atom (<feed><entry>...)
    if root_name == "feed":
        feed_title = _child_text(root, "title")
        source = _infer_source_name(feed_url, feed_title)
        for entry in root:
            if _local_tag(entry.tag) != "entry":
                continue
            title = _clean_text(_child_text(entry, "title"))
            link = ""
            for child in entry:
                if _local_tag(child.tag) == "link":
                    href = child.attrib.get("href", "").strip()
                    if href:
                        link = href
                        break
                    if child.text and child.text.strip():
                        link = child.text.strip()
                        break
            summary = _clean_text(_child_text(entry, "summary", "content"))
            published = _child_text(entry, "published", "updated")
            if not title or not link:
                continue
            articles.append(
                NewsArticle(
                    title=title,
                    link=link,
                    summary=summary[:350],
                    source=source,
                    published=published,
                )
            )

    return articles


def fetch_rss_feed(
    url: str,
    timeout: int = 10,
    session: requests.Session | None = None,
) -> list[NewsArticle]:
    """Fetch and parse a single RSS/Atom feed URL."""
    client = session or requests
    try:
        response = client.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
        response.raise_for_status()
        # Ensure UTF-8 decoding when feed omits charset header
        if not response.encoding or response.encoding.lower() == "iso-8859-1":
            response.encoding = response.apparent_encoding or "utf-8"
        return parse_rss_xml(response.text, feed_url=url)
    except requests.RequestException as exc:
        logger.warning("Không thể tải nguồn RSS %s: %s", url, exc)
        return []


def fetch_news_articles(
    urls: tuple[str, ...] | list[str],
    max_articles: int = 10,
    timeout: int = 10,
    session: requests.Session | None = None,
) -> list[NewsArticle]:
    """Fetch multiple RSS feeds, interleave them fairly, and deduplicate articles."""
    if max_articles <= 0 or not urls:
        return []

    per_feed_lists: list[list[NewsArticle]] = []
    for raw_url in urls:
        url = raw_url.strip()
        if not url:
            continue
        items = fetch_rss_feed(url, timeout=timeout, session=session)
        if items:
            per_feed_lists.append(items)

    if not per_feed_lists:
        return []

    # Round-robin interleave so every source is balanced in the final selection
    selected: list[NewsArticle] = []
    seen_keys: set[str] = set()
    max_len = max(len(items) for items in per_feed_lists)

    for idx in range(max_len):
        for items in per_feed_lists:
            if idx >= len(items):
                continue
            article = items[idx]
            norm_title = article.title.lower().strip()
            norm_link = article.link.strip()
            if norm_title in seen_keys or norm_link in seen_keys:
                continue
            seen_keys.add(norm_title)
            seen_keys.add(norm_link)
            selected.append(article)
            if len(selected) >= max_articles:
                return selected

    return selected
