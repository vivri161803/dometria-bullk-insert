"""Unit tests for the end-to-end preprocess() pipeline (LLM mocked)."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from bulk_insert.blocks.preprocessing import preprocess
from bulk_insert.config import Settings
from bulk_insert.models.chunk import Chunk, ChunkAnalysis


def _make_settings(**kwargs: object) -> Settings:
    defaults: dict[str, object] = {
        "OPENROUTER_API_KEY": "test-key",
        "MODEL_LIGHTWEIGHT": "openai/gpt-4o-mini",
        "CHUNK_SIZE": 3,
        "CHUNK_OVERLAP": 1,
    }
    defaults.update(kwargs)
    return Settings(**defaults)  # type: ignore[arg-type]


def _analysis(score: int = 7, resolved: str = "Resolved text.") -> ChunkAnalysis:
    return ChunkAnalysis(score=score, resolved_text=resolved)


@patch("bulk_insert.blocks.preprocessing._build_instructor_client")
class TestPreprocessPipeline:
    """End-to-end tests for preprocess() with mocked LLM."""

    def _make_client(self, mock_build: MagicMock, score: int = 7) -> MagicMock:
        client = MagicMock()
        client.chat.completions.create.return_value = _analysis(score=score)
        mock_build.return_value = client
        return client

    def test_empty_text_returns_empty_list(self, mock_build: MagicMock) -> None:
        result = preprocess("", _make_settings())
        assert result == []
        mock_build.assert_not_called()

    def test_whitespace_only_returns_empty_list(self, _mock_build: MagicMock) -> None:
        result = preprocess("   \n  ", _make_settings())
        assert result == []

    def test_returns_list_of_chunks(self, mock_build: MagicMock) -> None:
        self._make_client(mock_build)
        text = "First sentence. Second sentence. Third sentence. Fourth sentence. Fifth sentence."
        result = preprocess(text, _make_settings())
        assert isinstance(result, list)
        assert all(isinstance(c, Chunk) for c in result)

    def test_all_chunks_have_relevance_score(self, mock_build: MagicMock) -> None:
        self._make_client(mock_build, score=5)
        text = "Sentence one. Sentence two. Sentence three."
        result = preprocess(text, _make_settings())
        assert all(c.relevance_score is not None for c in result)

    def test_chunks_are_ordered(self, mock_build: MagicMock) -> None:
        self._make_client(mock_build)
        text = ". ".join(f"Sentence {i}" for i in range(10)) + "."
        result = preprocess(text, _make_settings())
        for i, chunk in enumerate(result):
            assert chunk.index == i

    def test_is_coref_resolved_true_after_pipeline(self, mock_build: MagicMock) -> None:
        """All chunks must have is_coref_resolved=True after the LLM step."""
        self._make_client(mock_build)
        text = "Alpha sentence. Beta sentence. Gamma sentence."
        result = preprocess(text, _make_settings())
        assert all(c.is_coref_resolved is True for c in result)

    def test_chunk_text_replaced_with_resolved(self, mock_build: MagicMock) -> None:
        """Chunk text must be the resolved_text returned by the LLM."""
        resolved = "Alice went to Alice's office."
        client = MagicMock()
        client.chat.completions.create.return_value = _analysis(resolved=resolved)
        mock_build.return_value = client

        result = preprocess("She went to her office.", _make_settings())
        assert all(c.text == resolved for c in result)

    def test_chunk_size_respected(self, mock_build: MagicMock) -> None:
        self._make_client(mock_build)
        settings = _make_settings(CHUNK_SIZE=3, CHUNK_OVERLAP=0)
        text = " ".join(f"Sentence {i}." for i in range(9))
        result = preprocess(text, settings)
        for chunk in result:
            sentence_count = chunk.end_sentence - chunk.start_sentence + 1
            assert sentence_count <= 3

    def test_logging_sentence_count(
        self, mock_build: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._make_client(mock_build)
        with caplog.at_level(logging.INFO, logger="bulk_insert.blocks.preprocessing"):
            preprocess("One sentence. Two sentences.", _make_settings())
        assert "sentence" in caplog.text.lower()

    def test_logging_chunk_count(
        self, mock_build: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._make_client(mock_build)
        with caplog.at_level(logging.INFO, logger="bulk_insert.blocks.preprocessing"):
            preprocess("One sentence. Two sentences. Third.", _make_settings())
        assert "chunk" in caplog.text.lower()

    def test_single_sentence_text(self, mock_build: MagicMock) -> None:
        self._make_client(mock_build, score=4)
        result = preprocess("Just one sentence.", _make_settings())
        assert len(result) == 1
        assert result[0].relevance_score == 4
        assert result[0].is_coref_resolved is True
