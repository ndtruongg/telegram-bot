"""Price and numeric string parsing helpers."""

from __future__ import annotations

import re


def parse_price(raw_value: str) -> float | None:
    """Parse a formatted price string like '13.500.000' or '13,500,000' into a float.

    Returns ``None`` if the string does not represent a valid numeric price.
    """
    cleaned = raw_value.replace(".", "").replace(",", "").strip()
    if not cleaned or not re.fullmatch(r"\d+", cleaned):
        return None
    return float(cleaned)


def format_price(value: float) -> str:
    """Format a numeric price using comma separators (e.g., 13500000 -> '13,500,000')."""
    return f"{value:,.0f}"
