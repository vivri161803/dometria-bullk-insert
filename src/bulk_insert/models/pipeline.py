"""Pipeline result models — the structured outcome of :func:`process_document`.

The orchestrator never raises: every call returns a :class:`PipelineResult`
carrying the original ``id``.  On success ``asset`` + ``confidence`` are
populated; on failure ``error`` describes the failing ``stage`` and a
``category`` rich enough for the (Phase 6) HTTP layer to choose a status code
without re-inspecting exception types.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from bulk_insert.models.asset import Asset

# Stage of the pipeline that produced an outcome.
PipelineStage = Literal["input", "preprocess", "contract", "extract"]
# Coarse failure class — drives the downstream HTTP status mapping.
#   bad_input    -> 400   (empty / blank text)
#   unprocessable-> 422   (no qualifying chunk; document cannot be categorized)
#   server       -> 500   (missing/malformed schema, unexpected error)
#   retryable    -> 502   (transient upstream LLM / network failure)
ErrorCategory = Literal["bad_input", "unprocessable", "server", "retryable"]


class PipelineError(BaseModel):
    """Structured description of a pipeline failure."""

    stage: PipelineStage
    category: ErrorCategory
    message: str


class PipelineResult(BaseModel):
    """Outcome of running the full pipeline on a single document.

    ``ok`` discriminates the two shapes:

    * success — ``ok=True``, ``asset`` and ``confidence`` set, ``error=None``.
    * failure — ``ok=False``, ``error`` set, ``asset``/``confidence`` ``None``.

    ``id`` is the opaque document identifier, echoed on every result.
    """

    id: str
    ok: bool
    asset: Asset | None = None
    confidence: float | None = None
    error: PipelineError | None = None

    @classmethod
    def success(cls, doc_id: str, asset: Asset) -> PipelineResult:
        """Build a success result, lifting ``confidence`` from the asset."""
        return cls(id=doc_id, ok=True, asset=asset, confidence=asset.confidence)

    @classmethod
    def failure(
        cls,
        doc_id: str,
        stage: PipelineStage,
        category: ErrorCategory,
        message: str,
    ) -> PipelineResult:
        """Build a failure result carrying a structured :class:`PipelineError`."""
        return cls(
            id=doc_id,
            ok=False,
            error=PipelineError(stage=stage, category=category, message=message),
        )


__all__: list[str] = [
    "PipelineResult",
    "PipelineError",
    "PipelineStage",
    "ErrorCategory",
]
