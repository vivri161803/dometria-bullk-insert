"""HTTP client for Connect categories/contracts sync endpoints."""

from __future__ import annotations

import time
from typing import Any

import httpx

from bulk_insert.config import Settings

__all__: list[str] = [
    "CatalogClientError",
    "CatalogClient",
]


class CatalogClientError(Exception):
    """Raised when categories/contracts cannot be fetched successfully."""


class CatalogClient:
    """Thin HTTP client with bounded retry/backoff for Connect catalog endpoints."""

    _CATEGORIES_PATH = "/api/v1/integrations/dometria-bulk-insert/assets/categories"

    def __init__(self, cfg: Settings) -> None:
        self._cfg = cfg

    def fetch_categories_tree(self) -> list[dict[str, Any]]:
        payload = self._get_json(self._CATEGORIES_PATH)
        data = payload.get("data")
        if not isinstance(data, list):
            raise CatalogClientError("Invalid categories payload: top-level 'data' is not a list.")
        return data

    def fetch_contract(self, category_id: int) -> dict[str, Any]:
        payload = self._get_json(
            f"/api/v1/integrations/dometria-bulk-insert/assets/categories/{category_id}/contract"
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise CatalogClientError(
                f"Invalid contract payload for category {category_id}: 'data' is not an object."
            )
        return data

    def _get_json(self, path: str) -> dict[str, Any]:
        base_url = self._cfg.CONNECT_BASE_URL.rstrip("/")
        url = f"{base_url}{path}"
        headers: dict[str, str] = {}
        if self._cfg.CONNECT_CALLBACK_TOKEN:
            headers["Authorization"] = f"Bearer {self._cfg.CONNECT_CALLBACK_TOKEN}"

        attempts = max(1, self._cfg.CATALOG_SYNC_MAX_RETRIES)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = httpx.get(url, headers=headers, timeout=self._cfg.CATALOG_SYNC_TIMEOUT_SECONDS)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise CatalogClientError(f"Invalid JSON envelope from {url}.")
                return payload
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < attempts:
                    time.sleep(self._cfg.CATALOG_SYNC_RETRY_BACKOFF_SECONDS * attempt)
        raise CatalogClientError(f"GET {url} failed after {attempts} attempts: {last_error}")
