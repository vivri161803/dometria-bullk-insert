"""Unit tests for Block 3 v2 — Tree-Driven Asset Extraction (Phase 6)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, get_args

import pytest
from pydantic import BaseModel

from bulk_insert.blocks.extraction import AssetExtractionError, extract_asset
from bulk_insert.blocks.extraction.derive import derive_computed, derive_judged
from bulk_insert.blocks.extraction.extract import (
    SELF_CONSISTENCY,
    STANDARD,
    ExtractionOutput,
    extract_fields,
    group_strategy,
    partition_by_topic,
)
from bulk_insert.blocks.extraction.ground import (
    FieldAssessment,
    GroundingReport,
    ground_and_assemble,
)
from bulk_insert.blocks.extraction.metadata import FieldMetadata, enrich
from bulk_insert.blocks.extraction.schema import (
    NOT_PRESENT,
    build_extraction_model,
    is_extractable,
    quote_field_name,
)
from bulk_insert.blocks.extraction.tree import (
    Derivation,
    DerivationKind,
    FieldNode,
    TreeField,
    select_tree_fields,
    signature_for,
)
from bulk_insert.config import Settings
from bulk_insert.models.chunk import Chunk
from bulk_insert.models.contract import Contract
from bulk_insert.models.contract_loader import ContractNotFoundError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _chunk(index: int = 0, text: str = "Ristorante Da Mario in centro, 40 coperti.") -> Chunk:
    return Chunk(
        index=index,
        text=text,
        start_sentence=0,
        end_sentence=0,
        overlap_previous=0,
        is_coref_resolved=True,
        relevance_score=8,
    )


def _node(
    name: str,
    *,
    topic: str = "attributes",
    field_type: str = "text",
    mandatory: bool = False,
    priority: int = 3,
    enum: list[str] | None = None,
    needs_evidence: bool = True,
    derivation: Derivation | None = None,
    source: str = "custom_fields",
    description: str | None = None,
) -> FieldNode:
    return FieldNode(
        name=name,
        lv_path=("commercial-activities", "food-beverage", "restaurant"),
        field_type=field_type,
        label=name.replace("_", " ").title(),
        source=source,
        constraints=None,
        description=description,
        topic=topic,
        mandatory=mandatory,
        priority=priority,
        enum=enum,
        needs_evidence=needs_evidence,
        derivation=derivation,
    )


class FakeLLM:
    """Instructor stand-in: ``create`` delegates to a per-call handler."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._handler(kwargs)


def _restaurant_contract(lv3: str = "restaurant") -> Contract:
    return Contract(lv1="commercial-activities", lv2="food-beverage", lv3=lv3)


# ---------------------------------------------------------------------------
# Stage 1 — tree selection
# ---------------------------------------------------------------------------


class TestTreeSelection:
    def test_selects_product_fields(self) -> None:
        fields = select_tree_fields(_restaurant_contract())
        names = {f.name for f in fields}
        assert "name" in names
        assert "commercial_activities_zona" in names
        # Every selected field is structural.
        assert all(isinstance(f, TreeField) for f in fields)

    def test_signature_is_stable_and_sorted(self) -> None:
        fields = select_tree_fields(_restaurant_contract())
        sig = signature_for(fields)
        assert sig == tuple(sorted(sig))
        assert signature_for(fields) == sig

    def test_unknown_path_raises_keyerror(self) -> None:
        bad = Contract(lv1="commercial-activities", lv2="food-beverage", lv3="does-not-exist")
        with pytest.raises(KeyError):
            select_tree_fields(bad)


# ---------------------------------------------------------------------------
# metadata enrichment
# ---------------------------------------------------------------------------


