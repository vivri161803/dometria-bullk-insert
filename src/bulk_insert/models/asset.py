"""Asset Pydantic model."""

from typing import Any

from pydantic import BaseModel, Field


class Asset(BaseModel):
    """The final extraction output for a single contract.

    Fields
    ------
    contract_id:
        Identifier of the ``Contract`` whose schema was populated.
    data:
        The populated JSON object (schema-compliant dict).
    confidence:
        Model confidence in the extraction, in [0.0, 1.0].
    evidence:
        Optional per-field verbatim source quotes (Block 3 v2 tree-driven
        extractor).  ``None`` for legacy/simple extractions.
    flags:
        Optional per-field agreement/grounding flags (e.g. voting disagreements,
        reconciliation mismatches, ungrounded values).  ``None`` when unused.
    review_queue:
        Optional list of mandatory field keys that could not be filled and need
        human review.  ``None`` when unused.
    """

    contract_id: str
    data: dict[str, object]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: dict[str, str] | None = None
    flags: dict[str, Any] | None = None
    review_queue: list[str] | None = None
