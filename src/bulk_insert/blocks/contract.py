"""Block 2 — Contract Determination.

Public entry point
------------------
:func:`determine_contract` orchestrates the full pipeline::

    scored chunks (from Block 1)
        → select_top_chunks     (filter + top-k selection)
        → extract_lv1           (LLM classifies into 3 macro-categories)
        → weighted_majority_vote (cosine similarity + weighted vote → lv2, lv3)
        → Contract(lv1, lv2, lv3, schema_def={})

Design: two-stage approach
--------------------------
1. **Stage 1 — LLM for lv1**: The three level-1 categories are few enough
   (commercial-activities, businesses, real-estate) that a single constrained
   LLM call reliably classifies the document.  Using ``Literal`` via Instructor
   eliminates hallucination risk.

2. **Stage 2 — Cosine similarity + weighted vote for lv2 + lv3**: After
   constraining the tree to a single lv1 branch, the number of candidates is
   small (~20).  Each chunk "votes" for the candidate label with highest cosine
   similarity, weighted by its ``relevance_score`` from Block 1.

All functions are independently importable for unit testing.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from functools import lru_cache

import instructor
from sentence_transformers import SentenceTransformer

from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings
from bulk_insert.models.categories import get_candidate_labels
from bulk_insert.models.chunk import Chunk
from bulk_insert.models.contract import Contract, Level1Prediction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ContractDeterminationError(Exception):
    """Raised when contract determination fails.

    Common causes:
    - No chunks pass the minimum relevance score filter.
    - Empty chunk list provided.
    """


# ---------------------------------------------------------------------------
# Stage 0 — Chunk filtering & selection
# ---------------------------------------------------------------------------


def select_top_chunks(
    chunks: list[Chunk],
    min_score: int = 5,
    top_k: int = 5,
) -> list[Chunk]:
    """Filter chunks by minimum score and select the top-*k* by relevance.

    Parameters
    ----------
    chunks:
        Scored chunks from Block 1.
    min_score:
        Minimum ``relevance_score`` to include a chunk.
    top_k:
        Maximum number of chunks to return.

    Returns
    -------
    list[Chunk]
        Up to *top_k* chunks, sorted descending by ``relevance_score``.

    Raises
    ------
    ContractDeterminationError
        If no chunks survive the filter.
    """
    filtered = [
        c for c in chunks if c.relevance_score is not None and c.relevance_score >= min_score
    ]

    if not filtered:
        raise ContractDeterminationError(
            f"No chunks with relevance_score >= {min_score}. "
            f"Received {len(chunks)} chunk(s), "
            f"scores: {[c.relevance_score for c in chunks]}."
        )

    # Sort descending by relevance_score (already guaranteed non-None)
    filtered.sort(key=lambda c: c.relevance_score or 0, reverse=True)
    selected = filtered[:top_k]

    logger.info(
        "Chunk filtering: %d/%d passed (min_score=%d), selected top-%d. Scores: %s.",
        len(filtered),
        len(chunks),
        min_score,
        top_k,
        [c.relevance_score for c in selected],
    )
    return selected


# ---------------------------------------------------------------------------
# Stage 1 — LLM extraction of lv1
# ---------------------------------------------------------------------------

_LV1_SYSTEM_PROMPT = (
    "You are an expert Italian real-estate and business document classifier.\n\n"
    "Based on the document content below, determine the macro-category of "
    "the asset described. You MUST classify into exactly one of these three "
    "categories:\n\n"
    '- "commercial-activities": retail shops, restaurants, bars, pharmacies, '
    "gyms, salons, agencies, and other commercial businesses open to the public.\n"
    '- "businesses": companies and corporate entities described at the '
    "macro-sector level (e.g. manufacturing, logistics, ICT, agro-food).\n"
    '- "real-estate": physical properties such as buildings, apartments, villas, '
    "hotels, warehouses, offices, and land.\n\n"
    "Return a JSON object with a single key 'lv1' set to the chosen category code."
)


def _build_instructor_client(cfg: Settings) -> instructor.Instructor:
    """Construct an Instructor client pointed at OpenRouter.

    Reuses the same pattern as ``preprocessing.py``: the OpenRouter API is
    OpenAI-compatible, so we use the ``openai`` provider prefix.
    """
    client: instructor.Instructor = instructor.from_provider(
        f"openai/{cfg.MODEL_LIGHTWEIGHT}",
        base_url=cfg.OPENROUTER_BASE_URL,
        api_key=cfg.OPENROUTER_API_KEY,
    )
    return client


def extract_lv1(
    chunks: list[Chunk],
    client: instructor.Instructor,
) -> str:
    """Call the LLM to classify chunk texts into a ``Level1Literal`` value.

    Parameters
    ----------
    chunks:
        The top-k chunks to concatenate and send to the LLM.
    client:
        A configured Instructor client.

    Returns
    -------
    str
        One of ``"commercial-activities"``, ``"businesses"``, ``"real-estate"``.
    """
    combined_text = "\n---\n".join(c.text for c in chunks)

    prediction: Level1Prediction = client.chat.completions.create(
        model=getattr(client, "model", None) or "openai/gpt-4o-mini",
        response_model=Level1Prediction,
        max_retries=3,
        messages=[
            {"role": "system", "content": _LV1_SYSTEM_PROMPT},
            {"role": "user", "content": combined_text},
        ],
    )

    logger.info("Stage 1 — LLM lv1 prediction: '%s'.", prediction.lv1)
    return prediction.lv1


# ---------------------------------------------------------------------------
# Stage 2 — Cosine similarity & weighted majority vote
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _get_embedding_model(model_name: str) -> SentenceTransformer:
    """Load and cache the sentence-transformers model.

    Using :func:`functools.lru_cache` ensures the model is loaded exactly
    once per process, avoiding redundant downloads and memory usage.
    """
    logger.info("Loading sentence-transformers model: '%s' ...", model_name)
    model: SentenceTransformer = SentenceTransformer(model_name)
    logger.info("Sentence-transformers model loaded.")
    return model


def weighted_majority_vote(
    chunks: list[Chunk],
    lv1: str,
    model_name: str,
) -> tuple[str, str]:
    """Compute cosine similarity + weighted vote to determine ``(lv2, lv3)``.

    For each chunk, the candidate label with the highest cosine similarity
    receives the chunk's ``relevance_score`` as vote weight.  The candidate
    with the highest cumulative weight wins.

    Parameters
    ----------
    chunks:
        Top-k scored chunks.
    lv1:
        The level-1 category code from Stage 1.
    model_name:
        Name of the sentence-transformers model.

    Returns
    -------
    tuple[str, str]
        The winning ``(lv2_code, lv3_code)`` pair.
    """
    candidates = get_candidate_labels(lv1)
    logger.info(
        "Stage 2 — %d candidate (lv2, lv3) pairs under lv1='%s'.",
        len(candidates),
        lv1,
    )

    model = _get_embedding_model(model_name)

    chunk_texts = [c.text for c in chunks]
    label_texts = [label for _, label in candidates]

    chunk_embeddings = model.encode(chunk_texts)
    label_embeddings = model.encode(label_texts)

    # Compute cosine similarity matrix: shape (n_chunks, n_labels)
    similarity_matrix = model.similarity(chunk_embeddings, label_embeddings)

    # Weighted majority vote
    # scores[key] = cumulative weighted vote
    # max_sim[key] = highest single-chunk similarity (for tiebreaking)
    scores: dict[tuple[str, str], float] = defaultdict(float)
    max_sim: dict[tuple[str, str], float] = defaultdict(lambda: -1.0)

    for chunk_idx, chunk in enumerate(chunks):
        weight = chunk.relevance_score or 1
        # Find the best label for this chunk
        best_label_idx = int(similarity_matrix[chunk_idx].argmax())
        best_sim = float(similarity_matrix[chunk_idx][best_label_idx])
        key = candidates[best_label_idx][0]  # (lv2, lv3) tuple

        scores[key] += weight
        if best_sim > max_sim[key]:
            max_sim[key] = best_sim

    # Sort candidates: primary = cumulative score (desc), secondary = max similarity (desc)
    ranked = sorted(
        scores.items(),
        key=lambda item: (item[1], max_sim[item[0]]),
        reverse=True,
    )

    # Log top-3
    top_3 = ranked[:3]
    logger.info("Stage 2 — Top-3 candidates:")
    for rank, ((lv2, lv3), cumulative) in enumerate(top_3, 1):
        logger.info(
            "  #%d: (%s, %s) — cumulative_score=%.1f, max_similarity=%.4f",
            rank,
            lv2,
            lv3,
            cumulative,
            max_sim[(lv2, lv3)],
        )

    winner = ranked[0][0]
    logger.info("Stage 2 — Winner: (%s, %s).", winner[0], winner[1])
    return winner


# ---------------------------------------------------------------------------
# Orchestrator (public API)
# ---------------------------------------------------------------------------


def determine_contract(
    chunks: list[Chunk],
    cfg: Settings | None = None,
) -> Contract:
    """Full Block 2 pipeline: chunks → Contract(lv1, lv2, lv3).

    Pipeline steps
    --------------
    1. **Filter & select** top-k chunks by relevance score.
    2. **Stage 1**: LLM extracts lv1 (one of 3 macro-categories).
    3. **Stage 2**: Cosine similarity + weighted vote → (lv2, lv3).
    4. **Assemble** and return ``Contract``.

    Parameters
    ----------
    chunks:
        Scored chunks from Block 1.
    cfg:
        Optional settings override.  Defaults to the process-level singleton.

    Returns
    -------
    Contract
        A contract with ``lv1``, ``lv2``, ``lv3`` populated and
        ``schema_def={}`` (schema loading is deferred to Block 3).

    Raises
    ------
    ContractDeterminationError
        If no chunks pass the minimum score filter.
    """
    effective_cfg = cfg or _default_settings

    # Step 1: Filter & select top-k
    logger.info("Block 2 — Step 1/3: Chunk filtering & selection.")
    top_chunks = select_top_chunks(
        chunks,
        min_score=effective_cfg.CONTRACT_MIN_SCORE,
        top_k=effective_cfg.CONTRACT_TOP_K,
    )

    # Step 2: LLM lv1 extraction
    logger.info("Block 2 — Step 2/3: LLM lv1 extraction.")
    client = _build_instructor_client(effective_cfg)
    lv1 = extract_lv1(top_chunks, client)

    # Step 3: Cosine similarity + weighted vote → (lv2, lv3)
    logger.info("Block 2 — Step 3/3: Cosine similarity voting.")
    lv2, lv3 = weighted_majority_vote(
        top_chunks,
        lv1,
        effective_cfg.EMBEDDING_MODEL,
    )

    # Step 4: Assemble contract
    contract = Contract(lv1=lv1, lv2=lv2, lv3=lv3)
    logger.info(
        "Block 2 complete. Contract: lv1='%s', lv2='%s', lv3='%s'.",
        contract.lv1,
        contract.lv2,
        contract.lv3,
    )
    return contract


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "determine_contract",
    "select_top_chunks",
    "extract_lv1",
    "weighted_majority_vote",
    "ContractDeterminationError",
]
