"""Asset Pydantic model."""

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
    """

    contract_id: str
    data: dict[str, object]
    confidence: float = Field(ge=0.0, le=1.0)
