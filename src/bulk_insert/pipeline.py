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
    if not text or not text.strip():
        logger.warning("Pipeline id=%s rejected: empty or blank text.", doc_id)
        return PipelineResult.failure(doc_id, "input", "bad_input", "text is empty or blank")

    started = time.perf_counter()

    def _elapsed_ms() -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    current_stage: PipelineStage = "preprocess"
    try:
        set_log_block(current_stage)
        logger.info("Pipeline id=%s — Block 1/3: preprocessing.", doc_id)
        chunks = preprocess(text, cfg=effective_cfg)

        current_stage = "contract"
        set_log_block(current_stage)
        logger.info("Pipeline id=%s — Block 2/3: contract determination.", doc_id)
        contract = determine_contract(chunks, cfg=effective_cfg)

        current_stage = "extract"
        set_log_block(current_stage)
        logger.info("Pipeline id=%s — Block 3/3: asset extraction.", doc_id)
        asset = extract_asset(chunks, contract, cfg=effective_cfg)

    except ContractDeterminationError as exc:
        logger.warning("Pipeline id=%s — unprocessable at contract stage: %s", doc_id, exc)
        return PipelineResult.failure(doc_id, "contract", "unprocessable", str(exc))

    except (ContractNotFoundError, AssetExtractionError) as exc:
        logger.error("Pipeline id=%s — server error at extract stage: %s", doc_id, exc)
        return PipelineResult.failure(doc_id, "extract", "server", str(exc))

    except _RETRYABLE_ERRORS as exc:
        logger.warning(
            "Pipeline id=%s — retryable upstream error at %s stage: %s",
            doc_id,
            current_stage,
            exc,
        )
        return PipelineResult.failure(doc_id, current_stage, "retryable", str(exc))

    except Exception as exc:  # noqa: BLE001 — fail-safe catch-all, never propagate.
        logger.exception("Pipeline id=%s — unexpected error at %s stage.", doc_id, current_stage)
        return PipelineResult.failure(doc_id, current_stage, "server", str(exc))

    logger.info(
        "Pipeline id=%s — complete. contract='%s', confidence=%.3f.",
        doc_id,
        asset.contract_id,
        asset.confidence,
        extra={"duration_ms": _elapsed_ms()},
    )
    return PipelineResult.success(doc_id, asset)


__all__: list[str] = ["process_document"]
