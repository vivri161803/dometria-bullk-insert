"""ASGI middleware — request-size guard + structured access log (Phase 9).

One middleware handles two edge concerns before/around routing:

* **Body-size guard** — reject a request whose ``Content-Length`` exceeds
  ``MAX_REQUEST_BYTES`` with ``413`` before the body is read/parsed, so an
  oversized payload can't exhaust memory.
* **Correlation id + access log** — assign each request a correlation id (the
  caller's ``X-Request-ID`` header if present, else a generated one), bind it to
  the logging context so downstream lines are tagged, echo it back on the
  response header, and emit one structured access line with method, path,
  status, and ``duration_ms``. Liveness/readiness probes (``/health``,
  ``/ready``) are excluded from the access log to avoid polluting stdout.
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from bulk_insert.logging_config import set_log_id

__all__: list[str] = ["RequestContextMiddleware"]

logger = logging.getLogger("bulk_insert.api.access")

_REQUEST_ID_HEADER = "X-Request-ID"
_PROBE_PATHS = frozenset({"/health", "/ready"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Size-guard, correlation-id, and structured access logging."""

    def __init__(self, app: object, max_request_bytes: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._max_bytes = max_request_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(_REQUEST_ID_HEADER) or uuid.uuid4().hex
        # Set before call_next so the (copied) downstream context inherits it.
        set_log_id(request_id)

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            if declared > self._max_bytes:
                logger.warning(
                    "Rejected oversized request (%s bytes > %s).",
                    content_length,
                    self._max_bytes,
                )
                return JSONResponse(
                    status_code=413,
                    content={"detail": "request body too large"},
                    headers={_REQUEST_ID_HEADER: request_id},
                )

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers[_REQUEST_ID_HEADER] = request_id

        if request.url.path not in _PROBE_PATHS:
            logger.info(
                "%s %s -> %d",
                request.method,
                request.url.path,
                response.status_code,
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
        return response
