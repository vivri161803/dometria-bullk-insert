"""Phase 9 — every Instructor client inherits SDK-level backoff/timeout.

Passing ``max_retries``/``timeout`` into ``instructor.from_provider`` configures
the underlying OpenAI client, which retries 429 / transient upstream errors with
exponential backoff honouring OpenRouter's ``Retry-After``. These tests assert
all three block builders forward the settings.
"""

from __future__ import annotations

from bulk_insert.blocks.contract import _build_instructor_client as contract_client
from bulk_insert.blocks.extraction.client import build_instructor_client as extraction_client
from bulk_insert.blocks.preprocessing import _build_instructor_client as preprocessing_client
from bulk_insert.config import Settings


def _cfg() -> Settings:
    return Settings(OPENROUTER_API_KEY="k", LLM_MAX_RETRIES=7, LLM_TIMEOUT=42.0)


def test_extraction_client_has_backoff() -> None:
    client = extraction_client(_cfg())
    assert client.client.max_retries == 7
    assert client.client.timeout == 42.0


def test_preprocessing_client_has_backoff() -> None:
    client = preprocessing_client(_cfg())
    assert client.client.max_retries == 7
    assert client.client.timeout == 42.0


def test_contract_client_has_backoff() -> None:
    client = contract_client(_cfg())
    assert client.client.max_retries == 7
    assert client.client.timeout == 42.0
