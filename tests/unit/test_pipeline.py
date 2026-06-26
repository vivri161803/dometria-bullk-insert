"""Unit tests for the pipeline orchestrator (Phase 5).

All three blocks are mocked — no live LLM calls.  Covers the happy path and
every error branch (V1–V9 of the validation spec), asserting the structured
outcome, that ``id`` is always echoed, and that ``process_document`` never raises.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from openai import APIConnectionError, RateLimitError

from bulk_insert.config import Settings
from bulk_insert.models.asset import Asset
from bulk_insert.models.chunk import Chunk
from bulk_insert.models.contract import Contract
from bulk_insert.pipeline import process_document

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

DOC_ID = "doc-42"
TEXT = "Ristorante in centro con 40 coperti."


def _chunk() -> Chunk:
    return Chunk(
        index=0,
        text="Ristorante in centro.",
        start_sentence=0,
        end_sentence=0,
        overlap_previous=0,
        is_coref_resolved=True,
        relevance_score=8,
    )


def _contract() -> Contract:
    return Contract(lv1="commercial-activities", lv2="food-beverage", lv3="restaurant")


def _asset(confidence: float = 0.92) -> Asset:
    return Asset(contract_id="162_restaurant", data={"name": "Da Mario"}, confidence=confidence)


def _patch_blocks(
    *,
    preprocess=None,
    determine_contract=None,
    extract_asset=None,
):
    """Patch the three block entry points as imported into ``pipeline``."""
    return (
        patch("bulk_insert.pipeline.preprocess", preprocess or MagicMock(return_value=[_chunk()])),
        patch(
            "bulk_insert.pipeline.determine_contract",
            determine_contract or MagicMock(return_value=_contract()),
        ),
        patch(
            "bulk_insert.pipeline.extract_asset",
            extract_asset or MagicMock(return_value=_asset()),
        ),
    )


def _make_api_connection_error() -> APIConnectionError:
    return APIConnectionError(request=httpx.Request("POST", "https://openrouter.ai"))


def _make_rate_limit_error() -> RateLimitError:
    response = httpx.Response(429, request=httpx.Request("POST", "https://openrouter.ai"))
    return RateLimitError("rate limited", response=response, body=None)


# ---------------------------------------------------------------------------
# V1 — happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_asset_and_echoes_id() -> None:
    p1, p2, p3 = _patch_blocks()
    with p1 as m_pre, p2 as m_contract, p3 as m_extract:
        result = process_document(DOC_ID, TEXT)

    assert result.ok is True
    assert result.id == DOC_ID
    assert result.asset == _asset()
    assert result.confidence == pytest.approx(0.92)
    assert result.error is None
    # Blocks were chained in order with the chunks / contract threaded through.
    m_pre.assert_called_once()
    m_contract.assert_called_once()
    m_extract.assert_called_once()


def test_cfg_is_forwarded_to_every_block() -> None:
    sentinel = Settings()
    p1, p2, p3 = _patch_blocks()
    with p1 as m_pre, p2 as m_contract, p3 as m_extract:
        process_document(DOC_ID, TEXT, cfg=sentinel)

    assert m_pre.call_args.kwargs["cfg"] is sentinel
    assert m_contract.call_args.kwargs["cfg"] is sentinel
    assert m_extract.call_args.kwargs["cfg"] is sentinel


# ---------------------------------------------------------------------------
# V2 — blank input guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\t  \n"])
def test_blank_input_is_bad_input_and_skips_blocks(text: str) -> None:
    p1, p2, p3 = _patch_blocks()
    with p1 as m_pre, p2 as m_contract, p3 as m_extract:
        result = process_document(DOC_ID, text)

    assert result.ok is False
    assert result.id == DOC_ID
    assert result.error is not None
    assert result.error.stage == "input"
    assert result.error.category == "bad_input"
    m_pre.assert_not_called()
    m_contract.assert_not_called()
    m_extract.assert_not_called()


# ---------------------------------------------------------------------------
# V3 — unprocessable (no qualifying chunk)
# ---------------------------------------------------------------------------


def test_contract_determination_error_is_unprocessable() -> None:
    from bulk_insert.blocks.contract import ContractDeterminationError

    failing = MagicMock(side_effect=ContractDeterminationError("no qualifying chunk"))
    p1, p2, p3 = _patch_blocks(determine_contract=failing)
    with p1, p2, p3 as m_extract:
        result = process_document(DOC_ID, TEXT)

    assert result.ok is False
    assert result.error is not None
    assert result.error.stage == "contract"
    assert result.error.category == "unprocessable"
    m_extract.assert_not_called()


# ---------------------------------------------------------------------------
# V4 / V5 — extract-stage server errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc_name", ["ContractNotFoundError", "AssetExtractionError"])
def test_extract_stage_structural_errors_are_server(exc_name: str) -> None:
    from bulk_insert.blocks.extraction import AssetExtractionError
    from bulk_insert.models.contract_loader import ContractNotFoundError

    exc_type = {
        "ContractNotFoundError": ContractNotFoundError,
        "AssetExtractionError": AssetExtractionError,
    }[exc_name]
    exc = exc_type("boom")
    p1, p2, p3 = _patch_blocks(extract_asset=MagicMock(side_effect=exc))
    with p1, p2, p3:
        result = process_document(DOC_ID, TEXT)

    assert result.ok is False
    assert result.error is not None
    assert result.error.stage == "extract"
    assert result.error.category == "server"


# ---------------------------------------------------------------------------
# V6 — transient upstream LLM/network errors -> retryable, stage = failing block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["preprocess", "contract", "extract"])
def test_transient_error_is_retryable_with_correct_stage(stage: str) -> None:
    exc = _make_api_connection_error()
    kwargs = {
        "preprocess": MagicMock(side_effect=exc) if stage == "preprocess" else None,
        "determine_contract": MagicMock(side_effect=exc) if stage == "contract" else None,
        "extract_asset": MagicMock(side_effect=exc) if stage == "extract" else None,
    }
    p1, p2, p3 = _patch_blocks(**kwargs)
    with p1, p2, p3:
        result = process_document(DOC_ID, TEXT)

    assert result.ok is False
    assert result.error is not None
    assert result.error.category == "retryable"
    assert result.error.stage == stage


def test_rate_limit_error_is_retryable() -> None:
    p1, p2, p3 = _patch_blocks(extract_asset=MagicMock(side_effect=_make_rate_limit_error()))
    with p1, p2, p3:
        result = process_document(DOC_ID, TEXT)

    assert result.ok is False
    assert result.error is not None
    assert result.error.category == "retryable"
    assert result.error.stage == "extract"


def test_httpx_transport_error_is_retryable() -> None:
    exc = httpx.ConnectError("connection refused")
    p1, p2, p3 = _patch_blocks(preprocess=MagicMock(side_effect=exc))
    with p1, p2, p3:
        result = process_document(DOC_ID, TEXT)

    assert result.ok is False
    assert result.error is not None
    assert result.error.category == "retryable"
    assert result.error.stage == "preprocess"


# ---------------------------------------------------------------------------
# V7 — unexpected error -> server, current stage
# ---------------------------------------------------------------------------


def test_unexpected_error_is_server_with_current_stage() -> None:
    p1, p2, p3 = _patch_blocks(determine_contract=MagicMock(side_effect=ValueError("weird")))
    with p1, p2, p3:
        result = process_document(DOC_ID, TEXT)

    assert result.ok is False
    assert result.error is not None
    assert result.error.category == "server"
    assert result.error.stage == "contract"
    assert "weird" in result.error.message


# ---------------------------------------------------------------------------
# V8 — invariants: id always echoed, never raises
# ---------------------------------------------------------------------------


def test_id_echoed_and_never_raises_across_branches() -> None:
    from bulk_insert.blocks.contract import ContractDeterminationError

    scenarios = [
        _patch_blocks(),  # success
        _patch_blocks(determine_contract=MagicMock(side_effect=ContractDeterminationError("x"))),
        _patch_blocks(extract_asset=MagicMock(side_effect=ValueError("x"))),
        _patch_blocks(preprocess=MagicMock(side_effect=_make_api_connection_error())),
    ]
    for p1, p2, p3 in scenarios:
        with p1, p2, p3:
            result = process_document("the-id", TEXT)
        assert result.id == "the-id"

    # Blank input branch (no block patching needed).
    assert process_document("the-id", "").id == "the-id"


# ---------------------------------------------------------------------------
# V9 — low confidence is success, not an error
# ---------------------------------------------------------------------------


def test_low_confidence_is_success() -> None:
    p1, p2, p3 = _patch_blocks(extract_asset=MagicMock(return_value=_asset(confidence=0.1)))
    with p1, p2, p3:
        result = process_document(DOC_ID, TEXT)

    assert result.ok is True
    assert result.error is None
    assert result.confidence == pytest.approx(0.1)
