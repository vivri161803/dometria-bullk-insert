"""Unit tests for preprocessing NLP functions (spaCy pipeline)."""

from bulk_insert.blocks.preprocessing import segment_sentences


class TestSegmentSentences:
    """Tests for segment_sentences()."""

    def test_empty_string_returns_empty_list(self) -> None:
        assert segment_sentences("") == []

    def test_whitespace_only_returns_empty_list(self) -> None:
        assert segment_sentences("   \n  ") == []

    def test_single_sentence(self) -> None:
        result = segment_sentences("Hello world.")
        assert len(result) == 1
        assert result[0] == "Hello world."

    def test_multiple_sentences(self) -> None:
        text = "John went to the store. He bought milk. She stayed home."
        result = segment_sentences(text)
        assert len(result) == 3

    def test_sentences_are_strings(self) -> None:
        result = segment_sentences("First sentence. Second sentence.")
        assert all(isinstance(s, str) for s in result)

    def test_no_empty_sentences_in_output(self) -> None:
        result = segment_sentences("A sentence.   Another one.")
        assert all(s.strip() for s in result)

    def test_preserves_sentence_content(self) -> None:
        text = "The tenant must pay rent. The landlord provides maintenance."
        result = segment_sentences(text)
        full = " ".join(result)
        assert "tenant" in full
        assert "landlord" in full

    def test_long_text(self) -> None:
        sentences = [f"This is sentence number {i}." for i in range(20)]
        text = " ".join(sentences)
        result = segment_sentences(text)
        assert len(result) >= 10  # sentencizer should find most boundaries
