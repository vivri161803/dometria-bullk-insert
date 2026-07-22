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


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("Authorization")
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def require_api_key(request: Request) -> None:
    """Reject the request with 401 unless a valid API key is presented.

    No-op when no key is configured (auth disabled). The comparison is
    constant-time to avoid leaking the key via timing.
    """
    cfg = request.app.state.settings
    expected: str = cfg.API_KEY
    if not expected:
        return  # auth disabled — no key configured

    provided_header = request.headers.get(cfg.API_KEY_HEADER)
    provided_bearer = _bearer_token(request)
    is_header_valid = (
        provided_header is not None and secrets.compare_digest(provided_header, expected)
    )
    is_bearer_valid = (
        provided_bearer is not None and secrets.compare_digest(provided_bearer, expected)
    )

    if not is_header_valid and not is_bearer_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": f"Bearer, {cfg.API_KEY_HEADER}"},
        )
