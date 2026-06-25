"""Unit tests for Block 3 — Asset Extraction."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import get_args
from unittest.mock import MagicMock, patch

import pytest
from bulk_insert.blocks.extraction import build_dynamic_model, extract_asset
from bulk_insert.models.chunk import Chunk
from bulk_insert.models.contract import Contract
from bulk_insert.models.contract_loader import ContractNotFoundError, load_contract_schema
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _restaurant_schema() -> dict[str, object]:
    """Load the real restaurant contract schema shipped with the repo."""
    return load_contract_schema("restaurant")


def _make_chunk(index: int = 0, text: str = "Ristorante in centro.") -> Chunk:
    return Chunk(
        index=index,
        text=text,
        start_sentence=0,
        end_sentence=0,
        overlap_previous=0,
        is_coref_resolved=True,
        relevance_score=8,
    )


def _non_none_arg(annotation: object) -> object:
    """Return the non-None member of an ``Optional[T]`` annotation."""
    args = [a for a in get_args(annotation) if a is not type(None)]
    return args[0] if len(args) == 1 else annotation


class _FakeInner(BaseModel):
    """Stand-in for the dynamically built asset model in orchestrator tests."""

    name: str | None = None


def _fake_result(confidence: float, name: str | None = "Trattoria da Mario") -> SimpleNamespace:
    return SimpleNamespace(asset=_FakeInner(name=name), confidence=confidence)


def _restaurant_contract(lv3: str = "restaurant") -> Contract:
    return Contract(lv1="commercial-activities", lv2="food-beverage", lv3=lv3)


# ---------------------------------------------------------------------------
# build_dynamic_model
# ---------------------------------------------------------------------------


class TestBuildDynamicModel:
    """Tests for build_dynamic_model()."""

    def test_field_types(self) -> None:
        model = build_dynamic_model(_restaurant_schema())
        fields = model.model_fields
        assert _non_none_arg(fields["name"].annotation) is str
        assert _non_none_arg(fields["restaurant_numero_coperti_interni"].annotation) is int
        assert _non_none_arg(fields["restaurant_scontrino_medio_eur"].annotation) is float

    def test_boolean_is_literal(self) -> None:
        model = build_dynamic_model(_restaurant_schema())
        annotation = _non_none_arg(model.model_fields["restaurant_canna_fumaria"].annotation)
        assert get_args(annotation) == ("si", "no", "non dichiarato")

    def test_select_is_literal(self) -> None:
        model = build_dynamic_model(_restaurant_schema())
        annotation = _non_none_arg(model.model_fields["commercial_activities_zona"].annotation)
        assert get_args(annotation) == ("centro", "semicentro", "periferia", "CC")

    def test_all_fields_optional(self) -> None:
        model = build_dynamic_model(_restaurant_schema())
        instance = model()  # no args — must not raise
        for value in instance.model_dump().values():
            assert value is None

    def test_excludes_non_extractable_fields(self) -> None:
        model = build_dynamic_model(_restaurant_schema())
        fields = model.model_fields
        for excluded in (
            "geo_municipality_id",
            "municipality_id",
            "latitude",
            "longitude",
            "country",
        ):
            assert excluded not in fields
        # Plain-text native_location fields are kept.
        assert "locality" in fields
        assert "address_line" in fields

    def test_preserves_required_metadata(self) -> None:
        model = build_dynamic_model(_restaurant_schema())
        extra = model.model_fields["name"].json_schema_extra
        assert extra == {"required_by_contract": True}
        # A non-required field carries the flag too.
        extra_optional = model.model_fields["locality"].json_schema_extra
        assert extra_optional == {"required_by_contract": False}


# ---------------------------------------------------------------------------
# extract_asset
# ---------------------------------------------------------------------------


class TestExtractAsset:
    """Tests for the extract_asset() orchestrator (LLM mocked)."""

    def _patch_client(self, *results: SimpleNamespace) -> MagicMock:
        client = MagicMock()
        client.chat.completions.create.side_effect = list(results)
        return client

    def test_happy_path(self) -> None:
        client = self._patch_client(_fake_result(0.9))
        with patch("bulk_insert.blocks.extraction._build_instructor_client", return_value=client):
            asset = extract_asset([_make_chunk()], _restaurant_contract())
        assert client.chat.completions.create.call_count == 1
        assert asset.contract_id == "162_restaurant"
        assert asset.confidence == 0.9
        assert asset.data["name"] == "Trattoria da Mario"

    def test_retries_until_threshold(self) -> None:
        client = self._patch_client(_fake_result(0.4), _fake_result(0.7))
        with patch("bulk_insert.blocks.extraction._build_instructor_client", return_value=client):
            asset = extract_asset([_make_chunk()], _restaurant_contract())
        assert client.chat.completions.create.call_count == 2
        assert asset.confidence == 0.7

    def test_exhausts_retries_returns_best(self, caplog: pytest.LogCaptureFixture) -> None:
        client = self._patch_client(_fake_result(0.3), _fake_result(0.5), _fake_result(0.4))
        with patch("bulk_insert.blocks.extraction._build_instructor_client", return_value=client):
            with caplog.at_level(logging.WARNING):
                asset = extract_asset([_make_chunk()], _restaurant_contract())
        assert client.chat.completions.create.call_count == 3
        assert asset.confidence == 0.5  # best of the three attempts
        assert "below threshold" in caplog.text

    def test_short_circuits_on_first_pass(self) -> None:
        # Confidence exactly at threshold should stop after one attempt.
        client = self._patch_client(_fake_result(0.6))
        with patch("bulk_insert.blocks.extraction._build_instructor_client", return_value=client):
            asset = extract_asset([_make_chunk()], _restaurant_contract())
        assert client.chat.completions.create.call_count == 1
        assert asset.confidence == 0.6

    def test_unknown_lv3_raises(self) -> None:
        with pytest.raises(ContractNotFoundError):
            extract_asset([_make_chunk()], _restaurant_contract(lv3="nonexistent-xyz"))

    def test_data_retains_none(self) -> None:
        client = self._patch_client(_fake_result(0.9, name=None))
        with patch("bulk_insert.blocks.extraction._build_instructor_client", return_value=client):
            asset = extract_asset([_make_chunk()], _restaurant_contract())
        assert "name" in asset.data
        assert asset.data["name"] is None
