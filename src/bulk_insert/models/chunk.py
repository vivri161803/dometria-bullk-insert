"""Chunk-related Pydantic models."""

from pydantic import BaseModel, Field


class Chunk(BaseModel):
    """A text chunk produced by the preprocessing block.

    Fields
    ------
    index:
        Zero-based position of this chunk in the document's chunk sequence.
    text:
        The joined text of the sentences in this chunk.  After scoring,
        this field holds the **coreference-resolved** version of the original
        text (coreference resolution is performed during the LLM scoring call).
    start_sentence:
        Index of the first sentence (0-based) included in this chunk.
    end_sentence:
        Index of the last sentence (0-based, **inclusive**) included in this chunk.
    overlap_previous:
        Number of sentences this chunk shares with the previous chunk.
        Always ``0`` for the first chunk.
    is_coref_resolved:
        Whether the text has undergone coreference resolution.
        Set to ``True`` after the combined LLM scoring + resolution call.
    relevance_score:
        LLM-assigned relevance score (1–10) for structured data extraction.
        ``None`` until populated by the scoring step.
    """

    index: int
    text: str
    start_sentence: int
    end_sentence: int
    overlap_previous: int
    is_coref_resolved: bool
    relevance_score: int | None = Field(default=None, ge=1, le=10)


class ChunkScore(BaseModel):
    """Instructor response schema for LLM chunk-scoring calls (score only).

    Kept for backwards compatibility and unit tests that verify scoring in
    isolation.  Production code uses :class:`ChunkAnalysis` which also
    returns the coreference-resolved text.
    """

    score: int = Field(ge=1, le=10)


class ChunkAnalysis(BaseModel):
    """Combined Instructor response schema for the scoring + coreference call.

    A single LLM call both rates the chunk's relevance and rewrites it with
    all pronouns/anaphora replaced by their explicit referents.

    Fields
    ------
    score:
        Relevance score for structured data extraction, 1 (irrelevant) – 10
        (extremely useful).  Constrained by ``Field(ge=1, le=10)``.
    resolved_text:
        The chunk text rewritten so that every pronoun and anaphoric reference
        is replaced by its resolved referent entity.
    """

    score: int = Field(ge=1, le=10)
    resolved_text: str
