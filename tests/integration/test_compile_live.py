"""Integration smoke test for the offline metadata compile (Phase 6.8).

Makes a real LLM call and is gated behind the ``integration`` mark.

Run with:
    uv run pytest tests/integration -m integration

Requires:
    - ``OPENROUTER_API_KEY`` set in environment or `.env` file.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from bulk_insert.blocks.extraction.compile import plan_compile
from bulk_insert.config import Settings

pytestmark = pytest.mark.integration

# One enum-candidate field with seeded Italian-ish options; the compile must return
# an Italian description and a blessed value list.
_REGISTRY: dict[str, dict[str, Any]] = {
    "cuisine_type": {
        "key": "cuisine_type",
        "label": "Tipo di cucina",
        "field_type": "select",
        "source": "custom_fields",
        "options": ["pizzeria", "pesce", "carne"],
        "products": ["162_restaurant"],
    }
}
_SIDECAR: dict[str, dict[str, Any]] = {
    "cuisine_type": {
        "description": None,
        "topic": "attributes",
        "mandatory": False,
        "priority": 3,
        "needs_evidence": True,
        "enum": {"status": "pending", "values": ["pizzeria", "pesce", "carne"]},
        "derivation": None,
    }
}


@pytest.mark.integration
def test_compile_single_field_live_returns_italian() -> None:
    """Real LLM call: one field compiles to a non-empty Italian description."""
    plan = plan_compile(_SIDECAR, _REGISTRY, {}, cfg=Settings())

    field = plan.fields_meta["cuisine_type"]
    assert field["description"] and isinstance(field["description"], str)
    # A crude Italian smoke check: some Latin letters, no obvious English stopwords.
    assert re.search(r"[a-zA-Zàèéìòù]", field["description"])
    assert not re.search(r"\b(the|and|of|is|field)\b", field["description"].lower())
    # Topic dictionary was built and the field references it by id.
    assert isinstance(field["topic"], int)
    assert field["topic"] in plan.topics.values()
