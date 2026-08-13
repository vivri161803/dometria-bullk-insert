"""One-shot connectivity probe: Bulk Insert → Dometria Connect connection-test API.

Calls the same endpoint used by the in-process ``ConnectionTestLoop``:

    GET {CONNECT_BASE_URL}/api/v1/integrations/dometria-bulk-insert/connection-test
    Authorization: Bearer {CONNECT_CALLBACK_TOKEN}

Logs every request parameter and the outcome (HTTP status, body, or exception).

Usage
-----
    uv run python scripts/test_connect_connection.py
    uv run python scripts/test_connect_connection.py --base-url https://connect.dometria.ai
    CONNECT_CALLBACK_TOKEN=... uv run python scripts/test_connect_connection.py

Credentials and defaults come from environment / ``.env`` via ``Settings``
(``CONNECT_BASE_URL``, ``CONNECT_CALLBACK_TOKEN``, ``CONNECT_PING_TIMEOUT_SECONDS``).
CLI flags override those values for the single call.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

import httpx

from bulk_insert.config import Settings

_CONNECTION_TEST_PATH = "/api/v1/integrations/dometria-bulk-insert/connection-test"

logger = logging.getLogger("test_connect_connection")


def _redact_bearer(token: str) -> str:
    """Show enough of the token to confirm identity without dumping the secret."""
    if not token:
        return "(empty)"
    if len(token) <= 8:
        return f"*** (len={len(token)})"
    return f"{token[:4]}…{token[-4:]} (len={len(token)})"


def _build_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}{_CONNECTION_TEST_PATH}"


def run_probe(
    *,
    base_url: str,
    token: str,
    timeout_seconds: float,
) -> int:
    """Execute one GET against Connect and log parameters + outcome.

    Returns a process exit code: 0 on HTTP 2xx, 1 otherwise.
    """
    url = _build_url(base_url)
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    method = "GET"
    log_params: dict[str, Any] = {
        "method": method,
        "url": url,
        "path": _CONNECTION_TEST_PATH,
        "base_url": base_url.rstrip("/"),
        "timeout_seconds": timeout_seconds,
        "authorization": f"Bearer {_redact_bearer(token)}",
        "has_token": bool(token),
        "headers_sent": sorted(headers.keys()),
    }

    logger.info("Connection-test call — parameters: %s", log_params)

    try:
        response = httpx.get(url, headers=headers, timeout=timeout_seconds)
    except Exception as exc:
        logger.error(
            "Connection-test FAILED — network/timeout error.",
            extra={
                **log_params,
                "outcome": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        logger.error("Exception: %s: %s", type(exc).__name__, exc)
        return 1

    body = response.text[:2000]
    outcome = "ok" if response.is_success else "non_2xx"
    level = logging.INFO if response.is_success else logging.WARNING
    logger.log(
        level,
        "Connection-test %s — HTTP %s.",
        outcome.upper(),
        response.status_code,
        extra={
            **log_params,
            "outcome": outcome,
            "status_code": response.status_code,
            "response_headers": dict(response.headers),
            "response_body": body,
            "elapsed_seconds": response.elapsed.total_seconds(),
        },
    )
    logger.log(
        level,
        "status_code=%s elapsed=%.3fs body=%s",
        response.status_code,
        response.elapsed.total_seconds(),
        body,
    )
    return 0 if response.is_success else 1


def main(argv: list[str] | None = None) -> int:
    cfg = Settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=cfg.CONNECT_BASE_URL,
        help=f"Connect base URL (default from Settings: {cfg.CONNECT_BASE_URL})",
    )
    parser.add_argument(
        "--token",
        default=cfg.CONNECT_CALLBACK_TOKEN,
        help="Sanctum Bearer token (default: CONNECT_CALLBACK_TOKEN from env/.env)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=cfg.CONNECT_PING_TIMEOUT_SECONDS,
        help=(
            "HTTP timeout in seconds "
            f"(default from Settings: {cfg.CONNECT_PING_TIMEOUT_SECONDS})"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    if not args.token:
        logger.warning(
            "CONNECT_CALLBACK_TOKEN is empty — request will go without Authorization."
        )

    return run_probe(
        base_url=args.base_url,
        token=args.token,
        timeout_seconds=args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
