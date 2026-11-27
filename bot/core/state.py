"""Lightweight JSON-backed key-value state store with optional GitHub Variable sync."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot.core.github_vars import GitHubVariablesClient

logger = logging.getLogger(__name__)


def _parse_state_dict(raw_json: str) -> dict[str, dict[str, Any]]:
    text = (raw_json or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Không thể parse JSON trạng thái bot (%s).", exc)
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): dict(v) for k, v in parsed.items() if isinstance(v, dict)}


def _merge_state_dicts(
    base: dict[str, dict[str, Any]],
    override: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Merge two state dicts, preserving union of nested dicts and latest ISO dates."""
    merged: dict[str, dict[str, Any]] = {k: dict(v) for k, v in base.items()}
    for feature_name, feat_state in override.items():
        target = merged.setdefault(feature_name, {})
        for key, value in feat_state.items():
            existing = target.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                combined = dict(existing)
                combined.update(value)
                target[key] = combined
            elif (
                key == "last_sent_date"
                and isinstance(existing, str)
                and isinstance(value, str)
            ):
                target[key] = max(existing, value)
            else:
                target[key] = value
    return merged


class StateStore:
    """Persists per-feature state in a local JSON file and optionally syncs via GitHub Variables."""

    DEFAULT_GITHUB_VAR = "BOT_STATE_JSON"

    def __init__(
        self,
        path: str | Path,
        *,
        github_vars: "GitHubVariablesClient | None" = None,
        github_var_name: str = DEFAULT_GITHUB_VAR,
        initial_json: str = "",
    ) -> None:
        self._path = Path(path)
        self._github_vars = github_vars
        self._github_var_name = github_var_name
        self._initial_json = initial_json
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        local_data: dict[str, dict[str, Any]] = {}
        if self._path.is_file():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    local_data = {
                        str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)
                    }
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "Không thể đọc file trạng thái %s (%s). Khởi tạo mới.",
                    self._path,
                    exc,
                )

        if self._github_vars is not None:
            remote_raw = self._github_vars.get_variable(self._github_var_name)
            if remote_raw is not None:
                remote_data = _parse_state_dict(remote_raw)
                return remote_data if remote_data or not local_data else local_data

        if self._initial_json:
            env_data = _parse_state_dict(self._initial_json)
            if env_data:
                return _merge_state_dicts(local_data, env_data)

        return local_data

    def get(self, feature_name: str, key: str, default: Any = None) -> Any:
        """Read a persisted value for a feature."""
        return self._data.get(feature_name, {}).get(key, default)

    def set(self, feature_name: str, key: str, value: Any) -> None:
        """Update a persisted value for a feature and flush to disk / GitHub Variable."""
        feature_state = self._data.setdefault(feature_name, {})
        if feature_state.get(key) == value:
            return
        feature_state[key] = value
        self._save()

    def _save(self) -> None:
        if self._github_vars is not None:
            compact = json.dumps(self._data, ensure_ascii=False, separators=(",", ":"))
            if not self._github_vars.set_variable(self._github_var_name, compact):
                logger.warning(
                    "Không thể đồng bộ trạng thái lên GitHub Variable '%s'.",
                    self._github_var_name,
                )

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            # On read-only serverless filesystems (e.g. Vercel), GitHub Variable sync is primary
            if self._github_vars is None:
                logger.error("Không thể lưu file trạng thái %s: %s", self._path, exc)
            else:
                logger.debug("Bỏ qua ghi file cục bộ %s (%s).", self._path, exc)
