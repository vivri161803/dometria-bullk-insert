"""Pipeline orchestrator — composes Block 1 → 2 → 3 into one synchronous call.

Public entry point
------------------
:func:`process_document` runs the full extraction pipeline on a single document::

    (doc_id, text)
        → preprocess           (Block 1: chunk + score + coref-resolve)
        → determine_contract   (Block 2: lv1/lv2/lv3 category path)
        → extract_asset        (Block 3: populate the contract schema)
        → PipelineResult(id, ok, asset, confidence)

Design notes
------------
* **Never raises** — every call returns a :class:`PipelineResult`.  Known block
  exceptions, transient upstream LLM/network errors, and any unexpected error are
  all translated into a structured failure that echoes ``doc_id`` (fail-safe over
  fail-fast, per the mission).
* **Stateless** — no I/O beyond Block 3's bundled ``contracts/`` read; nothing is
  persisted; ``doc_id`` is opaque and only echoed.
* **Stage-tagged failures** — ``current_stage`` tracks the executing block so a
  transient/unexpected error reports *where* it happened.
"""

from __future__ import annotations

import logging
import time

import httpx
from openai import APIConnectionError, APIError, RateLimitError

from bulk_insert.blocks.contract import ContractDeterminationError, determine_contract
from bulk_insert.blocks.extraction import AssetExtractionError, extract_asset
from bulk_insert.blocks.preprocessing import preprocess
from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings
from bulk_insert.logging_config import set_log_block
from bulk_insert.models.contract_loader import ContractNotFoundError
from bulk_insert.models.pipeline import PipelineResult, PipelineStage

logger = logging.getLogger(__name__)

# Transient upstream failures (OpenRouter via the OpenAI SDK / httpx transport).
# RateLimitError and APIConnectionError both subclass APIError, but they are
# listed explicitly for clarity. Mapped to category="retryable" (→ HTTP 502).
_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    APIError,
    APIConnectionError,
    RateLimitError,
    httpx.TransportError,
)


