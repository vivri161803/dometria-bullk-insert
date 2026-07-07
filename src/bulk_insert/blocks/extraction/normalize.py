"""Deterministic value normalization (Block 3 v2, Stage 4b, Phase 6.10 Part A).

A **pure-Python** pass between extraction (Stage 4) and derivation (Stage 5) that
canonicalizes raw extracted values.  Unlike cross-field derivation, this is
strictly single-field: it *cleans* an already-extracted value in place and never
fabricates one from siblings, so it cannot lower accuracy — the worst case is a
value it cannot parse, which it leaves untouched and flags.

No LLM, no I/O, no clock — deterministic and reproducible.

**Why the numeric normalizers are mostly a safety net.**  The generated
extraction model (:mod:`bulk_insert.blocks.extraction.schema`) already types
``integer``/``year`` as ``int`` and ``decimal``/``currency``/``percentage`` as
``float``, so Instructor/Pydantic coerces those to real numerics before they ever
reach here — the numeric normalizers see canonical values and pass them through.
They stay in the registry to defend the ``str``-fallback / stray-string edge
cases.  The genuine work is ``date`` (a free-text ``str`` → ISO) and ``text``
(whitespace tidy); enums/booleans are already canonical and skipped.

Normalizer selection is **type-driven** — a registry keyed by ``field_type``.
There is no LLM-compiled per-field override and no unit (mq/ha) resolution in
this cut; ambiguous-unit fields pass through untouched.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from bulk_insert.blocks.extraction.extract import ExtractionOutput
from bulk_insert.blocks.extraction.tree import FieldNode
from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings

logger = logging.getLogger(__name__)

__all__: list[str] = [
    "NORMALIZERS",
    "normalize_fields",
]

# A normalizer is a pure function of the raw extracted value, returning
# ``(canonical_value, ok)``.  ``ok=False`` means "could not parse" → the caller
# keeps the raw value and flags it.
Normalizer = Callable[[Any], "tuple[Any, bool]"]


# ---------------------------------------------------------------------------
# Numeric parsing (Italian locale: '.' thousands, ',' decimal)
# ---------------------------------------------------------------------------

# Keep only digits and the two separators / sign; drop currency symbols, units,
# spaces, '%', etc. before interpreting the Italian number grammar.
_NUMERIC_KEEP = re.compile(r"[^0-9.,-]")


def _parse_number(raw: Any) -> tuple[float, bool]:
    """Parse an already-numeric value or an Italian-formatted numeric string."""
    if isinstance(raw, bool):  # bool is an int subclass — never a numeric field
        return (0.0, False)
    if isinstance(raw, int | float):
        return (float(raw), True)
    if not isinstance(raw, str):
        return (0.0, False)
    cleaned = _NUMERIC_KEEP.sub("", raw).strip()
    if not cleaned:
        return (0.0, False)
    # Italian grammar: '.' groups thousands, ',' is the decimal separator.
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return (float(cleaned), True)
    except ValueError:
        return (0.0, False)


def _norm_number(raw: Any) -> tuple[Any, bool]:
    value, ok = _parse_number(raw)
    return (value, True) if ok else (raw, False)


def _norm_percent(raw: Any) -> tuple[Any, bool]:
    """Percentage as written (``"3,5 %"`` → ``3.5``), not a 0–1 fraction."""
    if isinstance(raw, str):
        raw = raw.rstrip().rstrip("%")
    value, ok = _parse_number(raw)
    return (value, True) if ok else (raw, False)


def _norm_integer(raw: Any) -> tuple[Any, bool]:
    value, ok = _parse_number(raw)
    if not ok or not float(value).is_integer():
        return (raw, False)  # keep-raw on non-integral (e.g. "1.200,5")
    return (int(value), True)


# ---------------------------------------------------------------------------
# Temporal parsing (deterministic, no clock, no dateutil)
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r"(?<!\d)((?:18|19|20|21)\d{2})(?!\d)")

_ITALIAN_MONTHS: dict[str, int] = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
}


def _norm_year(raw: Any) -> tuple[Any, bool]:
    """Extract a 4-digit year in ``[1800, 2100]``."""
    if isinstance(raw, bool):
        return (raw, False)
    if isinstance(raw, int):
        return (raw, True) if 1800 <= raw <= 2100 else (raw, False)
    if not isinstance(raw, str):
        return (raw, False)
    match = _YEAR_RE.search(raw)
    if match is None:
        return (raw, False)
    year = int(match.group(1))
    return (year, True) if 1800 <= year <= 2100 else (raw, False)


_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DMY_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")
_MY_RE = re.compile(r"^(\d{1,2})[/-](\d{4})$")


def _iso(year: int, month: int, day: int) -> tuple[Any, bool]:
    if not (1 <= month <= 12 and 1 <= day <= 31 and 1800 <= year <= 2100):
        return (None, False)
    return (f"{year:04d}-{month:02d}-{day:02d}", True)


def _norm_date(raw: Any) -> tuple[Any, bool]:
    """Parse common Italian date forms to ISO ``YYYY-MM-DD`` (bare year → -01-01)."""
    if not isinstance(raw, str):
        return (raw, False)
    text = raw.strip()

    if _ISO_DATE_RE.match(text):  # already ISO — accept as-is
        return (text, True)

    dmy = _DMY_RE.match(text)
    if dmy:
        day, month, year = (int(dmy.group(i)) for i in (1, 2, 3))
        value, ok = _iso(year, month, day)
        return (value, True) if ok else (raw, False)

    my = _MY_RE.match(text)
    if my:
        month, year = int(my.group(1)), int(my.group(2))
        value, ok = _iso(year, month, 1)
        return (value, True) if ok else (raw, False)

    lowered = text.casefold()
    for name, month in _ITALIAN_MONTHS.items():
        if name in lowered:
            year_match = _YEAR_RE.search(lowered)
            if year_match:
                value, ok = _iso(int(year_match.group(1)), month, 1)
                return (value, True) if ok else (raw, False)

    bare_year = _YEAR_RE.search(text)
    if bare_year:
        value, ok = _iso(int(bare_year.group(1)), 1, 1)
        return (value, True) if ok else (raw, False)

    return (raw, False)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def _norm_text(raw: Any) -> tuple[Any, bool]:
    """Trim + collapse internal whitespace only (no case/content changes)."""
    if not isinstance(raw, str):
        return (raw, False)
    return (_WHITESPACE_RE.sub(" ", raw).strip(), True)


# Type-driven registry.  Types absent here (``boolean``/``select``/unknown) and
# ``None`` values are no-ops — enums are already canonical (compiled vocab).
NORMALIZERS: dict[str, Normalizer] = {
    "currency": _norm_number,
    "decimal": _norm_number,
    "percentage": _norm_percent,
    "integer": _norm_integer,
    "year": _norm_year,
    "date": _norm_date,
    "text": _norm_text,
}


def normalize_fields(
    result: ExtractionOutput,
    nodes: list[FieldNode],
    cfg: Settings | None = None,
) -> None:
    """Canonicalize extracted values in place (Stage 4b). Deterministic, no LLM/IO.

    Mirrors :func:`derive.derive_computed`'s shape: mutates ``result.values`` in
    place and records provenance under ``result.flags[name]["normalization"]``.
    A successful no-op (value already canonical) records nothing, keeping flags
    sparse.  Short-circuits entirely when ``cfg.NORMALIZE_ENABLED`` is ``False``.
    """
    effective_cfg = cfg or _default_settings
    if not effective_cfg.NORMALIZE_ENABLED:
        return

    by_name = {n.name: n for n in nodes}
    for name in list(result.values):
        raw = result.values[name]
        if raw is None:  # abstention is not a normalization failure
            continue
        node = by_name.get(name)
        if node is None:
            continue
        normalizer = NORMALIZERS.get(node.field_type)
        if normalizer is None:  # enums / booleans / unknown → already canonical
            continue

        value, ok = normalizer(raw)
        if not ok:
            _flag(result, name, {"status": "unparsed", "field_type": node.field_type, "raw": raw})
            logger.warning(
                "Could not normalize '%s' (%s): kept raw value %r.", name, node.field_type, raw
            )
            continue
        if value != raw:
            result.values[name] = value
            _flag(result, name, {"status": "ok", "raw": raw, "value": value})


def _flag(result: ExtractionOutput, name: str, detail: dict[str, Any]) -> None:
    """Attach normalization provenance without clobbering prior flags."""
    existing = result.flags.get(name)
    flag = dict(existing) if isinstance(existing, dict) else {}
    flag["normalization"] = detail
    result.flags[name] = flag
