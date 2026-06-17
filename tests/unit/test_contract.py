"""Unit tests for Block 2 — Contract Determination."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from bulk_insert.blocks.contract import (
    ContractDeterminationError,
    determine_contract,
    extract_lv1,
    select_top_chunks,
    weighted_majority_vote,
)
from bulk_insert.config import Settings
from bulk_insert.models.categories import get_candidate_paths
from bulk_insert.models.chunk import Chunk
from bulk_insert.models.contract import Contract, Level1Prediction

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_chunk(
    index: int = 0,
    text: str = "test chunk text",
    relevance_score: int | None = 7,
) -> Chunk:
    return Chunk(
        index=index,
        text=text,
        start_sentence=0,
        end_sentence=0,
        overlap_previous=0,
        is_coref_resolved=True,
        relevance_score=relevance_score,
    )


# ---------------------------------------------------------------------------
# select_top_chunks
# ---------------------------------------------------------------------------


class TestSelectTopChunks:
    """Tests for select_top_chunks()."""

    def test_filters_by_score(self) -> None:
        chunks = [
            _make_chunk(index=0, relevance_score=3),
            _make_chunk(index=1, relevance_score=5),
            _make_chunk(index=2, relevance_score=7),
            _make_chunk(index=3, relevance_score=9),
        ]
        result = select_top_chunks(chunks, min_score=5, top_k=10)
        assert len(result) == 3
        scores = [c.relevance_score for c in result]
        assert all(s is not None and s >= 5 for s in scores)

    def test_limits_to_top_k(self) -> None:
        chunks = [_make_chunk(index=i, relevance_score=i + 1) for i in range(10)]
        result = select_top_chunks(chunks, min_score=1, top_k=5)
        assert len(result) == 5
        # Should be the top-5 by score, descending
        assert result[0].relevance_score == 10
        assert result[4].relevance_score == 6

    def test_empty_after_filter_raises(self) -> None:
        chunks = [
            _make_chunk(index=0, relevance_score=1),
            _make_chunk(index=1, relevance_score=2),
        ]
        with pytest.raises(ContractDeterminationError, match="relevance_score >= 5"):
            select_top_chunks(chunks, min_score=5, top_k=5)

    def test_none_scores_excluded(self) -> None:
        chunks = [
            _make_chunk(index=0, relevance_score=None),
            _make_chunk(index=1, relevance_score=7),
            _make_chunk(index=2, relevance_score=None),
        ]
        result = select_top_chunks(chunks, min_score=5, top_k=10)
        assert len(result) == 1
        assert result[0].relevance_score == 7

    def test_empty_input_raises(self) -> None:
        with pytest.raises(ContractDeterminationError):
            select_top_chunks([], min_score=5, top_k=5)

    def test_sorted_descending(self) -> None:
        chunks = [
            _make_chunk(index=0, relevance_score=5),
            _make_chunk(index=1, relevance_score=9),
            _make_chunk(index=2, relevance_score=7),
        ]
        result = select_top_chunks(chunks, min_score=5, top_k=10)
        scores = [c.relevance_score for c in result]
        assert scores == [9, 7, 5]


# ---------------------------------------------------------------------------
# extract_lv1 (mocked)
# ---------------------------------------------------------------------------


class TestExtractLv1:
    """Tests for extract_lv1() with mocked Instructor client."""

    def test_returns_valid_lv1(self) -> None:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = Level1Prediction(
            lv1="commercial-activities"
        )

        chunks = [_make_chunk(text="Un ristorante nel centro di Roma")]
        result = extract_lv1(chunks, mock_client)

        assert result == "commercial-activities"

    def test_uses_level1prediction_model(self) -> None:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = Level1Prediction(lv1="real-estate")

        chunks = [_make_chunk(text="Villa con giardino")]
        extract_lv1(chunks, mock_client)

        call_kwargs = mock_client.chat.completions.create.call_args
        assert call_kwargs.kwargs["response_model"] is Level1Prediction

    def test_max_retries_set(self) -> None:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = Level1Prediction(lv1="businesses")

        chunks = [_make_chunk(text="Azienda di logistica")]
        extract_lv1(chunks, mock_client)

        call_kwargs = mock_client.chat.completions.create.call_args
        assert call_kwargs.kwargs["max_retries"] == 3

    def test_concatenates_chunk_texts(self) -> None:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = Level1Prediction(
            lv1="commercial-activities"
        )

        chunks = [
            _make_chunk(index=0, text="Primo testo"),
            _make_chunk(index=1, text="Secondo testo"),
        ]
        extract_lv1(chunks, mock_client)

        call_kwargs = mock_client.chat.completions.create.call_args
        user_msg = call_kwargs.kwargs["messages"][1]["content"]
        assert "Primo testo" in user_msg
        assert "Secondo testo" in user_msg
        assert "\n---\n" in user_msg


# ---------------------------------------------------------------------------
# weighted_majority_vote (uses real embedding model)
# ---------------------------------------------------------------------------


class TestWeightedMajorityVote:
    """Tests for weighted_majority_vote().

    These tests use the real sentence-transformers model. The model will
    download on first run (~100MB) and is cached by lru_cache.
    """

    MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

    def test_restaurant_text(self) -> None:
        """Italian restaurant text should map to food-beverage > restaurant."""
        chunks = [
            _make_chunk(
                index=0,
                text="Ristorante con cucina tradizionale italiana nel centro storico",
                relevance_score=9,
            ),
            _make_chunk(
                index=1,
                text="Il locale offre piatti di pasta fresca e pizza al forno a legna",
                relevance_score=8,
            ),
        ]
        lv2, lv3 = weighted_majority_vote(chunks, "commercial-activities", self.MODEL_NAME)
        assert lv2 == "food-beverage"
        assert lv3 == "restaurant"

    def test_weights_matter(self) -> None:
        """A high-weight chunk should dominate a low-weight chunk voting differently."""
        # High-score chunk clearly about a restaurant
        dominant = _make_chunk(
            index=0,
            text="Ristorante elegante con menu degustazione di pesce",
            relevance_score=10,
        )
        # Low-score chunk about something else
        weak = _make_chunk(
            index=1,
            text="Palestra con attrezzature moderne e corsi fitness",
            relevance_score=1,
        )
        lv2, lv3 = weighted_majority_vote(
            [dominant, weak], "commercial-activities", self.MODEL_NAME
        )
        # Restaurant should win because of the dominant chunk's weight
        assert lv2 == "food-beverage"

    def test_all_candidates_generated(self) -> None:
        """Verify all (lv2, lv3) pairs under the lv1 are considered."""
        chunks = [_make_chunk(text="testo generico", relevance_score=5)]
        # We just need it to run without error and return a valid pair
        lv2, lv3 = weighted_majority_vote(chunks, "businesses", self.MODEL_NAME)
        # Result should be a valid path under businesses
        all_paths = get_candidate_paths("businesses")
        assert (lv2, lv3) in all_paths

    def test_real_estate_villa(self) -> None:
        """Villa description should map to prestige-residential-properties > villa."""
        chunks = [
            _make_chunk(
                index=0,
                text="Villa di prestigio con piscina e giardino privato in collina",
                relevance_score=9,
            ),
            _make_chunk(
                index=1,
                text="La proprietà dispone di 500mq con vista panoramica",
                relevance_score=8,
            ),
        ]
        lv2, lv3 = weighted_majority_vote(chunks, "real-estate", self.MODEL_NAME)
        assert lv3 == "villa"


# ---------------------------------------------------------------------------
# determine_contract (end-to-end with mocked LLM)
# ---------------------------------------------------------------------------


class TestDetermineContract:
    """Tests for determine_contract() — LLM is mocked, embeddings are real."""

    def _make_cfg(self) -> Settings:
        return Settings(
            OPENROUTER_API_KEY="test-key",
            EMBEDDING_MODEL="paraphrase-multilingual-MiniLM-L12-v2",
            CONTRACT_MIN_SCORE=5,
            CONTRACT_TOP_K=5,
        )

    @patch("bulk_insert.blocks.contract._build_instructor_client")
    def test_e2e_restaurant(self, mock_build: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = Level1Prediction(
            lv1="commercial-activities"
        )
        mock_build.return_value = mock_client

        chunks = [
            _make_chunk(
                index=0,
                text="Ristorante con cucina tradizionale italiana nel centro storico",
                relevance_score=9,
            ),
            _make_chunk(
                index=1,
                text="Il locale serve piatti di pasta e pizza. Ambiente accogliente.",
                relevance_score=8,
            ),
            _make_chunk(
                index=2,
                text="Capacità di 50 coperti con terrazza estiva",
                relevance_score=7,
            ),
        ]

        contract = determine_contract(chunks, cfg=self._make_cfg())

        assert isinstance(contract, Contract)
        assert contract.lv1 == "commercial-activities"
        assert contract.lv2 == "food-beverage"
        assert contract.lv3 == "restaurant"
        assert contract.schema_def == {}

    @patch("bulk_insert.blocks.contract._build_instructor_client")
    def test_empty_chunks_raises(self, _mock_build: MagicMock) -> None:
        with pytest.raises(ContractDeterminationError):
            determine_contract([], cfg=self._make_cfg())

    @patch("bulk_insert.blocks.contract._build_instructor_client")
    def test_all_low_scores_raises(self, _mock_build: MagicMock) -> None:
        chunks = [
            _make_chunk(index=0, relevance_score=1),
            _make_chunk(index=1, relevance_score=2),
            _make_chunk(index=2, relevance_score=3),
        ]
        with pytest.raises(ContractDeterminationError):
            determine_contract(chunks, cfg=self._make_cfg())

    @patch("bulk_insert.blocks.contract._build_instructor_client")
    def test_contract_has_empty_schema(self, mock_build: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = Level1Prediction(lv1="real-estate")
        mock_build.return_value = mock_client

        chunks = [
            _make_chunk(
                index=0,
                text="Villa di lusso con piscina e giardino",
                relevance_score=9,
            ),
        ]

        contract = determine_contract(chunks, cfg=self._make_cfg())
        assert contract.schema_def == {}


# ---------------------------------------------------------------------------
# Contract model — default schema
# ---------------------------------------------------------------------------


class TestContractDefaultSchema:
    """Verify Contract can be created without explicitly providing schema_def."""

    def test_contract_without_schema(self) -> None:
        c = Contract(lv1="real-estate", lv2="specialist-destinations", lv3="villa")
        assert c.schema_def == {}
        assert c.lv1 == "real-estate"

    def test_contract_with_schema(self) -> None:
        c = Contract(
            lv1="real-estate",
            lv2="specialist-destinations",
            lv3="villa",
            schema_def={"key": "value"},
        )
        assert c.schema_def == {"key": "value"}

    def test_contract_json_round_trip_no_schema(self) -> None:
        c = Contract(lv1="businesses", lv2="company-macro-sector", lv3="company-agro-food")
        data = c.model_dump(by_alias=True)
        assert data["schema"] == {}
        reloaded = Contract.model_validate(data)
        assert reloaded == c
