"""Contract Pydantic model."""

from pydantic import BaseModel, Field

from bulk_insert.models.categories import Level1Literal


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
        Defaults to ``{}`` so that Block 2 can return a Contract without
        populating the schema (schema loading is deferred to Block 3).
    """

    lv1: str
    lv2: str
    lv3: str
    schema_def: dict[str, object] = Field(default_factory=dict, alias="schema")

    model_config = {"populate_by_name": True}


class Level1Prediction(BaseModel):
    """Instructor response model for lv1 classification.

    The Literal type constrains the LLM output to exactly
    the three valid level-1 category codes.
    """

    lv1: Level1Literal
