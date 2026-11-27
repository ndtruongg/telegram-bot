"""Data models, recurrence calculation, and JSON serialization for reminders."""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

VALID_REPEATS: frozenset[str] = frozenset({"none", "daily", "weekly", "monthly", "yearly"})

REPEAT_LABELS_VI: dict[str, str] = {
    "none": "Một lần",
    "daily": "Hằng ngày",
    "weekly": "Hằng tuần",
    "monthly": "Hằng tháng",
    "yearly": "Hằng năm",
}

WEEKDAY_NAMES_VI: tuple[str, ...] = (
    "Thứ 2",
    "Thứ 3",
    "Thứ 4",
    "Thứ 5",
    "Thứ 6",
    "Thứ 7",
    "Chủ nhật",
)


def _clamp_date(year: int, month: int, day: int) -> date:
    """Construct a date, clamping ``day`` to the last valid day of ``(year, month)``."""
    max_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, max_day))


def format_date_vi(target_date: date, *, include_weekday: bool = False) -> str:
    """Format a date as ``DD/MM/YYYY`` (optionally prefixed with Vietnamese weekday)."""
    formatted = target_date.strftime("%d/%m/%Y")
    if include_weekday:
        return f"{WEEKDAY_NAMES_VI[target_date.weekday()]}, {formatted}"
    return formatted


def format_countdown_badge(days_left: int) -> str:
    """Return a concise Vietnamese countdown label for ``days_left``."""
    if days_left < 0:
        return f"⌛ Đã qua {abs(days_left)} ngày"
    if days_left == 0:
        return "🔥 Hôm nay!"
    if days_left == 1:
        return "⏰ Ngày mai (còn 1 ngày)"
    return f"⏳ Còn {days_left} ngày"


@dataclass(frozen=True)
class ReminderItem:
    """Single reminder or countdown event."""

    id: int
    title: str
    event_date: date
    event_time: str | None = None  # "HH:MM" or None
    repeat: str = "none"
    tag: str = ""

    def notification_token(self) -> str:
        """Short unique fingerprint so reusing a deleted ID never collides in dedupe state."""
        raw = f"{self.tag}|{self.event_time or ''}|{self.title}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:8]

    def next_occurrence(self, today: date) -> date:
        """Compute the active/next occurrence date on or after ``today``.

        For non-repeating reminders in the past, returns the original ``event_date``.
        """
        if self.repeat == "none" or self.event_date >= today:
            return self.event_date

        if self.repeat == "daily":
            return today

        if self.repeat == "weekly":
            delta_days = (today - self.event_date).days
            weeks_to_add = (delta_days + 6) // 7
            return self.event_date + timedelta(days=weeks_to_add * 7)

        if self.repeat == "monthly":
            year, month = today.year, today.month
            candidate = _clamp_date(year, month, self.event_date.day)
            if candidate < today:
                if month == 12:
                    year += 1
                    month = 1
                else:
                    month += 1
                candidate = _clamp_date(year, month, self.event_date.day)
            return candidate

        if self.repeat == "yearly":
            year = today.year
            candidate = _clamp_date(year, self.event_date.month, self.event_date.day)
            if candidate < today:
                candidate = _clamp_date(year + 1, self.event_date.month, self.event_date.day)
            return candidate

        return self.event_date

    def days_until(self, today: date) -> int:
        """Number of days from ``today`` until the active/next occurrence."""
        return (self.next_occurrence(today) - today).days

    def effective_lead_days(self, default_lead_days: tuple[int, ...]) -> tuple[int, ...]:
        """Filter lead days appropriately for the item's repeat cadence."""
        base = set(d for d in default_lead_days if d >= 0)
        base.add(0)

        if self.repeat == "daily":
            return (0,)
        if self.repeat == "weekly":
            return tuple(sorted(d for d in base if d <= 1))
        if self.repeat == "monthly":
            return tuple(sorted(d for d in base if d <= 7))
        return tuple(sorted(base))

    def parsed_time(self) -> tuple[int, int] | None:
        """Return ``(hour, minute)`` if ``event_time`` is set, else ``None``."""
        if not self.event_time:
            return None
        parts = self.event_time.split(":")
        if len(parts) != 2:
            return None
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute)
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a compact JSON-friendly dictionary."""
        data: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "date": self.event_date.isoformat(),
        }
        if self.event_time:
            data["time"] = self.event_time
        if self.repeat and self.repeat != "none":
            data["repeat"] = self.repeat
        if self.tag:
            data["tag"] = self.tag
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReminderItem | None":
        """Deserialize a dictionary into a ``ReminderItem``, or return ``None`` if invalid."""
        try:
            item_id = int(raw["id"])
            title = str(raw["title"]).strip()
            event_date = date.fromisoformat(str(raw["date"]).strip())
        except (KeyError, TypeError, ValueError):
            return None

        if item_id <= 0 or not title:
            return None

        raw_time = raw.get("time")
        event_time: str | None = None
        if isinstance(raw_time, str) and raw_time.strip():
            parts = raw_time.strip().split(":")
            if len(parts) == 2:
                try:
                    hour, minute = int(parts[0]), int(parts[1])
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        event_time = f"{hour:02d}:{minute:02d}"
                except ValueError:
                    event_time = None

        raw_repeat = str(raw.get("repeat", "none")).strip().lower()
        repeat = raw_repeat if raw_repeat in VALID_REPEATS else "none"
        tag = str(raw.get("tag", "")).strip()[:16]

        return cls(
            id=item_id,
            title=title,
            event_date=event_date,
            event_time=event_time,
            repeat=repeat,
            tag=tag,
        )


def deserialize_reminders(raw_json: str) -> list[ReminderItem]:
    """Parse a JSON string into a list of valid ``ReminderItem`` instances."""
    text = (raw_json or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Không thể parse JSON danh sách nhắc việc (%s).", exc)
        return []

    if not isinstance(parsed, list):
        logger.warning("Dữ liệu nhắc việc không phải mảng JSON.")
        return []

    items: list[ReminderItem] = []
    seen_ids: set[int] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        item = ReminderItem.from_dict(entry)
        if item is None or item.id in seen_ids:
            continue
        seen_ids.add(item.id)
        items.append(item)
    return items


def serialize_reminders(items: list[ReminderItem]) -> str:
    """Serialize reminders to compact UTF-8 JSON suitable for GitHub Repository Variables."""
    payload = [item.to_dict() for item in items]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
