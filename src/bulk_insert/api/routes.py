"""Route handlers for POST /process, GET /health, and GET /ready."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse

from bulk_insert.api.m2m import run_procedure_callback_flow
from bulk_insert.api.schemas import (
    ErrorResponse,
    HealthResponse,
    ModelConfigResponse,
    ProcessRequest,
    ProcessResponse,
    ProcedureDispatchRequest,
    ProcedureDispatchResponse,
    SetModelRequest,
)
from bulk_insert.api.security import require_api_key
from bulk_insert.config import Settings
from bulk_insert.logging_config import set_log_id
from bulk_insert.pipeline import process_document

__all__: list[str] = ["router"]

router = APIRouter()
logger = logging.getLogger(__name__)

# Applied to every non-probe route: enforce the API key when one is configured.
_AUTH = [Depends(require_api_key)]

# Maps PipelineError.category → HTTP status code.
_STATUS_MAP: dict[str, int] = {
    "bad_input": 400,
    "unprocessable": 422,
    "retryable": 502,
    "server": 500,
}


def _missing_process_env_vars(cfg: Settings) -> list[str]:
    """Required environment variables for synchronous processing."""
    missing: list[str] = []
    if not cfg.OPENROUTER_API_KEY.strip():
        missing.append("OPENROUTER_API_KEY")
    return missing


def _missing_procedure_env_vars(cfg: Settings) -> list[str]:
    """Required environment variables for async procedure dispatch/callback."""
    missing = _missing_process_env_vars(cfg)
    if not cfg.CONNECT_CALLBACK_TOKEN.strip():
        missing.append("CONNECT_CALLBACK_TOKEN")
    if not cfg.CONNECT_BASE_URL.strip():
        missing.append("CONNECT_BASE_URL")
    return missing


def _service_misconfigured_response(path: str, missing_env_vars: list[str]) -> JSONResponse:
    """Return a 503 when required runtime configuration is missing."""
    missing = ", ".join(missing_env_vars)
    logger.error(
        "Rejected %s due to missing required environment variables: %s",
        path,
        missing,
        extra={"path": path, "missing_env_vars": missing_env_vars},
    )
    return JSONResponse(
        status_code=503,
        content={"detail": f"service misconfigured: missing required env vars: {missing}"},
    )


@router.post("/process", response_model=ProcessResponse, dependencies=_AUTH, tags=["extraction"])
def process(request_body: ProcessRequest, request: Request) -> ProcessResponse | JSONResponse:
    """Run the extraction pipeline on a single document."""
    cfg: Settings = request.app.state.settings
    missing_env_vars = _missing_process_env_vars(cfg)
    if missing_env_vars:
        return _service_misconfigured_response(path="/process", missing_env_vars=missing_env_vars)

    # Tag every downstream log line with the caller's document id.
    set_log_id(request_body.id)
    text_chars = len(request_body.text)
    logger.info(
        "Accepted POST /process for document %s (%d chars).",
        request_body.id,
        text_chars,
        extra={"path": "/process", "text_chars": text_chars},
    )
    result = process_document(request_body.id, request_body.text, cfg)

    if result.ok and result.asset is not None:
        models_used = {
            "lightweight": cfg.MODEL_LIGHTWEIGHT,
            "heavy": cfg.MODEL_HEAVY,
            "embedding": cfg.EMBEDDING_MODEL,
        }
        logger.info(
            "POST /process succeeded for document %s — contract=%s confidence=%.3f.",
            result.id,
            result.asset.contract_id,
            result.asset.confidence,
            extra={
                "path": "/process",
                "contract_id": result.asset.contract_id,
                "confidence": result.asset.confidence,
                "completeness": result.asset.completeness,
            },
        )
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
    logger.warning(
        "POST /process failed for document %s — HTTP %d category=%s stage=%s: %s",
        result.id,
        status,
        result.error.category,
        result.error.stage,
        result.error.message,
        extra={
            "path": "/process",
            "status_code": status,
            "error_category": result.error.category,
            "stage": result.error.stage,
        },
    )
    body = ErrorResponse(id=result.id, error=result.error)
    return JSONResponse(status_code=status, content=body.model_dump())


@router.post(
    "/procedures",
    status_code=202,
    response_model=ProcedureDispatchResponse,
    dependencies=_AUTH,
    tags=["extraction"],
)
def dispatch_procedure(
    request_body: ProcedureDispatchRequest,
    background_tasks: BackgroundTasks,
    request: Request,
) -> ProcedureDispatchResponse | JSONResponse:
    """Accept an async M2M dispatch and process it after the HTTP ack."""
    cfg: Settings = request.app.state.settings
    missing_env_vars = _missing_procedure_env_vars(cfg)
    if missing_env_vars:
        set_log_id(str(request_body.procedure_id))
        return _service_misconfigured_response(path="/procedures", missing_env_vars=missing_env_vars)

    set_log_id(str(request_body.procedure_id))
    text_chars = len(request_body.source_text)
    background_tasks.add_task(
        run_procedure_callback_flow,
        request_body.procedure_id,
        request_body.source_text,
        request_body.user_prompt,
        cfg,
    )
    logger.info(
        "Accepted POST /procedures for procedure %s (%d chars) — processing asynchronously.",
        request_body.procedure_id,
        text_chars,
        extra={
            "path": "/procedures",
            "procedure_id": request_body.procedure_id,
            "text_chars": text_chars,
        },
    )
    return ProcedureDispatchResponse(procedure_id=request_body.procedure_id)


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
