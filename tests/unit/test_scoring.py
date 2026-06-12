"""Unit tests for combined LLM scoring + coreference resolution."""

from unittest.mock import MagicMock, patch

import pytest
from bulk_insert.blocks.preprocessing import score_and_resolve_chunk, score_and_resolve_chunks
from bulk_insert.config import Settings
from bulk_insert.models.chunk import Chunk, ChunkAnalysis, ChunkScore
from pydantic import ValidationError


def make_chunk(index: int = 0, text: str = "John went to the store. He bought milk.") -> Chunk:
    return Chunk(
        index=index,
        text=text,
        start_sentence=0,
        end_sentence=1,
        overlap_previous=0,
        is_coref_resolved=False,
    )


class TestChunkAnalysisModel:
    """Verify ChunkAnalysis Pydantic constraints."""

    def test_valid(self) -> None:
        a = ChunkAnalysis(score=7, resolved_text="John went to the store. John bought milk.")
        assert a.score == 7
        assert "John" in a.resolved_text

    def test_score_too_low(self) -> None:
        with pytest.raises(ValidationError):
            ChunkAnalysis(score=0, resolved_text="text")

    def test_score_too_high(self) -> None:
        with pytest.raises(ValidationError):
            ChunkAnalysis(score=11, resolved_text="text")

    def test_json_round_trip(self) -> None:
        a = ChunkAnalysis(score=5, resolved_text="Alice called Bob. Alice did not answer.")
        reloaded = ChunkAnalysis.model_validate_json(a.model_dump_json())
        assert reloaded == a


class TestScoreAndResolveChunk:
    """Tests for score_and_resolve_chunk() with a mocked Instructor client."""

    def _make_client(self, score: int, resolved_text: str) -> MagicMock:
        client = MagicMock()
        client.chat.completions.create.return_value = ChunkAnalysis(
            score=score,
            resolved_text=resolved_text,
        )
        return client

    def test_relevance_score_written(self) -> None:
        client = self._make_client(score=8, resolved_text="John bought milk.")
        result = score_and_resolve_chunk(make_chunk(), client)
        assert result.relevance_score == 8

    def test_text_replaced_with_resolved(self) -> None:
        resolved = "John went to the store. John bought milk."
        client = self._make_client(score=6, resolved_text=resolved)
        result = score_and_resolve_chunk(make_chunk(), client)
        assert result.text == resolved

    def test_is_coref_resolved_set_true(self) -> None:
        client = self._make_client(score=5, resolved_text="resolved text")
        result = score_and_resolve_chunk(make_chunk(), client)
        assert result.is_coref_resolved is True

    def test_original_chunk_not_mutated(self) -> None:
        chunk = make_chunk()
        client = self._make_client(score=7, resolved_text="new text")
        result = score_and_resolve_chunk(chunk, client)
        # model_copy → original unchanged
        assert chunk.relevance_score is None
        assert chunk.is_coref_resolved is False
        assert chunk.text != result.text

    def test_chunk_analysis_used_as_response_model(self) -> None:
        client = self._make_client(score=5, resolved_text="x")
        score_and_resolve_chunk(make_chunk(), client)
        call_kwargs = client.chat.completions.create.call_args.kwargs
        assert call_kwargs["response_model"] is ChunkAnalysis

    def test_max_retries_passed(self) -> None:
        client = self._make_client(score=4, resolved_text="x")
        score_and_resolve_chunk(make_chunk(), client)
        call_kwargs = client.chat.completions.create.call_args.kwargs
        assert call_kwargs.get("max_retries", 1) >= 1

    def test_score_boundary_low(self) -> None:
        client = self._make_client(score=1, resolved_text="low relevance text")
        result = score_and_resolve_chunk(make_chunk(), client)
        assert result.relevance_score == 1

    def test_score_boundary_high(self) -> None:
        client = self._make_client(score=10, resolved_text="high relevance text")
        result = score_and_resolve_chunk(make_chunk(), client)
        assert result.relevance_score == 10

    def test_invalid_score_rejected_by_pydantic(self) -> None:
        with pytest.raises(ValidationError):
            ChunkAnalysis(score=0, resolved_text="x")
        with pytest.raises(ValidationError):
            ChunkAnalysis(score=11, resolved_text="x")


class TestScoreAndResolveChunks:
    """Tests for score_and_resolve_chunks() — iterates over a list of chunks."""

    def _make_settings(self) -> Settings:
        return Settings(OPENROUTER_API_KEY="test-key", MODEL_LIGHTWEIGHT="openai/gpt-4o-mini")

    @patch("bulk_insert.blocks.preprocessing._build_instructor_client")
    def test_empty_list_returns_empty(self, mock_build: MagicMock) -> None:
        result = score_and_resolve_chunks([], self._make_settings())
        assert result == []
        mock_build.assert_not_called()

    @patch("bulk_insert.blocks.preprocessing._build_instructor_client")
    def test_all_chunks_scored_and_resolved(self, mock_build: MagicMock) -> None:
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            ChunkAnalysis(score=3, resolved_text="Alice went to Alice's office."),
            ChunkAnalysis(score=8, resolved_text="Bob signed the contract."),
            ChunkAnalysis(score=5, resolved_text="Carol reviewed the lease."),
        ]
        mock_build.return_value = client

        chunks = [make_chunk(i) for i in range(3)]
        analysed = score_and_resolve_chunks(chunks, self._make_settings())

        assert len(analysed) == 3
        assert analysed[0].relevance_score == 3
        assert analysed[1].relevance_score == 8
        assert analysed[2].relevance_score == 5
        assert all(c.is_coref_resolved is True for c in analysed)

    @patch("bulk_insert.blocks.preprocessing._build_instructor_client")
    def test_resolved_text_propagated(self, mock_build: MagicMock) -> None:
        resolved = "Alice called Bob. Alice did not answer."
        client = MagicMock()
        client.chat.completions.create.return_value = ChunkAnalysis(score=6, resolved_text=resolved)
        mock_build.return_value = client

        analysed = score_and_resolve_chunks([make_chunk()], self._make_settings())
        assert analysed[0].text == resolved


class TestChunkScoreBackcompat:
    """ChunkScore (score-only model) still works for isolated scoring tests."""

    def test_valid(self) -> None:
        cs = ChunkScore(score=5)
        assert cs.score == 5

    def test_invalid_low(self) -> None:
        with pytest.raises(ValidationError):
            ChunkScore(score=0)

    def test_invalid_high(self) -> None:
        with pytest.raises(ValidationError):
            ChunkScore(score=11)
