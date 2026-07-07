"""Block 1 — Preprocessing.

Public entry point
------------------
:func:`preprocess` orchestrates the full pipeline::

    raw text
        → segment_sentences         (spaCy sentencizer)
        → create_chunks             (sliding-window with overlap)
        → score_and_resolve_chunks  (single LLM call: score + coref resolution)
        → list[Chunk]

Design: combined scoring + coreference resolution
-------------------------------------------------
Coreference resolution is performed **during** the LLM scoring call rather
than as a separate pre-processing step.  Each chunk is sent to the lightweight
model (default GPT-4o-mini) once; the model returns a :class:`ChunkAnalysis`
object containing both the integer relevance score *and* the chunk text
rewritten with all pronouns/anaphoric references replaced by their explicit
referents.

Rationale:

* **No extra latency** — coreference resolution was previously an unavoidable
  sequential step (and had no compatible library for spaCy 3.8 / Python 3.13).
  Combining it with scoring costs zero additional LLM calls.
* **Better quality** — the LLM resolves coreferences in the context of the
  full chunk, which is semantically equivalent to document-level resolution
  for extraction purposes.
* **Simpler code** — the NLP pipeline is purely spaCy (sentencizer only, no
  model download required); the ``resolve_coreferences`` function is removed.

All other functions in this module are importable for independent testing.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import instructor
import spacy
from spacy.language import Language

from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings
from bulk_insert.llm import sampling_params
from bulk_insert.models.chunk import Chunk, ChunkAnalysis

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# spaCy pipeline — loaded lazily and cached for the lifetime of the process
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _get_nlp() -> Language:
    """Load and cache the spaCy pipeline (sentencizer only).

    Using :func:`functools.lru_cache` ensures the pipeline object is
    initialised exactly once, even across multiple calls.

    Returns
    -------
    Language
        A spaCy ``Language`` instance with sentence boundary detection enabled.
    """
    nlp = spacy.blank("en")
    nlp.add_pipe("sentencizer")
    logger.debug("spaCy pipeline loaded (sentencizer).")
    return nlp


# ---------------------------------------------------------------------------
# Sentence segmentation
# ---------------------------------------------------------------------------


def segment_sentences(text: str) -> list[str]:
    """Segment *text* into individual sentences using spaCy's sentencizer.

    Parameters
    ----------
    text:
        Input text.

    Returns
    -------
    list[str]
        List of sentence strings.  Empty list if *text* is blank.
    """
    if not text.strip():
        return []

    nlp = _get_nlp()
    doc = nlp(text)
    sentences = [sent.text.strip() for sent in doc.sents if sent.text.strip()]
    logger.debug("Segmented %d sentence(s).", len(sentences))
    return sentences


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def create_chunks(sentences: list[str], chunk_size: int, overlap: int) -> list[Chunk]:
    """Group *sentences* into overlapping chunks using a sliding window.

    Parameters
    ----------
    sentences:
        Ordered list of sentence strings.
    chunk_size:
        Maximum number of sentences per chunk.
    overlap:
        Number of sentences the current chunk shares with the previous chunk.

    Returns
    -------
    list[Chunk]
        Ordered list of :class:`~bulk_insert.models.chunk.Chunk` objects.
        ``relevance_score`` is left as ``None`` and ``is_coref_resolved`` is
        ``False``; both are set by :func:`score_and_resolve_chunk`.

    Edge cases
    ----------
    * Empty *sentences* → ``[]``.
    * ``len(sentences) <= chunk_size`` → single chunk.
    * ``overlap >= chunk_size`` → treated as ``chunk_size - 1`` to avoid
      infinite loops.
    """
    if not sentences:
        return []

    # Guard: overlap must be strictly less than chunk_size
    effective_overlap = min(overlap, max(0, chunk_size - 1))
    step = max(1, chunk_size - effective_overlap)

    chunks: list[Chunk] = []
    idx = 0
    start = 0

    while start < len(sentences):
        end_exclusive = min(start + chunk_size, len(sentences))
        window = sentences[start:end_exclusive]

        overlap_previous = effective_overlap if idx > 0 else 0

        chunks.append(
            Chunk(
                index=idx,
                text=" ".join(window),
                start_sentence=start,
                end_sentence=end_exclusive - 1,
                overlap_previous=overlap_previous,
                is_coref_resolved=False,
                relevance_score=None,
            )
        )

        idx += 1
        start += step

    logger.debug(
        "Created %d chunk(s) from %d sentence(s) (chunk_size=%d, overlap=%d, step=%d).",
        len(chunks),
        len(sentences),
        chunk_size,
        effective_overlap,
        step,
    )
    return chunks


# ---------------------------------------------------------------------------
# Combined LLM: scoring + coreference resolution
# ---------------------------------------------------------------------------

_ANALYSIS_SYSTEM_PROMPT = (
    "You are an expert document analyst. For the provided text chunk, do two things:\n\n"
    "1. SCORE: Rate the chunk on a scale from 1 to 10 based on how useful it is for "
    "structured data extraction from a legal or business document. "
    "1 = completely irrelevant (e.g. page numbers, headers, boilerplate); "
    "10 = extremely information-dense and directly useful for extraction "
    "(e.g. contains key facts, dates, names, obligations, or amounts).\n\n"
    "2. RESOLVE: Rewrite the chunk text, replacing every pronoun and anaphoric reference "
    "(e.g. 'he', 'she', 'they', 'it', 'the latter', 'the former', 'the company') "
    "with the explicit entity it refers to. Preserve all other wording exactly. "
    "If no coreferences exist, return the original text unchanged.\n\n"
    "Return a JSON object with keys 'score' (integer 1–10) and 'resolved_text' (string)."
)


def score_and_resolve_chunk(
    chunk: Chunk,
    client: instructor.Instructor,
    *,
    cfg: Settings | None = None,
) -> Chunk:
    """Score and coreference-resolve a single *chunk* in one LLM call.

    Parameters
    ----------
    chunk:
        The chunk to analyse.  ``relevance_score`` is ``None`` and
        ``is_coref_resolved`` is ``False`` on entry.
    client:
        A configured :class:`instructor.Instructor` instance.
    cfg:
        Optional settings override supplying the sampling + provider-routing
        parameters.  Defaults to the process-level singleton.

    Returns
    -------
    Chunk
        A new ``Chunk`` (via ``model_copy``) with:

        * ``text`` replaced by the coreference-resolved text,
        * ``relevance_score`` set to the LLM-assigned score,
        * ``is_coref_resolved`` set to ``True``.
    """
    sampling = sampling_params(cfg or _default_settings)
    analysis: ChunkAnalysis = client.chat.completions.create(
        model=getattr(client, "model", None) or "openai/gpt-4o-mini",
        response_model=ChunkAnalysis,
        max_retries=3,
        messages=[
            {"role": "system", "content": _ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": chunk.text},
        ],
        **sampling,
    )
    resolved = chunk.model_copy(
        update={
            "text": analysis.resolved_text,
            "relevance_score": analysis.score,
            "is_coref_resolved": True,
        }
    )
    logger.debug(
        "Chunk %d → score=%d/10, coref_resolved=%s.",
        chunk.index,
        analysis.score,
        resolved.is_coref_resolved,
    )
    return resolved


def _build_instructor_client(cfg: Settings) -> instructor.Instructor:
    """Construct an Instructor client pointed at OpenRouter.

    The OpenRouter API is OpenAI-compatible, so we use the ``openai`` provider
    prefix and pass ``base_url`` / ``api_key`` as kwargs to override defaults.
    """
    client: instructor.Instructor = instructor.from_provider(
        f"openai/{cfg.MODEL_LIGHTWEIGHT}",
        base_url=cfg.OPENROUTER_BASE_URL,
        api_key=cfg.OPENROUTER_API_KEY,
        # SDK-level backoff on 429 / transient errors, honouring Retry-After.
        max_retries=cfg.LLM_MAX_RETRIES,
        timeout=cfg.LLM_TIMEOUT,
    )
    # The `openai/` prefix above only selects the OpenAI-compatible provider.
    # The model sent to OpenRouter must be the bare slug (e.g. "openai/gpt-4o-mini"
    # or "mistralai/ministral-3b"); store it so the create call reads it back.
    client.model = cfg.MODEL_LIGHTWEIGHT  # type: ignore[attr-defined]
    return client


def score_and_resolve_chunks(chunks: list[Chunk], cfg: Settings | None = None) -> list[Chunk]:
    """Score and coreference-resolve all *chunks* via the lightweight LLM.

    One LLM call per chunk; each call returns both the relevance score and
    the coreference-resolved text (see :class:`~bulk_insert.models.chunk.ChunkAnalysis`).

    Parameters
    ----------
    chunks:
        Chunks to analyse.  May be empty.
    cfg:
        Settings instance.  Defaults to the process-level singleton.

    Returns
    -------
    list[Chunk]
        Chunks with ``relevance_score`` populated, ``text`` rewritten with
        resolved coreferences, and ``is_coref_resolved=True``.
    """
    if not chunks:
        return []

    effective_cfg = cfg or _default_settings
    client = _build_instructor_client(effective_cfg)

    analysed: list[Chunk] = []
    for chunk in chunks:
        analysed_chunk = score_and_resolve_chunk(chunk, client, cfg=effective_cfg)
        analysed.append(analysed_chunk)

    scores = [c.relevance_score for c in analysed]
    logger.info(
        "Scored + coref-resolved %d chunk(s). Scores: %s. Mean: %.1f.",
        len(analysed),
        scores,
        sum(s for s in scores if s is not None) / max(1, len(analysed)),
    )
    return analysed


# ---------------------------------------------------------------------------
# Top-level orchestrator (public API)
# ---------------------------------------------------------------------------


def preprocess(text: str, cfg: Settings | None = None) -> list[Chunk]:
    """Full preprocessing pipeline: raw text → scored, coref-resolved Chunks.

    This is the public entry point consumed by Celery tasks (Phase 6).

    Pipeline steps
    --------------
    1. **Sentence segmentation** — spaCy sentencizer splits raw text into
       individual sentences (no model download required).
    2. **Chunking** — sliding window with configurable size and overlap groups
       sentences into :class:`~bulk_insert.models.chunk.Chunk` objects.
    3. **Score + resolve** — a single LLM call per chunk (lightweight model,
       default GPT-4o-mini) returns both the relevance score (1–10) and the
       chunk text with coreferences resolved.

    Parameters
    ----------
    text:
        Raw document text received from Dometria Connect.
    cfg:
        Optional settings override.  Defaults to the process-level singleton.

    Returns
    -------
    list[Chunk]
        Ordered, scored, coreference-resolved chunks ready for downstream
        blocks.  Returns ``[]`` for empty or whitespace-only input.
    """
    effective_cfg: Settings = cfg or _default_settings

    if not text.strip():
        logger.info("preprocess received empty text — returning empty list.")
        return []

    # Step 1: Sentence segmentation
    logger.info("Step 1/3 — Sentence segmentation.")
    sentences = segment_sentences(text)
    logger.info("  → %d sentence(s) extracted.", len(sentences))

    if not sentences:
        logger.warning("No sentences extracted from non-empty text.")
        return []

    # Step 2: Chunking
    logger.info(
        "Step 2/3 — Chunking (size=%d, overlap=%d).",
        effective_cfg.CHUNK_SIZE,
        effective_cfg.CHUNK_OVERLAP,
    )
    chunks = create_chunks(sentences, effective_cfg.CHUNK_SIZE, effective_cfg.CHUNK_OVERLAP)
    logger.info("  → %d chunk(s) created.", len(chunks))

    # Step 3: Score + coreference resolution (single LLM call per chunk)
    logger.info("Step 3/3 — LLM scoring + coreference resolution.")
    analysed = score_and_resolve_chunks(chunks, effective_cfg)
    logger.info("Preprocessing complete. %d chunk(s) ready.", len(analysed))

    return analysed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "preprocess",
    "segment_sentences",
    "create_chunks",
    "score_and_resolve_chunk",
    "score_and_resolve_chunks",
]
