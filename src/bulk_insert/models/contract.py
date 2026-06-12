"""Contract Pydantic model."""

from pydantic import BaseModel, Field


class Contract(BaseModel):
    """Represents a target JSON schema organised in a three-level hierarchy.

    Fields
    ------
    lv1:
        Top-level category (e.g. "Real Estate", "Employment").
    lv2:
        Sub-category within ``lv1``.
    lv3:
        Leaf-level label within ``lv2``.
    schema:
        The actual JSON schema dict that the Asset Extraction block must populate.
        Stored internally as ``schema_def`` to avoid shadowing Pydantic's
        built-in ``schema`` class method; serialised as ``"schema"`` in JSON.
    """

    lv1: str
    lv2: str
    lv3: str
    schema_def: dict[str, object] = Field(alias="schema")

    model_config = {"populate_by_name": True}
