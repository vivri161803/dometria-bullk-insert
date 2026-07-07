"""Unit tests for the offline enum/description compile (Phase 6.8).

All LLM calls are mocked (``FakeLLM``); no network. Covers the compile core
(Italian description/values, topic dictionary, status flip, ``unsure``,
``not_applicable``, dedupe/``NOT_PRESENT`` stripping, ``--force``, dry-run
immutability) and the runtime topic-id resolution in ``metadata.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from bulk_insert.blocks.extraction.compile import (
    NOT_PRESENT,
    CompileBatch,
    CompiledField,
    _clean_values,
    _needs_compile,
    dump_topics_yaml,
    plan_compile,
)
from bulk_insert.blocks.extraction.metadata import FieldMetadata
from bulk_insert.blocks.extraction.tree import TreeField
from bulk_insert.config import Settings


def _cfg(**over: Any) -> Settings:
    return Settings(COMPILE_BATCH_SIZE=10, **over)


class FakeLLM:
    """Instructor stand-in whose ``create`` returns a per-batch ``CompileBatch``."""

    def __init__(self, overrides: dict[str, dict[str, Any]] | None = None) -> None:
        self.overrides = overrides or {}
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        user_msg = kwargs["messages"][-1]["content"]
        contexts = json.loads(user_msg.split("\n\n", 1)[1])
        fields = []
        for ctx in contexts:
            name = ctx["name"]
            over = self.overrides.get(name, {})
            fields.append(
                CompiledField(
                    name=over.get("name", name),
                    description=over.get("description", f"Descrizione italiana di {name}."),
                    topic=over.get("topic", "identità"),
                    applicable=over.get("applicable", True),
                    values=over.get("values", list(ctx["seeded_options"])),
                    unsure=over.get("unsure", False),
                )
            )
        # Drop any field the test asked to omit from the response.
        drop = {n for n, o in self.overrides.items() if o.get("_omit")}
        return CompileBatch(fields=[f for f in fields if f.name not in drop])


def _registry() -> dict[str, dict[str, Any]]:
    return {
        "cuisine_type": {
            "key": "cuisine_type",
            "label": "Tipo di cucina",
            "field_type": "select",
            "source": "custom_fields",
            "options": ["pizza", "pesce"],
            "products": ["162_restaurant"],
        },
        "seats_total": {
            "key": "seats_total",
            "label": "Coperti",
            "field_type": "integer",
            "source": "custom_fields",
            "options": [],
            "products": ["162_restaurant"],
        },
    }


def _sidecar() -> dict[str, dict[str, Any]]:
    return {
        "cuisine_type": {
            "description": None,
            "topic": "attributes",
            "mandatory": False,
            "priority": 3,
            "needs_evidence": True,
            "enum": {"status": "pending", "values": ["pizza", "pesce"]},
            "derivation": None,
        },
        "seats_total": {
            "description": None,
            "topic": "attributes",
            "mandatory": True,
            "priority": 5,
            "needs_evidence": True,
            "enum": {"status": "not_applicable", "values": []},
            "derivation": None,
        },
    }


class TestNeedsCompile:
    def test_pending_enum_needs_compile(self) -> None:
        assert _needs_compile(_sidecar()["cuisine_type"]) is True

    def test_null_description_needs_compile(self) -> None:
        assert _needs_compile(_sidecar()["seats_total"]) is True

    def test_compiled_field_skipped(self) -> None:
        meta = {"description": "x", "enum": {"status": "compiled", "values": ["a"]}}
        assert _needs_compile(meta) is False


class TestCleanValues:
    def test_strips_not_present_and_dedupes(self) -> None:
        # The model's refined list wins when non-empty; seeded is only a fallback.
        out = _clean_values(["Pizza", "pizza ", NOT_PRESENT, ""], seeded=["Pesce"])
        assert out == ["Pizza"]

    def test_falls_back_to_seeded_when_empty(self) -> None:
        assert _clean_values([], seeded=["a", "b"]) == ["a", "b"]


class TestPlanCompile:
    def test_italian_description_and_values_written(self) -> None:
        plan = plan_compile(_sidecar(), _registry(), {}, cfg=_cfg(), client=FakeLLM())
        cuisine = plan.fields_meta["cuisine_type"]
        assert cuisine["description"] == "Descrizione italiana di cuisine_type."
        assert cuisine["enum"]["status"] == "compiled"
        assert cuisine["enum"]["values"] == ["pizza", "pesce"]

    def test_topic_dictionary_and_id_reference(self) -> None:
        plan = plan_compile(_sidecar(), _registry(), {}, cfg=_cfg(), client=FakeLLM())
        assert plan.topics == {"identità": 1}
        assert plan.fields_meta["cuisine_type"]["topic"] == 1
        assert plan.fields_meta["seats_total"]["topic"] == 1

    def test_status_flips_to_compiled(self) -> None:
        plan = plan_compile(_sidecar(), _registry(), {}, cfg=_cfg(), client=FakeLLM())
        assert plan.enum_compile_status == "compiled"

    def test_unsure_recorded(self) -> None:
        llm = FakeLLM({"cuisine_type": {"unsure": True}})
        plan = plan_compile(_sidecar(), _registry(), {}, cfg=_cfg(), client=llm)
        assert plan.fields_meta["cuisine_type"]["unsure"] is True
        assert plan.fields_meta["seats_total"]["unsure"] is False

    def test_not_applicable_path(self) -> None:
        llm = FakeLLM({"cuisine_type": {"applicable": False}})
        plan = plan_compile(_sidecar(), _registry(), {}, cfg=_cfg(), client=llm)
        enum = plan.fields_meta["cuisine_type"]["enum"]
        assert enum["status"] == "not_applicable"
        assert enum["values"] == []

    def test_numeric_field_stays_not_applicable_but_gets_description(self) -> None:
        llm = FakeLLM({"seats_total": {"topic": "capienza", "values": ["junk"]}})
        plan = plan_compile(_sidecar(), _registry(), {}, cfg=_cfg(), client=llm)
        seats = plan.fields_meta["seats_total"]
        assert seats["enum"]["status"] == "not_applicable"
        assert seats["enum"]["values"] == []
        assert seats["description"].startswith("Descrizione") or seats["description"]
        assert seats["topic"] == plan.topics["capienza"]

    def test_not_present_stripped_from_values(self) -> None:
        llm = FakeLLM({"cuisine_type": {"values": ["Pizza", NOT_PRESENT, "pizza"]}})
        plan = plan_compile(_sidecar(), _registry(), {}, cfg=_cfg(), client=llm)
        assert plan.fields_meta["cuisine_type"]["enum"]["values"] == ["Pizza"]

    def test_only_field_limits_scope(self) -> None:
        llm = FakeLLM()
        plan = plan_compile(
            _sidecar(), _registry(), {}, cfg=_cfg(), client=llm, only_field="cuisine_type"
        )
        assert plan.compiled == ["cuisine_type"]
        assert plan.fields_meta["seats_total"]["description"] is None

    def test_force_recompiles_compiled_field(self) -> None:
        sidecar = _sidecar()
        sidecar["cuisine_type"]["description"] = "vecchia"
        sidecar["cuisine_type"]["enum"] = {"status": "compiled", "values": ["pizza"]}
        # Without force: skipped (already compiled).
        plan = plan_compile(sidecar, _registry(), {}, cfg=_cfg(), client=FakeLLM())
        assert "cuisine_type" not in plan.compiled
        # With force: recompiled.
        forced = plan_compile(sidecar, _registry(), {}, cfg=_cfg(), client=FakeLLM(), force=True)
        assert "cuisine_type" in forced.compiled

    def test_dry_run_does_not_mutate_inputs(self) -> None:
        sidecar = _sidecar()
        topics: dict[str, int] = {}
        plan_compile(sidecar, _registry(), topics, cfg=_cfg(), client=FakeLLM())
        assert sidecar["cuisine_type"]["description"] is None  # untouched
        assert topics == {}  # topics dict not mutated

    def test_reuses_existing_topic_ids(self) -> None:
        existing = {"ubicazione": 7}
        llm = FakeLLM({"cuisine_type": {"topic": "ubicazione"}, "seats_total": {"topic": "nuovo"}})
        plan = plan_compile(_sidecar(), _registry(), existing, cfg=_cfg(), client=llm)
        assert plan.topics["ubicazione"] == 7  # stable
        assert plan.topics["nuovo"] == 8  # allocated after max

    def test_missing_field_in_response_flagged_unsure(self) -> None:
        llm = FakeLLM({"cuisine_type": {"_omit": True}})
        plan = plan_compile(_sidecar(), _registry(), {}, cfg=_cfg(), client=llm)
        assert plan.fields_meta["cuisine_type"]["unsure"] is True


class TestDumpTopicsYaml:
    def test_ordered_by_id_and_roundtrips(self) -> None:
        import yaml

        text = dump_topics_yaml({"b": 2, "a": 1})
        loaded = yaml.safe_load(text)
        assert list(loaded["topics"].items()) == [("a", 1), ("b", 2)]


class TestMetadataTopicResolution:
    def _field(self) -> TreeField:
        return TreeField(
            name="cuisine_type",
            label="Tipo",
            field_type="select",
            is_required=False,
            source="custom_fields",
        )

    def test_int_id_resolves_to_name(self) -> None:
        md = FieldMetadata.from_mapping(
            {"cuisine_type": {"topic": 2, "description": "x"}},
            topics_by_id={1: "identità", 2: "menu"},
        )
        node = md.resolve(self._field(), ("a", "b", "c"))
        assert node.topic == "menu"

    def test_unmapped_id_falls_back_to_source_default(self) -> None:
        md = FieldMetadata.from_mapping(
            {"cuisine_type": {"topic": 99}},
            topics_by_id={1: "identità"},
        )
        node = md.resolve(self._field(), ("a", "b", "c"))
        assert node.topic == "attributes"  # custom_fields fallback

    def test_string_topic_used_verbatim(self) -> None:
        md = FieldMetadata.from_mapping({"cuisine_type": {"topic": "menu"}})
        node = md.resolve(self._field(), ("a", "b", "c"))
        assert node.topic == "menu"