class TestMetadataEnrichment:
    def _tree_field(
        self, name: str, field_type: str = "select", source: str = "custom_fields"
    ) -> TreeField:
        return TreeField(
            name=name,
            label=name.title(),
            field_type=field_type,
            is_required=False,
            source=source,
        )

    def test_compiled_enum_injected(self) -> None:
        meta = FieldMetadata.from_mapping(
            {"zona": {"topic": "loc", "enum": {"status": "compiled", "values": ["a", "b"]}}}
        )
        (node,) = enrich([self._tree_field("zona")], ("l1", "l2", "l3"), metadata=meta)
        assert node.enum == ["a", "b"]

    def test_pending_enum_is_free_text(self) -> None:
        meta = FieldMetadata.from_mapping(
            {"zona": {"enum": {"status": "pending", "values": ["a", "b"]}}}
        )
        (node,) = enrich([self._tree_field("zona")], ("l1", "l2", "l3"), metadata=meta)
        assert node.enum is None

    def test_description_and_topic_defaults(self) -> None:
        meta = FieldMetadata.from_mapping({})  # nothing configured
        (node,) = enrich(
            [self._tree_field("addr", field_type="text", source="native_location")],
            ("l1", "l2", "l3"),
            metadata=meta,
        )
        assert node.topic == "location"  # fallback from source
        assert node.prompt_context == "Addr"  # falls back to label

    def test_derivation_parsed(self) -> None:
        meta = FieldMetadata.from_mapping(
            {
                "total": {
                    "derivation": {
                        "source_fields": ["a", "b"],
                        "rule": "sum",
                        "kind": "computed",
                    }
                }
            }
        )
        (node,) = enrich(
            [self._tree_field("total", field_type="integer")], ("l1", "l2", "l3"), metadata=meta
        )
        assert node.derivation is not None
        assert node.derivation.kind is DerivationKind.COMPUTED
        assert node.derivation.source_fields == ("a", "b")
        assert node.is_derived


# ---------------------------------------------------------------------------
# Stage 2 — schema generation
# ---------------------------------------------------------------------------


class TestSchemaGeneration:
    def test_quote_fields_synthesised(self) -> None:
        model = build_extraction_model([_node("name", needs_evidence=True)])
        assert "name" in model.model_fields
        assert quote_field_name("name") in model.model_fields

    def test_no_quote_when_evidence_disabled(self) -> None:
        model = build_extraction_model([_node("x", needs_evidence=False)])
        assert quote_field_name("x") not in model.model_fields

    def test_not_present_in_every_compiled_enum(self) -> None:
        model = build_extraction_model(
            [_node("zona", field_type="select", enum=["centro", "periferia"])]
        )
        args = get_args(model.model_fields["zona"].annotation)
        literal = args[0]
        assert NOT_PRESENT in get_args(literal)

    def test_validator_requires_quote(self) -> None:
        model = build_extraction_model([_node("name")])
        with pytest.raises(ValueError, match="verbatim source quote"):
            model(name="Mario")  # no quote

    def test_validator_accepts_not_present_without_quote(self) -> None:
        model = build_extraction_model([_node("zona", field_type="select", enum=["centro"])])
        inst = model(zona=NOT_PRESENT)
        assert inst.zona == NOT_PRESENT

    def test_validator_accepts_value_with_quote(self) -> None:
        model = build_extraction_model([_node("name")])
        inst = model(name="Mario", name_quote="Ristorante Mario")
        assert inst.name == "Mario"

    def test_excludes_derived_and_excluded_types(self) -> None:
        nodes = [
            _node("kept"),
            _node("geo", field_type="foreign_id"),
            _node("coords", field_type="decimal_pair"),
            _node("derived", derivation=Derivation(("a",), "sum", DerivationKind.COMPUTED)),
            _node("geo_txt", field_type="integer", source="native_location"),
        ]
        model = build_extraction_model(nodes, cfg=_cfg())
        assert "kept" in model.model_fields
        for excluded in ("geo", "coords", "derived", "geo_txt"):
            assert excluded not in model.model_fields

    def test_model_is_cached_by_signature(self) -> None:
        n = [_node("cached_field_unique")]
        assert build_extraction_model(n) is build_extraction_model(list(n))

    def test_is_extractable_rules(self) -> None:
        assert is_extractable(_node("t"), ("foreign_id", "decimal_pair"))
        assert not is_extractable(_node("t", field_type="foreign_id"), ("foreign_id",))
        assert not is_extractable(
            _node("t", derivation=Derivation((), "sum", DerivationKind.COMPUTED)), ()
        )