def process_document(
    doc_id: str,
    text: str,
    cfg: Settings | None = None,
) -> PipelineResult:
    """Run the full pipeline on one document and return a structured result.

    Parameters
    ----------
    doc_id:
        Opaque document identifier.  Never parsed or branched on — only echoed
        onto the result (success and failure alike).
    text:
        Raw document text.
    cfg:
        Optional settings override, forwarded to every block.  Defaults to the
        process-level singleton.

    Returns
    -------
    PipelineResult
        ``ok=True`` with ``asset``/``confidence`` on success, or ``ok=False``
        with a structured :class:`~bulk_insert.models.pipeline.PipelineError`.
        Low extraction confidence is **not** an error — the asset is returned.
    """
    effective_cfg = cfg or _default_settings

    # Input guard — reject empty / whitespace-only text before any block runs.
    text_chars = len(text) if text else 0
    if not text or not text.strip():
        logger.warning(
            "Pipeline rejected document %s: empty or blank text (%d chars).",
            doc_id,
            text_chars,
            extra={"text_chars": text_chars},
        )
        return PipelineResult.failure(doc_id, "input", "bad_input", "text is empty or blank")

    started = time.perf_counter()

    def _elapsed_ms(since: float | None = None) -> float:
        return round((time.perf_counter() - (since if since is not None else started)) * 1000, 2)

    logger.info(
        "Pipeline started for document %s (%d chars of source text).",
        doc_id,
        text_chars,
        extra={"text_chars": text_chars},
    )

    current_stage: PipelineStage = "preprocess"
    try:
        set_log_block(current_stage)
        stage_started = time.perf_counter()
        logger.info(
            "Block 1/3 preprocessing — segment, chunk, score, and resolve coreferences.",
            extra={"stage": "preprocess", "text_chars": text_chars},
        )
        chunks = preprocess(text, cfg=effective_cfg)
        logger.info(
            "Block 1/3 preprocessing done — %d scored chunk(s) ready.",
            len(chunks),
            extra={
                "stage": "preprocess",
                "duration_ms": _elapsed_ms(stage_started),
                "chunk_count": len(chunks),
                "chunk_scores": [c.relevance_score for c in chunks],
            },
        )

        current_stage = "contract"
        set_log_block(current_stage)
        stage_started = time.perf_counter()
        logger.info(
            "Block 2/3 contract determination — classify lv1/lv2/lv3 from top chunks.",
            extra={"stage": "contract", "chunk_count": len(chunks)},
        )
        contract = determine_contract(chunks, cfg=effective_cfg)
        logger.info(
            "Block 2/3 contract determination done — path %s / %s / %s.",
            contract.lv1,
            contract.lv2,
            contract.lv3,
            extra={
                "stage": "contract",
                "duration_ms": _elapsed_ms(stage_started),
                "lv1": contract.lv1,
                "lv2": contract.lv2,
                "lv3": contract.lv3,
            },
        )

        current_stage = "extract"
        set_log_block(current_stage)
        stage_started = time.perf_counter()
        logger.info(
            "Block 3/3 asset extraction — populate contract fields from document evidence.",
            extra={
                "stage": "extract",
                "lv1": contract.lv1,
                "lv2": contract.lv2,
                "lv3": contract.lv3,
            },
        )
        asset = extract_asset(chunks, contract, cfg=effective_cfg)
        filled = sum(1 for value in asset.data.values() if value not in (None, "", [], {}))
        review_n = len(asset.review_queue or [])
        logger.info(
            "Block 3/3 asset extraction done — contract=%s confidence=%.3f "
            "completeness=%s filled_fields=%d/%d review_queue=%d.",
            asset.contract_id,
            asset.confidence,
            f"{asset.completeness:.3f}" if asset.completeness is not None else "n/a",
            filled,
            len(asset.data),
            review_n,
            extra={
                "stage": "extract",
                "duration_ms": _elapsed_ms(stage_started),
                "contract_id": asset.contract_id,
                "confidence": asset.confidence,
                "completeness": asset.completeness,
                "filled_fields": filled,
                "total_fields": len(asset.data),
                "review_queue_count": review_n,
            },
        )

    except ContractDeterminationError as exc:
        logger.warning(
            "Pipeline failed for document %s at contract stage (unprocessable): %s",
            doc_id,
            exc,
            extra={"stage": "contract", "duration_ms": _elapsed_ms(), "error_category": "unprocessable"},
        )
        return PipelineResult.failure(doc_id, "contract", "unprocessable", str(exc))

    except (ContractNotFoundError, AssetExtractionError) as exc:
        logger.error(
            "Pipeline failed for document %s at extract stage (server): %s",
            doc_id,
            exc,
            extra={"stage": "extract", "duration_ms": _elapsed_ms(), "error_category": "server"},
        )
        return PipelineResult.failure(doc_id, "extract", "server", str(exc))

    except _RETRYABLE_ERRORS as exc:
        logger.warning(
            "Pipeline failed for document %s at %s stage (retryable upstream): %s",
            doc_id,
            current_stage,
            exc,
            extra={
                "stage": current_stage,
                "duration_ms": _elapsed_ms(),
                "error_category": "retryable",
            },
        )
        return PipelineResult.failure(doc_id, current_stage, "retryable", str(exc))

    except Exception as exc:  # noqa: BLE001 — fail-safe catch-all, never propagate.
        logger.exception(
            "Pipeline failed for document %s at %s stage (unexpected).",
            doc_id,
            current_stage,
            extra={"stage": current_stage, "duration_ms": _elapsed_ms(), "error_category": "server"},
        )
        return PipelineResult.failure(doc_id, current_stage, "server", str(exc))

    review_queue = asset.review_queue or []
    logger.info(
        "Pipeline complete for document %s — contract=%s confidence=%.3f "
        "completeness=%s review_queue=%s.",
        doc_id,
        asset.contract_id,
        asset.confidence,
        f"{asset.completeness:.3f}" if asset.completeness is not None else "n/a",
        review_queue or [],
        extra={
            "duration_ms": _elapsed_ms(),
            "contract_id": asset.contract_id,
            "confidence": asset.confidence,
            "completeness": asset.completeness,
            "review_queue": review_queue,
            "text_chars": text_chars,
            "chunk_count": len(chunks),
        },
    )
    return PipelineResult.success(doc_id, asset)


__all__: list[str] = ["process_document"]
