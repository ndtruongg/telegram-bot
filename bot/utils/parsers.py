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


def parse_user_price(raw_value: str) -> float | None:
    """Parse user-entered target price supporting both VND integers and decimals.

    Examples:
        - '13450000' -> 13450000.0
        - '13.450.000' -> 13450000.0
        - '13,450,000' -> 13450000.0
        - '84.5' / '84,5' -> 84.5
    """
    cleaned = raw_value.strip().lower()
    cleaned = re.sub(r"(vnd|vnđ|đ|d)$", "", cleaned).strip()
    cleaned = cleaned.replace(" ", "")
    if not cleaned or not re.fullmatch(r"\d+(?:[.,]\d+)*", cleaned):
        return None

    separators = re.findall(r"[.,]", cleaned)
    if not separators:
        value = float(cleaned)
    elif len(separators) == 1:
        left, right = re.split(r"[.,]", cleaned)
        # Treat single separator followed by exactly 3 digits (e.g. '13.500') as thousands separator,
        # otherwise treat as decimal point (e.g. '84.5', '84.55').
        if len(right) == 3 and len(left) >= 1 and not left.startswith("0"):
            value = float(left + right)
        else:
            value = float(f"{left}.{right}")
    else:
        parts = re.split(r"[.,]", cleaned)
        if all(len(part) == 3 for part in parts[1:]):
            value = float("".join(parts))
        else:
            return None

    return value if value > 0 else None


def format_price(value: float) -> str:
    """Format a numeric price cleanly (e.g., 13500000 -> '13,500,000', 84.5 -> '84.5')."""
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def serialize_price(value: float) -> str:
    """Serialize a numeric price for env vars / GitHub Repository Variables."""
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")
