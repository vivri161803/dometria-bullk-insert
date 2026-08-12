"""Periodic connection-test loop: Bulk Insert → Dometria Connect.

Runs a background thread that contacts
``GET {CONNECT_BASE_URL}/api/v1/integrations/dometria-bulk-insert/connection-test``
every ``CONNECT_PING_INTERVAL_SECONDS`` seconds and logs the outcome
(URL contacted, HTTP status code, response body).
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from bulk_insert.config import Settings

__all__: list[str] = ["ConnectionTestLoop"]

logger = logging.getLogger(__name__)

_CONNECTION_TEST_PATH = "/api/v1/integrations/dometria-bulk-insert/connection-test"


class ConnectionTestLoop:
    """Background thread that polls the Connect connection-test endpoint.

    Start with ``start()`` inside the application lifespan and stop cleanly
    with ``stop()`` on shutdown.  The loop does not raise: every failure is
    absorbed and logged so it never disrupts the main service.
    """

    def __init__(self, cfg: Settings) -> None:
        self._cfg = cfg
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Spawn the background polling thread."""
        if not self._cfg.CONNECT_PING_ENABLED:
            logger.info(
                "Connection-test loop disabled (CONNECT_PING_ENABLED=false).",
                extra={"path": _CONNECTION_TEST_PATH},
            )
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="connection-test-loop",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Connection-test loop started (interval=%ds url=%s).",
            self._cfg.CONNECT_PING_INTERVAL_SECONDS,
            self._ping_url(),
            extra={
                "path": _CONNECTION_TEST_PATH,
                "interval_seconds": self._cfg.CONNECT_PING_INTERVAL_SECONDS,
                "ping_url": self._ping_url(),
            },
        )

    def stop(self) -> None:
        """Signal the loop to stop and wait for the thread to finish."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info(
            "Connection-test loop stopped.",
            extra={"path": _CONNECTION_TEST_PATH},
        )

    def _ping_url(self) -> str:
        return f"{self._cfg.CONNECT_BASE_URL.rstrip('/')}{_CONNECTION_TEST_PATH}"

    def _run(self) -> None:
        """Loop body: wait for the configured interval, then ping Connect."""
        while not self._stop_event.wait(timeout=self._cfg.CONNECT_PING_INTERVAL_SECONDS):
            self._ping()

    def _ping(self) -> None:
        """Execute a single GET request and log the result."""
        url = self._ping_url()
        headers: dict[str, str] = {}
        if self._cfg.CONNECT_CALLBACK_TOKEN:
            headers["Authorization"] = f"Bearer {self._cfg.CONNECT_CALLBACK_TOKEN}"

        try:
            response = httpx.get(
                url,
                headers=headers,
                timeout=self._cfg.CONNECT_PING_TIMEOUT_SECONDS,
            )
            body = response.text[:2000]
            if response.is_success:
                logger.info(
                    "Connection test OK — %s %d.",
                    url,
                    response.status_code,
                    extra={
                        "ping_url": url,
                        "status_code": response.status_code,
                        "response_body": body,
                    },
                )
            else:
                logger.warning(
                    "Connection test non-2xx — %s %d.",
                    url,
                    response.status_code,
                    extra={
                        "ping_url": url,
                        "status_code": response.status_code,
                        "response_body": body,
                    },
                )
        except Exception as exc:
            logger.error(
                "Connection test failed — %s: %s.",
                url,
                exc,
                extra={
                    "ping_url": url,
                    "error": str(exc),
                },
            )
