"""Unit tests for Stage 4c OUT_OF_VOCAB resolution (Phase 6.10 Part B.1)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, get_args

import pytest

from bulk_insert.blocks.extraction.extract import ExtractionOutput
from bulk_insert.blocks.extraction.ground import ground_and_assemble
from bulk_insert.blocks.extraction.resolve import OOVResolution, resolve_out_of_vocab
from bulk_insert.blocks.extraction.schema import (
    NOT_PRESENT,
    OUT_OF_VOCAB,
    build_extraction_model,
)
from bulk_insert.blocks.extraction.tree import FieldNode
from bulk_insert.config import Settings
from bulk_insert.models.chunk import Chunk


def _cfg(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _node(name: str, *, enum: list[str] | None = None, mandatory: bool = False) -> FieldNode:
    return FieldNode(
        name=name,
        lv_path=("a", "b", "c"),
        field_type="select" if enum else "text",
        label=name,
        source="custom_fields",
        constraints=None,
        description=f"desc for {name}",
        topic="attributes",
        mandatory=mandatory,
        priority=5 if mandatory else 3,
        enum=enum,
    )


class FakeLLM:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._handler(kwargs)


def _chunk(text: str = "Riscaldamento: Autonomo (Gas naturale). Villa Da Mario.") -> Chunk:
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
# Schema: the sentinel
# ---------------------------------------------------------------------------


class TestSentinelInSchema:
    def test_both_sentinels_in_compiled_enum(self) -> None:
        model = build_extraction_model(
            [_node("risc", enum=["aria", "radiatori"])], cfg=_cfg(OOV_ENABLED=True)
        )
        literal = get_args(model.model_fields["risc"].annotation)[0]
        members = get_args(literal)
        assert NOT_PRESENT in members
        assert OUT_OF_VOCAB in members

    def test_sentinel_absent_when_disabled(self) -> None:
        model = build_extraction_model(
            [_node("risc", enum=["aria", "radiatori"])], cfg=_cfg(OOV_ENABLED=False)
        )
        literal = get_args(model.model_fields["risc"].annotation)[0]
        members = get_args(literal)
        assert NOT_PRESENT in members
        assert OUT_OF_VOCAB not in members

    def test_out_of_vocab_requires_a_quote(self) -> None:
        model = build_extraction_model([_node("risc", enum=["aria"])], cfg=_cfg(OOV_ENABLED=True))
        with pytest.raises(ValueError, match="verbatim source quote"):
            model(risc=OUT_OF_VOCAB)  # no quote → validator rejects (like a real value)
        inst = model(risc=OUT_OF_VOCAB, risc_quote="Autonomo a gas")
        assert inst.risc == OUT_OF_VOCAB


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class TestResolver:
    def test_prompt_excludes_raw_document(self) -> None:
        out = ExtractionOutput(
            values={"risc": OUT_OF_VOCAB},
            quotes={"risc": "Riscaldamento: Autonomo (Gas naturale)"},
        )

        def handler(kw: dict[str, Any]) -> OOVResolution:
            content = kw["messages"][1]["content"]
            assert "Villa Da Mario" not in content  # raw doc never present
            assert "Autonomo (Gas naturale)" in content  # only the quote
            assert "desc for risc" in content
            return OOVResolution(value="Autonomo a gas", in_vocab=False)

        resolve_out_of_vocab(out, [_node("risc", enum=["aria"])], _cfg(), client=FakeLLM(handler))
        assert out.values["risc"] == "Autonomo a gas"
        assert out.flags["risc"]["resolution"] == {
            "out_of_vocab": True,
            "inferred": "Autonomo a gas",
        }

    def test_remap_to_listed_value(self) -> None:
        out = ExtractionOutput(values={"risc": OUT_OF_VOCAB}, quotes={"risc": "termosifoni"})

        def handler(_kw: dict[str, Any]) -> OOVResolution:
            return OOVResolution(value="radiatori", in_vocab=True)

        resolve_out_of_vocab(
            out, [_node("risc", enum=["aria", "radiatori"])], _cfg(), client=FakeLLM(handler)
        )
        assert out.values["risc"] == "radiatori"

    def test_disabled_normalizes_token_to_none(self) -> None:
        out = ExtractionOutput(values={"risc": OUT_OF_VOCAB}, quotes={"risc": "x"})

        def handler(_kw: dict[str, Any]) -> OOVResolution:  # pragma: no cover
            raise AssertionError("must not call the judge when disabled")

        resolve_out_of_vocab(
            out, [_node("risc", enum=["aria"])], _cfg(OOV_ENABLED=False), client=FakeLLM(handler)
        )
        assert out.values["risc"] is None

    def test_no_targets_is_noop(self) -> None:
        out = ExtractionOutput(values={"risc": "aria"}, quotes={"risc": "aria condizionata"})

        def handler(_kw: dict[str, Any]) -> OOVResolution:  # pragma: no cover
            raise AssertionError("no OOV fields → no call")

        resolve_out_of_vocab(out, [_node("risc", enum=["aria"])], _cfg(), client=FakeLLM(handler))
        assert out.values["risc"] == "aria"

    def test_resolved_value_survives_grounding(self) -> None:
        node = _node("risc", enum=["aria"])
        out = ExtractionOutput(
            values={"risc": OUT_OF_VOCAB}, quotes={"risc": "Autonomo (Gas naturale)"}
        )
        resolve_out_of_vocab(
            out,
            [node],
            _cfg(),
            client=FakeLLM(lambda _k: OOVResolution(value="autonomo a gas", in_vocab=False)),
        )

        def ground_handler(_kw: dict[str, Any]) -> Any:
            from bulk_insert.blocks.extraction.ground import FieldAssessment, GroundingReport

            return GroundingReport(assessments=[FieldAssessment(field="risc", passed=True)])

        res = ground_and_assemble([_chunk()], out, [node], _cfg(), client=FakeLLM(ground_handler))
        assert res.record["risc"] == "autonomo a gas"
        assert res.evidence["risc"] == "Autonomo (Gas naturale)"
