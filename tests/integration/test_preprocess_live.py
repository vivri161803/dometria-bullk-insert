"""Integration test for the live preprocessing pipeline.

These tests make real LLM calls and are gated behind the ``integration`` mark.

Run with:
    uv run pytest tests/integration -m integration

Requires:
    - ``OPENROUTER_API_KEY`` set in environment or `.env` file.
"""

import pytest
from bulk_insert.blocks.preprocessing import preprocess
from bulk_insert.config import Settings

pytestmark = pytest.mark.integration

_SAMPLE_TEXT = """
The lease agreement was entered into on the first day of January 2024.
John Smith, referred to herein as the Tenant, agrees to pay monthly rent of $1,500.
He must transfer the payment by the fifth of each month.
The Landlord, Acme Properties LLC, will provide maintenance services within 48 hours.
They are also responsible for ensuring the heating system is operational.
The property is located at 123 Main Street, Springfield.
It has three bedrooms and two bathrooms.
The security deposit of $3,000 must be paid prior to move-in.
This deposit will be returned within 30 days of lease termination.
Both parties agree to a 12-month term, beginning January 1, 2024.
"""


@pytest.mark.integration
def test_preprocess_live_returns_scored_and_resolved_chunks() -> None:
    """Real LLM call: verify all chunks are scored and coreference-resolved."""
    settings = Settings()
    if not settings.OPENROUTER_API_KEY:
        pytest.skip("OPENROUTER_API_KEY not set — skipping live integration test.")

    chunks = preprocess(_SAMPLE_TEXT.strip(), settings)

    assert len(chunks) > 0, "Expected at least one chunk."
    for chunk in chunks:
        assert chunk.relevance_score is not None, f"Chunk {chunk.index} not scored."
        assert (
            1 <= chunk.relevance_score <= 10
        ), f"Score {chunk.relevance_score} out of range for chunk {chunk.index}."
        assert chunk.text.strip(), f"Chunk {chunk.index} has empty text."
        assert chunk.is_coref_resolved is True, f"Chunk {chunk.index} not coref-resolved."
