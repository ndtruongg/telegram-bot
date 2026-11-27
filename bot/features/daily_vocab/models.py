"""Data models, topic normalization, and history serialization for daily_vocab."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

CONCRETE_TOPICS: tuple[str, ...] = ("it", "communication", "ielts")
VALID_TOPICS: tuple[str, ...] = ("it", "communication", "ielts", "mixed")

TOPIC_LABELS_VI: dict[str, str] = {
    "it": "IT & Công nghệ",
    "communication": "Giao tiếp hằng ngày & Công sở",
    "ielts": "IELTS & Học thuật",
    "mixed": "Tổng hợp (luân phiên IT / Giao tiếp / IELTS)",
}

TOPIC_BADGES: dict[str, str] = {
    "it": "💻 IT",
    "communication": "💬 Giao tiếp",
    "ielts": "🎓 IELTS",
    "mixed": "🔀 Tổng hợp",
}

TOPIC_ALIASES: dict[str, str] = {
    "it": "it",
    "tech": "it",
    "dev": "it",
    "software": "it",
    "cntt": "it",
    "communication": "communication",
    "comm": "communication",
    "daily": "communication",
    "speaking": "communication",
    "giaotiep": "communication",
    "giao_tiep": "communication",
    "work": "communication",
    "ielts": "ielts",
    "academic": "ielts",
    "exam": "ielts",
    "mixed": "mixed",
    "mix": "mixed",
    "all": "mixed",
    "auto": "mixed",
    "random": "mixed",
    "tonghop": "mixed",
    "tong_hop": "mixed",
}

_WHITESPACE_RE = re.compile(r"\s+")
_EDGE_PUNCT_CHARS = " \t\n\r\"'`.,;:!?()[]{}<>-"


def normalize_topic(raw: str) -> str | None:
    """Normalize a user-supplied topic string to one of VALID_TOPICS, or None if invalid."""
    cleaned = raw.strip().lower().replace("-", "_").replace(" ", "_")
    return TOPIC_ALIASES.get(cleaned)


def normalize_term(term: str) -> str:
    """Normalize an English word/phrase for case- and punctuation-insensitive deduplication."""
    stripped = term.strip().strip(_EDGE_PUNCT_CHARS)
    collapsed = _WHITESPACE_RE.sub(" ", stripped).lower()
    return collapsed


@dataclass(frozen=True)
class VocabItem:
    """Single English word or phrase with pronunciation, Vietnamese meaning, and example."""

    term: str
    phonetic: str
    part_of_speech: str
    meaning_vi: str
    example_en: str
    example_vi: str
    usage_note: str = ""
    topic: str = ""


@dataclass(frozen=True)
class VocabBatch:
    """Generated batch of vocabulary items ready to be formatted and sent."""

    topic: str
    items: tuple[VocabItem, ...]
    used_ai: bool
    fallback_reason: str = ""


@dataclass(frozen=True)
class VocabHistoryEntry:
    """Compact persisted record of a previously sent vocabulary term."""

    term: str
    topic: str = ""
    meaning_vi: str = ""
    learned_date: str = ""

    def to_compact_dict(self) -> dict[str, str]:
        data: dict[str, str] = {"t": self.term}
        if self.topic:
            data["c"] = self.topic
        if self.meaning_vi:
            data["m"] = self.meaning_vi
        if self.learned_date:
            data["d"] = self.learned_date
        return data

    @classmethod
    def from_raw(cls, raw: Any) -> "VocabHistoryEntry | None":
        if isinstance(raw, str):
            term = raw.strip()
            return cls(term=term) if term else None
        if not isinstance(raw, dict):
            return None
        term = str(raw.get("t") or raw.get("term") or "").strip()
        if not term:
            return None
        topic_raw = str(raw.get("c") or raw.get("topic") or "").strip()
        topic = normalize_topic(topic_raw) or ""
        if topic == "mixed":
            topic = ""
        meaning_vi = str(raw.get("m") or raw.get("meaning_vi") or raw.get("meaning") or "").strip()
        learned_date = str(raw.get("d") or raw.get("learned_date") or raw.get("date") or "").strip()
        return cls(
            term=term,
            topic=topic,
            meaning_vi=meaning_vi,
            learned_date=learned_date,
        )


def deserialize_history(raw: str | list[Any] | None) -> list[VocabHistoryEntry]:
    """Parse JSON string or list payload into deduplicated VocabHistoryEntry objects."""
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning("Không thể parse JSON lịch sử từ vựng (%s).", exc)
            return []
    elif isinstance(raw, list):
        parsed = raw
    else:
        return []

    if not isinstance(parsed, list):
        return []

    entries: list[VocabHistoryEntry] = []
    seen: set[str] = set()
    for item in parsed:
        entry = VocabHistoryEntry.from_raw(item)
        if entry is None:
            continue
        norm = normalize_term(entry.term)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        entries.append(entry)
    return entries


def merge_history(
    existing: list[VocabHistoryEntry],
    new_entries: list[VocabHistoryEntry],
    *,
    max_entries: int = 300,
) -> list[VocabHistoryEntry]:
    """Append unique new entries to existing history, trimming oldest beyond max_entries."""
    combined: list[VocabHistoryEntry] = []
    seen: set[str] = set()
    for entry in (*existing, *new_entries):
        norm = normalize_term(entry.term)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        combined.append(entry)

    limit = max(1, max_entries)
    if len(combined) > limit:
        combined = combined[-limit:]
    return combined


def serialize_history(
    entries: list[VocabHistoryEntry],
    *,
    max_bytes: int = 44000,
) -> str:
    """Serialize history entries to compact JSON while staying within GitHub Variable size limits."""
    trimmed = list(entries)
    while True:
        payload = [entry.to_compact_dict() for entry in trimmed]
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) <= max_bytes or len(trimmed) <= 1:
            return serialized
        # Drop oldest ~10% (at least 1) until within byte budget
        drop_count = max(1, len(trimmed) // 10)
        trimmed = trimmed[drop_count:]
