"""Lightweight JSON-backed key-value state store for cross-run persistence."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class StateStore:
    """Persists per-feature state in a local JSON file (useful for stateless CI/cron runs)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {
                    str(k): v for k, v in raw.items() if isinstance(v, dict)
                }
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Không thể đọc file trạng thái %s (%s). Khởi tạo mới.", self._path, exc)
        return {}

    def get(self, feature_name: str, key: str, default: Any = None) -> Any:
        """Read a persisted value for a feature."""
        return self._data.get(feature_name, {}).get(key, default)

    def set(self, feature_name: str, key: str, value: Any) -> None:
        """Update a persisted value for a feature and flush to disk."""
        feature_state = self._data.setdefault(feature_name, {})
        if feature_state.get(key) == value:
            return
        feature_state[key] = value
        self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("Không thể lưu file trạng thái %s: %s", self._path, exc)
