"""Web scraper for SJC gold prices from webgia.com."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from bot.utils.parsers import parse_price

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


@dataclass(frozen=True)
class GoldPrice:
    name: str
    buy_raw: str
    sell: float
    sell_raw: str


def parse_sjc_gold_html(html: str) -> GoldPrice | None:
    """Parse the first valid SJC gold price row from HTML content."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        logger.warning("Không tìm thấy bảng dữ liệu giá vàng.")
        return None

    rows = table.find_all("tr")
    for row in rows[1:]:
        cols = [col.get_text(strip=True) for col in row.find_all("td")]
        if len(cols) < 3:
            continue

        loai_vang, gia_mua, gia_ban = cols[0], cols[1], cols[2]
        sell_price = parse_price(gia_ban)
        if sell_price is None:
            continue

        return GoldPrice(
            name=loai_vang,
            buy_raw=gia_mua,
            sell=sell_price,
            sell_raw=gia_ban,
        )

    logger.warning("Không tìm thấy dòng giá bán hợp lệ trong bảng.")
    return None


def fetch_sjc_gold_price(url: str, timeout: int = 15) -> GoldPrice | None:
    """Fetch and parse the latest SJC gold price from the target URL."""
    try:
        response = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Lỗi khi lấy dữ liệu web (%s): %s", url, exc)
        return None

    return parse_sjc_gold_html(response.text)