# ---------------------------------------------------------------------------
# Stages 3-4 — partition + extraction + voting
# ---------------------------------------------------------------------------


class TestPartitionAndStrategy:
    def test_partition_by_topic(self) -> None:
        nodes = [_node("a", topic="x"), _node("b", topic="y"), _node("c", topic="x")]
        groups = partition_by_topic(nodes)
        assert set(groups) == {"x", "y"}
        assert [n.name for n in groups["x"]] == ["a", "c"]

    def test_strategy_self_consistency_when_mandatory_high_priority(self) -> None:
        group = [_node("a"), _node("b", mandatory=True, priority=5)]
        assert group_strategy(group, _cfg(HIGH_PRIORITY=4)) == SELF_CONSISTENCY

    def test_strategy_standard_otherwise(self) -> None:
        group = [_node("a", mandatory=True, priority=2), _node("b", priority=9)]
        assert group_strategy(group, _cfg(HIGH_PRIORITY=4)) == STANDARD


class TestExtractFields:
    def test_standard_extraction_and_not_present_normalised(self) -> None:
        nodes = [
            _node("name", topic="t"),
            _node("zona", topic="t", field_type="select", enum=["centro"]),
        ]

        def handler(kw: dict[str, Any]) -> BaseModel:
            rm = kw["response_model"]
            return rm(name="Mario", name_quote="Ristorante Mario", zona=NOT_PRESENT)

        llm = FakeLLM(handler)
        out = extract_fields([_chunk()], nodes, _cfg(), client=llm)
        assert len(llm.calls) == 1  # one topic group
        assert out.values["name"] == "Mario"
        assert out.values["zona"] is None  # NOT_PRESENT -> None
        assert out.quotes["name"] == "Ristorante Mario"

    def test_self_consistency_unanimous(self) -> None:
        nodes = [_node("seats", topic="cap", field_type="integer", mandatory=True, priority=5)]

        def handler(kw: dict[str, Any]) -> BaseModel:
            return kw["response_model"](seats=40, seats_quote="40 coperti")

        llm = FakeLLM(handler)
        out = extract_fields([_chunk()], nodes, _cfg(SELF_CONSISTENCY_N=3), client=llm)
        assert len(llm.calls) == 3
        assert out.values["seats"] == 40
        assert "seats" not in out.flags  # unanimous → no flag

    def test_self_consistency_disagreement_nulls_and_flags(self) -> None:
        nodes = [_node("seats", topic="cap", field_type="integer", mandatory=True, priority=5)]
        answers = iter([40, 50, 60])

        def handler(kw: dict[str, Any]) -> BaseModel:
            v = next(answers)
            return kw["response_model"](seats=v, seats_quote=f"{v} coperti")

        llm = FakeLLM(handler)
        out = extract_fields([_chunk()], nodes, _cfg(SELF_CONSISTENCY_N=3), client=llm)
        assert out.values["seats"] is None
        assert out.flags["seats"]["agreement"] == "disagreement"

    def test_self_consistency_majority(self) -> None:
        nodes = [_node("seats", topic="cap", field_type="integer", mandatory=True, priority=5)]
        answers = iter([40, 40, 60])

        def handler(kw: dict[str, Any]) -> BaseModel:
            v = next(answers)
            return kw["response_model"](seats=v, seats_quote=f"{v} coperti")

        llm = FakeLLM(handler)
        out = extract_fields([_chunk()], nodes, _cfg(SELF_CONSISTENCY_N=3), client=llm)
        assert out.values["seats"] == 40
        assert out.flags["seats"]["agreement"] == "majority"


# ---------------------------------------------------------------------------
# Stages 5-7 — derivation
# ---------------------------------------------------------------------------


