"""Route handlers for POST /process, GET /health, and GET /ready."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bulk_insert.api.schemas import (
    ErrorResponse,
    HealthResponse,
    ProcessRequest,
    ProcessResponse,
)
from bulk_insert.config import Settings
from bulk_insert.pipeline import process_document

__all__: list[str] = ["router"]

router = APIRouter()

# Maps PipelineError.category → HTTP status code.
_STATUS_MAP: dict[str, int] = {
    "bad_input": 400,
    "unprocessable": 422,
    "retryable": 502,
    "server": 500,
}


@router.post("/process", response_model=ProcessResponse)
def process(request_body: ProcessRequest, request: Request) -> ProcessResponse | JSONResponse:
    """Run the extraction pipeline on a single document."""
    cfg: Settings = request.app.state.settings

    result = process_document(request_body.id, request_body.text, cfg)

    if result.ok and result.asset is not None:
        models_used = {
            "lightweight": cfg.MODEL_LIGHTWEIGHT,
            "heavy": cfg.MODEL_HEAVY,
            "embedding": cfg.EMBEDDING_MODEL,
        }
        return ProcessResponse(
            id=result.id,
            asset=result.asset,
            confidence=result.asset.confidence,
            completeness=result.asset.completeness,
            models_used=models_used,
        )

    # Error path — guaranteed to have result.error set.
    assert result.error is not None  # noqa: S101
    status = _STATUS_MAP.get(result.error.category, 500)
    body = ErrorResponse(id=result.id, error=result.error)
    return JSONResponse(status_code=status, content=body.model_dump())


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness probe — always returns 200."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=HealthResponse)
def ready(request: Request) -> HealthResponse | JSONResponse:
    """Readiness probe — 200 once warm-up is complete, 503 otherwise."""
    is_ready: bool = getattr(request.app.state, "ready", False)
    if is_ready:
        return HealthResponse(status="ready")
    return JSONResponse(
        status_code=503,
        content=HealthResponse(status="not ready").model_dump(),
    )
