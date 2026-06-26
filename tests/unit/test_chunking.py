"""Unit tests for the sliding-window chunking logic."""

import pytest

from bulk_insert.blocks.preprocessing import create_chunks
from bulk_insert.models.chunk import Chunk


def make_sentences(n: int) -> list[str]:
    """Create a list of *n* deterministic dummy sentences."""
    return [f"Sentence {i}." for i in range(n)]


class TestCreateChunksEmpty:
    def test_empty_input_returns_empty_list(self) -> None:
        assert create_chunks([], chunk_size=3, overlap=1) == []


class TestCreateChunksSingleSentence:
    def test_single_sentence_creates_one_chunk(self) -> None:
        chunks = create_chunks(["Only one."], chunk_size=3, overlap=1)
        assert len(chunks) == 1

    def test_single_chunk_has_correct_fields(self) -> None:
        chunks = create_chunks(["Only one."], chunk_size=3, overlap=1)
        c = chunks[0]
        assert c.index == 0
        assert c.start_sentence == 0
        assert c.end_sentence == 0
        assert c.overlap_previous == 0
        assert c.is_coref_resolved is False  # set to True only after LLM call
        assert c.relevance_score is None


class TestCreateChunksNoOverlap:
    def test_non_overlapping_chunks_count(self) -> None:
        # 9 sentences, size=3, overlap=0 → 3 chunks
        chunks = create_chunks(make_sentences(9), chunk_size=3, overlap=0)
        assert len(chunks) == 3

    def test_non_overlapping_no_shared_sentences(self) -> None:
        chunks = create_chunks(make_sentences(9), chunk_size=3, overlap=0)
        for chunk in chunks:
            assert chunk.overlap_previous == 0

    def test_non_overlapping_sentence_ranges_correct(self) -> None:
        chunks = create_chunks(make_sentences(9), chunk_size=3, overlap=0)
        assert chunks[0].start_sentence == 0
        assert chunks[0].end_sentence == 2
        assert chunks[1].start_sentence == 3
        assert chunks[1].end_sentence == 5
        assert chunks[2].start_sentence == 6
        assert chunks[2].end_sentence == 8


class TestCreateChunksWith10SentencesOverlap1:
    """10 sentences, chunk_size=3, overlap=1.

    step = 3 - 1 = 2 → windows start at 0, 2, 4, 6, 8
    Expected chunks:
      0: sents[0:3]  start=0  end=2
      1: sents[2:5]  start=2  end=4
      2: sents[4:7]  start=4  end=6
      3: sents[6:9]  start=6  end=8
      4: sents[8:10] start=8  end=9  (shorter last chunk)
    """

    @pytest.fixture()
    def chunks(self) -> list[Chunk]:
        return create_chunks(make_sentences(10), chunk_size=3, overlap=1)

    def test_chunk_count(self, chunks: list[Chunk]) -> None:
        assert len(chunks) == 5

    def test_first_chunk_no_overlap(self, chunks: list[Chunk]) -> None:
        assert chunks[0].overlap_previous == 0

    def test_subsequent_chunks_have_overlap(self, chunks: list[Chunk]) -> None:
        for chunk in chunks[1:]:
            assert chunk.overlap_previous == 1

    def test_sentence_ranges(self, chunks: list[Chunk]) -> None:
        assert chunks[0].start_sentence == 0 and chunks[0].end_sentence == 2
        assert chunks[1].start_sentence == 2 and chunks[1].end_sentence == 4
        assert chunks[2].start_sentence == 4 and chunks[2].end_sentence == 6
        assert chunks[3].start_sentence == 6 and chunks[3].end_sentence == 8

    def test_last_chunk_may_be_shorter(self, chunks: list[Chunk]) -> None:
        last = chunks[-1]
        # sents[8:10] → 2 sentences
        assert last.start_sentence == 8
        assert last.end_sentence == 9

    def test_chunk_indices_sequential(self, chunks: list[Chunk]) -> None:
        for i, chunk in enumerate(chunks):
            assert chunk.index == i

    def test_relevance_score_none(self, chunks: list[Chunk]) -> None:
        assert all(c.relevance_score is None for c in chunks)

    def test_is_coref_resolved(self, chunks: list[Chunk]) -> None:
        # Chunks are NOT resolved at creation time; resolution happens during
        # the combined LLM scoring + coref call (score_and_resolve_chunk).
        assert all(c.is_coref_resolved is False for c in chunks)


class TestCreateChunksFewSentencesLargeWindow:
    def test_fewer_sentences_than_chunk_size(self) -> None:
        # 2 sentences, chunk_size=6 → 1 chunk
        chunks = create_chunks(make_sentences(2), chunk_size=6, overlap=2)
        assert len(chunks) == 1
        assert chunks[0].start_sentence == 0
        assert chunks[0].end_sentence == 1

    def test_exactly_chunk_size_sentences(self) -> None:
        chunks = create_chunks(make_sentences(6), chunk_size=6, overlap=2)
        # step = 6-2 = 4; starts at 0, 4 → 2 chunks
        assert len(chunks) == 2


class TestCreateChunksOverlapGuard:
    def test_overlap_equal_to_chunk_size_does_not_loop(self) -> None:
        # overlap >= chunk_size is guarded; should still produce finite results
        chunks = create_chunks(make_sentences(5), chunk_size=3, overlap=3)
        # effective_overlap capped to chunk_size-1=2, step=1
        assert len(chunks) > 0

    def test_text_content_non_empty(self) -> None:
        sentences = ["Apple.", "Banana.", "Cherry."]
        chunks = create_chunks(sentences, chunk_size=2, overlap=0)
        assert all(c.text for c in chunks)