class TestComputedDerivation:
    def test_sum_rule(self) -> None:
        out = ExtractionOutput(values={"a": 10, "b": 5}, quotes={"a": "10", "b": "5"})
        node = _node(
            "total",
            field_type="integer",
            derivation=Derivation(("a", "b"), "sum", DerivationKind.COMPUTED),
        )
        derive_computed(out, [node])
        assert out.values["total"] == 15.0
        assert out.flags["total"]["derived"] == "computed"
        assert out.flags["total"]["source_quotes"] == {"a": "10", "b": "5"}

    def test_ratio_rule(self) -> None:
        out = ExtractionOutput(values={"a": 10, "b": 4})
        node = _node("r", derivation=Derivation(("a", "b"), "ratio", DerivationKind.COMPUTED))
        derive_computed(out, [node])
        assert out.values["r"] == 2.5

    def test_unknown_rule_flags_error(self) -> None:
        out = ExtractionOutput(values={"a": 1})
        node = _node("x", derivation=Derivation(("a",), "bogus", DerivationKind.COMPUTED))
        derive_computed(out, [node])
        assert out.flags["x"]["error"] == "unknown_rule"

    def test_reconcile_mismatch_flagged(self) -> None:
        out = ExtractionOutput(values={"a": 10, "b": 5, "total": 99})
        node = _node("total", derivation=Derivation(("a", "b"), "sum", DerivationKind.COMPUTED))
        derive_computed(out, [node])
        assert out.flags["total"]["reconcile"]["agreement"] is False
        assert out.values["total"] == 15.0  # computed wins

    def test_missing_source_yields_none(self) -> None:
        out = ExtractionOutput(values={"a": 10})  # b missing
        node = _node("total", derivation=Derivation(("a", "b"), "sum", DerivationKind.COMPUTED))
        derive_computed(out, [node])
        assert out.values["total"] is None


class TestJudgedDerivation:
    def test_judged_uses_primitives_only(self) -> None:
        out = ExtractionOutput(values={"a": "x", "b": "y"}, quotes={"a": "qa", "b": "qb"})
        node = _node("verdict", derivation=Derivation(("a", "b"), "reason", DerivationKind.JUDGED))

        def handler(kw: dict[str, Any]) -> BaseModel:
            # The raw document must never be in the prompt — only primitives.
            content = kw["messages"][1]["content"]
            assert "Ristorante Da Mario" not in content
            assert "qa" in content
            return kw["response_model"](value="ok", justification="because a and b")

        llm = FakeLLM(handler)
        derive_judged(out, [node], _cfg(), client=llm)
        assert out.values["verdict"] == "ok"
        assert out.flags["verdict"]["derived"] == "judged"

    def test_no_judged_fields_is_noop(self) -> None:
        out = ExtractionOutput(values={"a": 1})

        def handler(_kw: dict[str, Any]) -> BaseModel:  # pragma: no cover
            raise AssertionError("should not call the LLM")

        derive_judged(out, [_node("a")], _cfg(), client=FakeLLM(handler))
        assert out.values == {"a": 1}


# ---------------------------------------------------------------------------
# Stage 8 — grounding + assembly
# ---------------------------------------------------------------------------


