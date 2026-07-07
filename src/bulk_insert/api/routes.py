"""Route handlers for POST /process, GET /health, and GET /ready."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from bulk_insert.api.schemas import (
    ErrorResponse,
    HealthResponse,
    ModelConfigResponse,
    ProcessRequest,
    ProcessResponse,
    SetModelRequest,
)
from bulk_insert.api.security import require_api_key
from bulk_insert.config import Settings
from bulk_insert.logging_config import set_log_id
from bulk_insert.pipeline import process_document

__all__: list[str] = ["router"]

router = APIRouter()

# Applied to every non-probe route: enforce the API key when one is configured.
_AUTH = [Depends(require_api_key)]

# Maps PipelineError.category → HTTP status code.
_STATUS_MAP: dict[str, int] = {
    "bad_input": 400,
    "unprocessable": 422,
    "retryable": 502,
    "server": 500,
}


@router.post("/process", response_model=ProcessResponse, dependencies=_AUTH, tags=["extraction"])
def process(request_body: ProcessRequest, request: Request) -> ProcessResponse | JSONResponse:
    """Run the extraction pipeline on a single document."""
    cfg: Settings = request.app.state.settings

    # Tag every downstream log line with the caller's document id.
    set_log_id(request_body.id)
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


def _model_config(cfg: Settings) -> ModelConfigResponse:
    """Snapshot the three model slugs currently in effect."""
    return ModelConfigResponse(
        lightweight=cfg.MODEL_LIGHTWEIGHT,
        heavy=cfg.MODEL_HEAVY,
        embedding=cfg.EMBEDDING_MODEL,
    )


@router.get(
    "/config/models", response_model=ModelConfigResponse, dependencies=_AUTH, tags=["config"]
)
def get_models(request: Request) -> ModelConfigResponse:
    """Report the LLM/embedding models currently in effect on this container."""
    cfg: Settings = request.app.state.settings
    return _model_config(cfg)


@router.post(
    "/config/model", response_model=ModelConfigResponse, dependencies=_AUTH, tags=["config"]
)
def set_model(
    request_body: SetModelRequest, request: Request
) -> ModelConfigResponse | JSONResponse:
    """Swap one LLM slug on the running container (per-replica, ephemeral).

    Sends one role per request. The change applies to subsequent /process calls
    on this process only — it is not shared across replicas and resets on restart.
    """
    cfg: Settings = request.app.state.settings

    if not cfg.RUNTIME_MODEL_OVERRIDE_ENABLED:
        return JSONResponse(
            status_code=403,
            content={"detail": "runtime model override is disabled"},
        )

    field = "MODEL_LIGHTWEIGHT" if request_body.role == "lightweight" else "MODEL_HEAVY"
    new_cfg = cfg.model_copy(update={field: request_body.model})
    # Atomic pointer swap — in-flight requests keep their captured cfg.
    request.app.state.settings = new_cfg
    return _model_config(new_cfg)


@router.get("/health", response_model=HealthResponse, tags=["probes"])
def health() -> HealthResponse:
    """Liveness probe — always returns 200."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=HealthResponse, tags=["probes"])
def ready(request: Request) -> HealthResponse | JSONResponse:
    """Readiness probe — 200 once warm-up is complete, 503 otherwise."""
    is_ready: bool = getattr(request.app.state, "ready", False)
    if is_ready:
        return HealthResponse(status="ready")
    return JSONResponse(
        status_code=503,
        content=HealthResponse(status="not ready").model_dump(),
    )
