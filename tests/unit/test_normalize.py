"""Unit tests for Stage 4b deterministic normalization (Phase 6.10 Part A)."""

from __future__ import annotations

from typing import Any

from bulk_insert.blocks.extraction.derive import derive_computed
from bulk_insert.blocks.extraction.extract import ExtractionOutput
from bulk_insert.blocks.extraction.normalize import (
    _norm_date,
    _norm_integer,
    _norm_number,
    _norm_percent,
    _norm_text,
    _norm_year,
    normalize_fields,
)
from bulk_insert.blocks.extraction.tree import Derivation, DerivationKind, FieldNode
from bulk_insert.config import Settings


def _cfg(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _node(name: str, field_type: str = "text") -> FieldNode:
    return FieldNode(
        name=name,
        lv_path=("a", "b", "c"),
        field_type=field_type,
        label=name,
        source="custom_fields",
        constraints=None,
        description=None,
        topic="attributes",
        mandatory=False,
        priority=3,
    )


# ---------------------------------------------------------------------------
# Pure normalizers
# ---------------------------------------------------------------------------


class TestNumber:
    def test_italian_currency_string(self) -> None:
        assert _norm_number("1.200,50 €") == (1200.5, True)

    def test_unit_suffix_string(self) -> None:
        assert _norm_number("1.500 mq") == (1500.0, True)

    def test_already_float_passes_through(self) -> None:
        assert _norm_number(1200.5) == (1200.5, True)

    def test_already_int_becomes_float(self) -> None:
        assert _norm_number(3) == (3.0, True)

    def test_unparseable_keeps_raw(self) -> None:
        assert _norm_number("abc") == ("abc", False)

    def test_bool_rejected(self) -> None:
        assert _norm_number(True) == (True, False)


class TestPercent:
    def test_written_percentage_not_fraction(self) -> None:
        assert _norm_percent("3,5 %") == (3.5, True)

    def test_plain_number(self) -> None:
        assert _norm_percent(3.5) == (3.5, True)


class TestInteger:
    def test_thousands_string(self) -> None:
        assert _norm_integer("1.200") == (1200, True)

    def test_non_integral_keeps_raw(self) -> None:
        assert _norm_integer("1.200,5") == ("1.200,5", False)

    def test_already_int(self) -> None:
        assert _norm_integer(42) == (42, True)


class TestYear:
    def test_month_and_year(self) -> None:
        assert _norm_year("gennaio 2019") == (2019, True)

    def test_bare_year_string(self) -> None:
        assert _norm_year("2019") == (2019, True)

    def test_two_digit_keeps_raw(self) -> None:
        assert _norm_year("19") == ("19", False)

    def test_int_in_range(self) -> None:
        assert _norm_year(2005) == (2005, True)

    def test_int_out_of_range_keeps_raw(self) -> None:
        assert _norm_year(93) == (93, False)


class TestDate:
    def test_month_slash_year(self) -> None:
        assert _norm_date("01/2020") == ("2020-01-01", True)

    def test_italian_month_name(self) -> None:
        assert _norm_date("gennaio 2020") == ("2020-01-01", True)

    def test_bare_year(self) -> None:
        assert _norm_date("2019") == ("2019-01-01", True)

    def test_full_dmy(self) -> None:
        assert _norm_date("12/03/2021") == ("2021-03-12", True)

    def test_already_iso(self) -> None:
        assert _norm_date("2021-03-12") == ("2021-03-12", True)

    def test_junk_keeps_raw(self) -> None:
        assert _norm_date("not a date") == ("not a date", False)


class TestText:
    def test_whitespace_collapse(self) -> None:
        assert _norm_text("  a   b \n c ") == ("a b c", True)

    def test_no_case_change(self) -> None:
        assert _norm_text("Villa AL Mare") == ("Villa AL Mare", True)


# ---------------------------------------------------------------------------
# normalize_fields orchestration
# ---------------------------------------------------------------------------


class TestNormalizeFields:
    def test_rewrites_and_flags_provenance(self) -> None:
        out = ExtractionOutput(values={"anno": "gennaio 2019"})
        normalize_fields(out, [_node("anno", "year")])
        assert out.values["anno"] == 2019
        assert out.flags["anno"]["normalization"] == {
            "status": "ok",
            "raw": "gennaio 2019",
            "value": 2019,
        }

    def test_no_flag_when_already_canonical(self) -> None:
        out = ExtractionOutput(values={"prezzo": 1200.0})
        normalize_fields(out, [_node("prezzo", "currency")])
        assert out.values["prezzo"] == 1200.0
        assert "prezzo" not in out.flags

    def test_unparseable_kept_and_flagged(self) -> None:
        out = ExtractionOutput(values={"prezzo": "abc"})
        normalize_fields(out, [_node("prezzo", "currency")])
        assert out.values["prezzo"] == "abc"
        assert out.flags["prezzo"]["normalization"]["status"] == "unparsed"

    def test_enum_and_boolean_untouched(self) -> None:
        out = ExtractionOutput(values={"zona": "centro", "attivo": "si"})
        normalize_fields(out, [_node("zona", "select"), _node("attivo", "boolean")])
        assert out.values == {"zona": "centro", "attivo": "si"}
        assert not out.flags

    def test_none_value_untouched(self) -> None:
        out = ExtractionOutput(values={"prezzo": None})
        normalize_fields(out, [_node("prezzo", "currency")])
        assert out.values["prezzo"] is None
        assert not out.flags

    def test_disabled_short_circuits(self) -> None:
        out = ExtractionOutput(values={"anno": "gennaio 2019"})
        normalize_fields(out, [_node("anno", "year")], _cfg(NORMALIZE_ENABLED=False))
        assert out.values["anno"] == "gennaio 2019"
        assert not out.flags

    def test_preserves_existing_flags(self) -> None:
        out = ExtractionOutput(
            values={"anno": "2019"},
            flags={"anno": {"vote": "2/3", "agreement": "majority"}},
        )
        normalize_fields(out, [_node("anno", "year")])
        assert out.flags["anno"]["agreement"] == "majority"
        assert out.flags["anno"]["normalization"]["status"] == "ok"

    def test_placement_computed_sees_normalized_value(self) -> None:
        """Stage 4b runs before Stage 5: a sum sees canonical floats, not strings."""
        out = ExtractionOutput(values={"a": "1.200,50 €", "b": "300 €"})
        source = [_node("a", "currency"), _node("b", "currency")]
        derived = FieldNode(
            name="totale",
            lv_path=("a", "b", "c"),
            field_type="currency",
            label="totale",
            source="custom_fields",
            constraints=None,
            description=None,
            topic="prezzo",
            mandatory=False,
            priority=3,
            derivation=Derivation(("a", "b"), "sum", DerivationKind.COMPUTED),
        )
        normalize_fields(out, [*source, derived])
        derive_computed(out, [*source, derived])
        assert out.values["totale"] == 1500.5
