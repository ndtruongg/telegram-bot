"""Flexible argument and configuration parsers for the reminders feature."""

from __future__ import annotations

import calendar
import html
import re
from dataclasses import dataclass
from datetime import date, timedelta

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY_DATE_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")
_DM_DATE_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})$")
_RELATIVE_DATE_RE = re.compile(r"^\+(\d+)([dw])$", re.IGNORECASE)
_TIME_RE = re.compile(r"^(\d{1,2})[:hH](\d{2})?$")

_REPEAT_ALIASES: dict[str, str] = {
    "#none": "none",
    "#once": "none",
    "#motlan": "none",
    "#daily": "daily",
    "#day": "daily",
    "#ngay": "daily",
    "#hangngay": "daily",
    "#weekly": "weekly",
    "#week": "weekly",
    "#tuan": "weekly",
    "#hangtuan": "weekly",
    "#monthly": "monthly",
    "#month": "monthly",
    "#thang": "monthly",
    "#hangthang": "monthly",
    "#yearly": "yearly",
    "#year": "yearly",
    "#nam": "yearly",
    "#hangnam": "yearly",
}

_TODAY_ALIASES: frozenset[str] = frozenset({"today", "homnay", "hômnay", "hn"})
_TOMORROW_ALIASES: frozenset[str] = frozenset({"tomorrow", "ngaymai", "ngàymai", "mai"})


@dataclass(frozen=True)
class ParsedReminderInput:
    """Validated result of parsing user arguments for ``/remind_add``."""

    event_date: date
    event_time: str | None
    title: str
    repeat: str


def parse_lead_days(raw: str, default: tuple[int, ...] = (7, 3, 1, 0)) -> tuple[int, ...]:
    """Parse a comma-separated list of non-negative lead days (always includes 0)."""
    text = (raw or "").strip()
    if not text:
        return default

    days: set[int] = set()
    for part in text.split(","):
        cleaned = part.strip()
        if not cleaned:
            continue
        try:
            val = int(cleaned)
        except ValueError:
            continue
        if 0 <= val <= 365:
            days.add(val)

    if not days:
        return default
    days.add(0)
    return tuple(sorted(days, reverse=True))


def parse_date_token(token: str, today: date) -> date | None:
    """Parse a flexible date token relative to ``today``.

    Supported formats:
    - ``YYYY-MM-DD`` (e.g. ``2026-12-25``)
    - ``DD/MM/YYYY`` or ``DD-MM-YYYY`` (e.g. ``25/12/2026``)
    - ``DD/MM`` or ``DD-MM`` (auto-picks current or next year)
    - ``today`` / ``homnay``, ``tomorrow`` / ``ngaymai`` / ``mai``
    - ``+<N>d`` (days) or ``+<N>w`` (weeks)
    """
    cleaned = token.strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()

    if lowered in _TODAY_ALIASES:
        return today
    if lowered in _TOMORROW_ALIASES:
        return today + timedelta(days=1)

    rel_match = _RELATIVE_DATE_RE.match(cleaned)
    if rel_match:
        amount = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        if amount <= 0 or amount > 3650:
            return None
        delta = amount * 7 if unit == "w" else amount
        return today + timedelta(days=delta)

    iso_match = _ISO_DATE_RE.match(cleaned)
    if iso_match:
        try:
            return date(
                int(iso_match.group(1)),
                int(iso_match.group(2)),
                int(iso_match.group(3)),
            )
        except ValueError:
            return None

    dmy_match = _DMY_DATE_RE.match(cleaned)
    if dmy_match:
        try:
            return date(
                int(dmy_match.group(3)),
                int(dmy_match.group(2)),
                int(dmy_match.group(1)),
            )
        except ValueError:
            return None

    dm_match = _DM_DATE_RE.match(cleaned)
    if dm_match:
        day = int(dm_match.group(1))
        month = int(dm_match.group(2))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None

        # Handle 29/02 cleanly by finding the next valid leap year
        if month == 2 and day == 29:
            year = today.year
            for _ in range(8):
                if calendar.isleap(year):
                    candidate = date(year, 2, 29)
                    if candidate >= today:
                        return candidate
                year += 1
            return None

        try:
            candidate = date(today.year, month, day)
        except ValueError:
            return None

        if candidate < today:
            candidate = date(today.year + 1, month, day)
        return candidate

    return None


def parse_time_token(token: str) -> str | None:
    """Parse a time token like ``09:30``, ``9:05``, ``14h30``, or ``8h`` into ``HH:MM``."""
    match = _TIME_RE.match(token.strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) is not None else 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return None


def parse_reminder_args(args: str, today: date) -> tuple[ParsedReminderInput | None, str | None]:
    """Parse ``/remind_add`` arguments into ``(ParsedReminderInput, error_message)``."""
    tokens = args.strip().split()
    if len(tokens) < 2:
        return None, (
            "⚠️ Thiếu thông tin ngày hoặc nội dung nhắc việc.\n"
            "Ví dụ:\n"
            "• <code>/remind_add 25/12 Giáng sinh #yearly</code>\n"
            "• <code>/remind_add 10/12 Đóng tiền điện #monthly</code>\n"
            "• <code>/remind_add ngaymai 09:30 Họp dự án</code>\n"
            "• <code>/remind_add +3d Nộp báo cáo</code>"
        )

    repeat = "none"
    filtered_tokens: list[str] = []
    for tok in tokens:
        mapped = _REPEAT_ALIASES.get(tok.lower())
        if mapped is not None:
            repeat = mapped
        else:
            filtered_tokens.append(tok)

    if len(filtered_tokens) < 2:
        return None, "⚠️ Vui lòng nhập đầy đủ ngày và nội dung nhắc việc."

    event_date = parse_date_token(filtered_tokens[0], today)
    if event_date is None:
        return None, (
            f"⚠️ Không nhận diện được ngày <code>{html.escape(filtered_tokens[0])}</code>.\n"
            "Hỗ trợ: <code>DD/MM</code>, <code>DD/MM/YYYY</code>, <code>YYYY-MM-DD</code>, "
            "<code>homnay</code>, <code>ngaymai</code>, <code>+3d</code>, <code>+2w</code>."
        )

    remaining = filtered_tokens[1:]
    event_time: str | None = None
    if len(remaining) >= 2:
        maybe_time = parse_time_token(remaining[0])
        if maybe_time is not None:
            event_time = maybe_time
            remaining = remaining[1:]

    title = " ".join(remaining).strip()
    if not title:
        return None, "⚠️ Nội dung nhắc việc không được để trống."

    if len(title) > 160:
        title = title[:157].rstrip() + "..."

    return (
        ParsedReminderInput(
            event_date=event_date,
            event_time=event_time,
            title=title,
            repeat=repeat,
        ),
        None,
    )
