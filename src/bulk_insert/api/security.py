"""API-key authentication for the extraction endpoints (Phase 9).

A FastAPI dependency guarding the non-probe routes. When ``Settings.API_KEY`` is
non-empty, callers must present a matching value in the configured header
(default ``X-API-Key``); otherwise the request is rejected with ``401``. An empty
key (the default) disables the check for local development — set the key in the
deployment environment to require it. The liveness/readiness probes are left
open so orchestrators can poll them without a credential.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

__all__: list[str] = ["require_api_key"]


def require_api_key(request: Request) -> None:
    """Reject the request with 401 unless a valid API key is presented.

    No-op when no key is configured (auth disabled). The comparison is
    constant-time to avoid leaking the key via timing.
    """
    cfg = request.app.state.settings
    expected: str = cfg.API_KEY
    if not expected:
        return  # auth disabled — no key configured

    provided = request.headers.get(cfg.API_KEY_HEADER)
    if provided is None or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": cfg.API_KEY_HEADER},
        )
