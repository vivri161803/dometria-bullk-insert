"""Unit tests for count/aggregate fields (Phase 6.10 Part B.2)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from bulk_insert.blocks.extraction.derive import COMPUTED_RULES, derive_computed
from bulk_insert.blocks.extraction.extract import extract_fields
from bulk_insert.blocks.extraction.ground import (
    FieldAssessment,
    GroundingReport,
    ground_and_assemble,
)
from bulk_insert.blocks.extraction.metadata import FieldMetadata, enrich
from bulk_insert.blocks.extraction.schema import build_extraction_model
from bulk_insert.blocks.extraction.tree import (
    Aggregate,
    Derivation,
    DerivationKind,
    FieldNode,
    TreeField,
)
from bulk_insert.config import Settings
from bulk_insert.models.chunk import Chunk


def _cfg(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _agg_node(name: str = "numero_camere", *, mandatory: bool = True) -> FieldNode:
    """A count-aggregate node as `metadata.resolve` would produce it."""
    return FieldNode(
        name=name,
        lv_path=("a", "b", "c"),
        field_type="integer",
        label=name,
        source="custom_fields",
        constraints=None,
        description=f"desc {name}",
        topic="capienza",
        mandatory=mandatory,
        priority=5 if mandatory else 3,
        derivation=Derivation((f"{name}__items",), "count", DerivationKind.COMPUTED),
        aggregate=Aggregate(rule="count", item_hint="una camera da letto"),
    )


class FakeLLM:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        return self._handler(kwargs)


def _chunk(text: str = "Camere: 1 matrimoniale, master, seconda camera.") -> Chunk:
    return Chunk(
        index=0,
        text=text,
        start_sentence=0,
        end_sentence=0,
        overlap_previous=0,
        is_coref_resolved=True,
        relevance_score=8,
    )


# ---------------------------------------------------------------------------
# count rule
# ---------------------------------------------------------------------------


class TestCountRule:
    def test_len_of_list(self) -> None:
        assert COMPUTED_RULES["count"]([[1, 2, 3]]) == 3

    def test_empty_list_is_zero(self) -> None:
        assert COMPUTED_RULES["count"]([[]]) == 0

    def test_none_companion_is_none(self) -> None:
        assert COMPUTED_RULES["count"]([None]) is None

    def test_wrong_arity_is_none(self) -> None:
        assert COMPUTED_RULES["count"]([]) is None
        assert COMPUTED_RULES["count"]([[1], [2]]) is None


# ---------------------------------------------------------------------------
# metadata + schema wiring
# ---------------------------------------------------------------------------


class TestMetadataWiring:
    def test_aggregate_parsed_and_derivation_synthesized(self) -> None:
        meta = FieldMetadata.from_mapping(
            {"numero_camere": {"aggregate": {"rule": "count", "item_hint": "una camera"}}}
        )
        field = TreeField("numero_camere", "Camere", "integer", True, "custom_fields")
        node = enrich([field], ("a", "b", "c"), metadata=meta)[0]
        assert node.aggregate is not None and node.aggregate.rule == "count"
        assert node.is_derived  # a single `aggregate:` key opts in
        assert node.derivation is not None
        assert node.derivation.rule == "count"
        assert node.derivation.source_fields == ("numero_camere__items",)


class TestSchemaSynthesis:
    def test_companion_synthesized_scalar_excluded(self) -> None:
        model = build_extraction_model([_agg_node()], cfg=_cfg())
        assert "numero_camere__items" in model.model_fields
        assert "numero_camere" not in model.model_fields  # scalar is derived

    def test_plain_scalar_unchanged_without_aggregate(self) -> None:
        plain = FieldNode(
            name="numero_bagni",
            lv_path=("a", "b", "c"),
            field_type="integer",
            label="Bagni",
            source="custom_fields",
            constraints=None,
            description=None,
            topic="capienza",
            mandatory=True,
            priority=5,
        )
        model = build_extraction_model([plain], cfg=_cfg())
        assert "numero_bagni" in model.model_fields
        assert "numero_bagni__items" not in model.model_fields


# ---------------------------------------------------------------------------
# End-to-end: transcribe → count → ground
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_enumerated_items_counted_and_grounded(self) -> None:
        node = _agg_node()

        def extract_handler(kw: dict[str, Any]) -> Any:
            model = kw["response_model"]
            return model(
                numero_camere__items=[
                    {"value": "matrimoniale", "quote": "1 matrimoniale"},
                    {"value": "master", "quote": "master"},
                    {"value": "seconda", "quote": "seconda camera"},
                ]
            )

        out = extract_fields([_chunk()], [node], _cfg(), client=FakeLLM(extract_handler))
        assert out.values["numero_camere__items"] is not None

        derive_computed(out, [node], _cfg())
        assert out.values["numero_camere"] == 3

        def ground_handler(_kw: dict[str, Any]) -> Any:  # no evidence-bearing candidates
            return GroundingReport(assessments=[FieldAssessment(field="x", passed=True)])

        res = ground_and_assemble([_chunk()], out, [node], _cfg(), client=FakeLLM(ground_handler))
        assert res.record["numero_camere"] == 3
        assert "numero_camere__items" not in res.record  # companion is internal
        assert node.name not in res.review_queue  # mandatory satisfied
        assert res.evidence["numero_camere"] == "1 matrimoniale | master | seconda camera"

    def test_missing_enumeration_stays_none(self) -> None:
        node = _agg_node()

        def extract_handler(kw: dict[str, Any]) -> Any:
            return kw["response_model"](numero_camere__items=None)

        out = extract_fields([_chunk()], [node], _cfg(), client=FakeLLM(extract_handler))
        derive_computed(out, [node], _cfg())
        assert out.values["numero_camere"] is None
        res = ground_and_assemble(
            [_chunk()],
            out,
            [node],
            _cfg(),
            client=FakeLLM(lambda _k: GroundingReport(assessments=[])),
        )
        assert node.name in res.review_queue  # mandatory + null → review