class TestGroundAndAssemble:
    def _report(self, scores: dict[str, float]) -> Any:
        def handler(_kw: dict[str, Any]) -> BaseModel:
            return GroundingReport(
                assessments=[FieldAssessment(field=k, score=v) for k, v in scores.items()]
            )

        return FakeLLM(handler)

    def test_gate_nulls_below_threshold(self) -> None:
        out = ExtractionOutput(values={"name": "Mario"}, quotes={"name": "Ristorante Mario"})
        node = _node("name")
        res = ground_and_assemble(
            [_chunk()], out, [node], _cfg(JUDGE_THRESHOLD=0.7), client=self._report({"name": 0.2})
        )
        assert res.record["name"] is None
        assert "ungrounded" in res.flags["name"]

    def test_gate_keeps_above_threshold(self) -> None:
        out = ExtractionOutput(values={"name": "Mario"}, quotes={"name": "Ristorante Mario"})
        node = _node("name")
        res = ground_and_assemble(
            [_chunk()], out, [node], _cfg(JUDGE_THRESHOLD=0.7), client=self._report({"name": 0.95})
        )
        assert res.record["name"] == "Mario"
        assert res.evidence["name"] == "Ristorante Mario"
        assert res.flags["name"]["grounding"] == 0.95

    def test_completeness_routes_to_review_queue(self) -> None:
        out = ExtractionOutput(values={"name": None})
        node = _node("name", mandatory=True)
        res = ground_and_assemble([_chunk()], out, [node], _cfg(), run_judge=False)
        assert res.review_queue == ["name"]
        assert res.confidence == 0.0  # 0/1 mandatory filled

    def test_run_judge_false_skips_llm(self) -> None:
        out = ExtractionOutput(values={"name": "Mario"}, quotes={"name": "Ristorante Mario"})

        def handler(_kw: dict[str, Any]) -> BaseModel:  # pragma: no cover
            raise AssertionError("judge should be skipped")

        res = ground_and_assemble(
            [_chunk()], out, [_node("name")], _cfg(), client=FakeLLM(handler), run_judge=False
        )
        assert res.record["name"] == "Mario"
        assert res.confidence == 1.0


# ---------------------------------------------------------------------------
# Orchestrator (public API) — end to end with a mocked client
# ---------------------------------------------------------------------------


class TestExtractAsset:
    def _e2e_client(self) -> FakeLLM:
        def handler(kw: dict[str, Any]) -> BaseModel:
            rm = kw["response_model"]
            if rm is GroundingReport:
                fields = [
                    m for m in kw["messages"][1]["content"].splitlines() if m.startswith("- ")
                ]
                names = [line[2:].split(":", 1)[0] for line in fields]
                return GroundingReport(
                    assessments=[FieldAssessment(field=n, score=0.95) for n in names]
                )
            # A group model: fill only the required 'name' field, abstain the rest.
            payload: dict[str, Any] = {}
            if "name" in rm.model_fields:
                payload["name"] = "Trattoria Da Mario"
                payload["name_quote"] = "Ristorante Trattoria Da Mario"
            return rm(**payload)

        return FakeLLM(handler)

    def test_happy_path(self) -> None:
        asset = extract_asset([_chunk()], _restaurant_contract(), client=self._e2e_client())
        assert asset.contract_id == "162_restaurant"
        assert asset.data["name"] == "Trattoria Da Mario"
        assert asset.evidence is not None and asset.evidence["name"].startswith("Ristorante")
        assert 0.0 <= asset.confidence <= 1.0

    def test_unknown_lv3_raises_contract_not_found(self) -> None:
        with pytest.raises(ContractNotFoundError):
            extract_asset([_chunk()], _restaurant_contract(lv3="nonexistent-xyz"))

    def test_missing_tree_path_raises_asset_extraction_error(self, tmp_path: Any) -> None:
        # Point ARTIFACTS_DIR at an empty tree so selection fails structurally,
        # but keep a resolvable contract file for the id lookup.
        import json

        (tmp_path / "field_tree.json").write_text(
            json.dumps({"schema_version": 1, "tree": {}}), encoding="utf-8"
        )
        cfg = _cfg(ARTIFACTS_DIR=tmp_path)
        with pytest.raises(AssetExtractionError):
            extract_asset([_chunk()], _restaurant_contract(), cfg=cfg, client=self._e2e_client())

    def test_review_queue_populated_when_mandatory_missing(self) -> None:
        # A client that abstains on everything → mandatory 'name' unfilled.
        def handler(kw: dict[str, Any]) -> BaseModel:
            rm = kw["response_model"]
            if rm is GroundingReport:
                return GroundingReport(assessments=[])
            return rm()  # all defaults (None)

        asset = extract_asset([_chunk()], _restaurant_contract(), client=FakeLLM(handler))
        assert asset.review_queue is not None
        assert "name" in asset.review_queue
