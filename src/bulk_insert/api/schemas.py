"""Request and response schemas for the FastAPI extraction endpoint."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from bulk_insert.models.asset import Asset
from bulk_insert.models.pipeline import PipelineError

__all__: list[str] = [
    "MAX_TEXT_CHARS",
    "ProcessRequest",
    "ProcessResponse",
    "ProcedureDispatchRequest",
    "ProcedureDispatchResponse",
    "ErrorResponse",
    "HealthResponse",
    "SetModelRequest",
    "ModelConfigResponse",
]

# Static upper bound on the document text field (shape validation → 422). The
# dynamic byte-size guard (Settings.MAX_REQUEST_BYTES → 413) rejects oversized
# request bodies at the edge before parsing; this bounds the parsed field.
MAX_TEXT_CHARS = 1_000_000


class ProcessRequest(BaseModel):
    """Inbound extraction request from Dometria Connect."""

    id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)


class ProcessResponse(BaseModel):
    """Successful extraction — the full Block 3 v2 Asset + lifted scalars."""

    id: str
    asset: Asset
    confidence: float
    completeness: float | None
    models_used: dict[str, str] = Field(serialization_alias="_models")


class ProcedureDispatchMeta(BaseModel):
    """Metadata attached by Connect to the async dispatch request."""

    original_filename: str = Field(min_length=1)
    mime_type: str = Field(min_length=1)


class ProcedureDispatchRequest(BaseModel):
    """Inbound M2M dispatch request from Dometria Connect."""

    procedure_id: int = Field(ge=1)
    source_text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    user_prompt: str | None = Field(default=None, max_length=50_000)
    meta: ProcedureDispatchMeta


class ProcedureDispatchResponse(BaseModel):
    """Ack body for accepted M2M dispatches."""

    procedure_id: int
    status: Literal["accepted"] = "accepted"


class ErrorResponse(BaseModel):
    """Structured error — echoes id + the PipelineError detail."""

    id: str
    error: PipelineError


class HealthResponse(BaseModel):
    """Response body for the liveness and readiness probes."""

    status: str


class SetModelRequest(BaseModel):
    """Runtime override for one LLM role — one request per model.

    `model` is the bare OpenRouter slug (e.g. ``anthropic/claude-haiku-4.5``);
    it is whitespace-stripped and must be non-empty. Only the two LLM roles are
    mutable — the embedding model is baked into the image and excluded.
    """

    role: Literal["lightweight", "heavy"]
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ModelConfigResponse(BaseModel):
    """The models currently in effect on this container (post-swap)."""

    lightweight: str
    heavy: str
    embedding: str
