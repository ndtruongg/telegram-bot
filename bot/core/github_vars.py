"""GitHub REST API client for reading and updating Actions Repository Variables."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


class GitHubVariablesClient:
    """Reads and writes GitHub Actions Repository Variables for cross-environment sync."""

    API_BASE = "https://api.github.com"

    def __init__(
        self,
        token: str,
        repository: str,
        timeout: int = 10,
        session: requests.Session | None = None,
    ) -> None:
        self._token = token.strip()
        self._repository = repository.strip().strip("/")
        self._timeout = timeout
        self._session = session or requests.Session()

    @property
    def repository(self) -> str:
        return self._repository

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _var_url(self, name: str) -> str:
        return f"{self.API_BASE}/repos/{self._repository}/actions/variables/{name}"

    def _collection_url(self) -> str:
        return f"{self.API_BASE}/repos/{self._repository}/actions/variables"

    def get_variable(self, name: str) -> str | None:
        """Return the current string value of a repository variable, or None if missing/error."""
        try:
            response = self._session.get(
                self._var_url(name),
                headers=self._headers(),
                timeout=self._timeout,
            )
            if response.status_code == 200:
                value = response.json().get("value")
                return str(value) if value is not None else None
            if response.status_code == 404:
                return None
            logger.error(
                "Lỗi đọc GitHub Variable '%s' (%s): %s",
                name,
                response.status_code,
                response.text,
            )
            return None
        except requests.RequestException as exc:
            logger.error("Lỗi kết nối GitHub API khi đọc '%s': %s", name, exc)
            return None

    def set_variable(self, name: str, value: str) -> bool:
        """Update an existing repository variable, creating it if it does not exist yet."""
        payload = {"name": name, "value": value}
        try:
            response = self._session.patch(
                self._var_url(name),
                headers=self._headers(),
                json=payload,
                timeout=self._timeout,
            )
            if response.status_code == 204:
                logger.info("Đã cập nhật GitHub Variable %s=%s.", name, value)
                return True

            if response.status_code == 404:
                create_resp = self._session.post(
                    self._collection_url(),
                    headers=self._headers(),
                    json=payload,
                    timeout=self._timeout,
                )
                if create_resp.status_code == 201:
                    logger.info("Đã tạo mới GitHub Variable %s=%s.", name, value)
                    return True
                logger.error(
                    "Lỗi tạo mới GitHub Variable '%s' (%s): %s",
                    name,
                    create_resp.status_code,
                    create_resp.text,
                )
                return False

            logger.error(
                "Lỗi cập nhật GitHub Variable '%s' (%s): %s",
                name,
                response.status_code,
                response.text,
            )
            return False
        except requests.RequestException as exc:
            logger.error("Lỗi kết nối GitHub API khi ghi '%s': %s", name, exc)
            return False
