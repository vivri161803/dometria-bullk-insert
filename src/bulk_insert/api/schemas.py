"""Request and response schemas for the FastAPI extraction endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field

from bulk_insert.models.asset import Asset
from bulk_insert.models.pipeline import PipelineError

__all__: list[str] = [
    "ProcessRequest",
    "ProcessResponse",
    "ErrorResponse",
    "HealthResponse",
]


class ProcessRequest(BaseModel):
    """Inbound extraction request from Dometria Connect."""

    id: str
    text: str = Field(min_length=1)


class ProcessResponse(BaseModel):
    """Successful extraction — the full Block 3 v2 Asset + lifted scalars."""

    id: str
    asset: Asset
    confidence: float
    completeness: float | None
    models_used: dict[str, str] = Field(serialization_alias="_models")


class ErrorResponse(BaseModel):
    """Structured error — echoes id + the PipelineError detail."""

    id: str
    error: PipelineError


class HealthResponse(BaseModel):
    """Response body for the liveness and readiness probes."""

    status: str
