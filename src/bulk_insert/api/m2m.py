"""M2M adapter helpers for async dispatch and callback to Connect."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from bulk_insert.config import Settings
from bulk_insert.models.asset import Asset
from bulk_insert.models.pipeline import PipelineResult

logger = logging.getLogger(__name__)


def run_procedure_callback_flow(procedure_id: int, source_text: str, cfg: Settings) -> None:
    """Run extraction for one procedure and push candidates to Connect."""
    text_chars = len(source_text)
    started = time.perf_counter()
    logger.info(
        "Async procedure %s — starting pipeline (%d chars).",
        procedure_id,
        text_chars,
        extra={"procedure_id": procedure_id, "text_chars": text_chars},
    )
    result = _run_pipeline(procedure_id, source_text, cfg)
    if not result.ok or result.asset is None:
        logger.error(
            "Async procedure %s — pipeline failed after dispatch accepted: %s",
            procedure_id,
            result.error.model_dump() if result.error is not None else None,
            extra={
                "procedure_id": procedure_id,
                "error": result.error.model_dump() if result.error is not None else None,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return

    logger.info(
        "Async procedure %s — pipeline succeeded (contract=%s confidence=%.3f); "
        "building Connect callback payload.",
        procedure_id,
        result.asset.contract_id,
        result.asset.confidence,
        extra={
            "procedure_id": procedure_id,
            "contract_id": result.asset.contract_id,
            "confidence": result.asset.confidence,
            "completeness": result.asset.completeness,
        },
    )

    try:
        payload = build_candidates_payload(procedure_id=procedure_id, result=result, cfg=cfg)
    except ValueError as exc:
        logger.error(
            "Async procedure %s — cannot build callback payload: %s",
            procedure_id,
            exc,
            extra={"procedure_id": procedure_id, "reason": str(exc)},
        )
        return

    candidate_count = len(payload.get("data", [])) if isinstance(payload.get("data"), list) else 0
    logger.info(
        "Async procedure %s — posting %d candidate(s) to Connect.",
        procedure_id,
        candidate_count,
        extra={"procedure_id": procedure_id, "candidate_count": candidate_count},
    )
    post_candidates_callback(procedure_id=procedure_id, payload=payload, cfg=cfg)
    logger.info(
        "Async procedure %s — callback flow finished (%.0f ms).",
        procedure_id,
        (time.perf_counter() - started) * 1000,
        extra={
            "procedure_id": procedure_id,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )


def _run_pipeline(procedure_id: int, source_text: str, cfg: Settings) -> PipelineResult:
    from bulk_insert.pipeline import process_document

    return process_document(str(procedure_id), source_text, cfg)


def build_candidates_payload(procedure_id: int, result: PipelineResult, cfg: Settings) -> dict[str, Any]:
    """Build Connect callback body in envelope form `{data: [...]}`."""
    if result.asset is None:
        raise ValueError("missing asset on successful pipeline result")

    category_id = _extract_category_id(result.asset.contract_id)
    native_fields = _asset_to_native_fields(result.asset, result.confidence)
    candidate = {
        "external_asset_id": f"{procedure_id}-1",
        "procedure_id": procedure_id,
        "category_id": category_id,
        "contract_version": cfg.M2M_CONTRACT_VERSION,
        "schema_version": cfg.M2M_SCHEMA_VERSION,
        "fields": {"native": native_fields},
        "meta": {
            "extraction_score": result.confidence,
            "source_file_name": result.id,
            "extraction_notes": [],
        },
    }
    return {"data": [candidate]}


def post_candidates_callback(procedure_id: int, payload: dict[str, Any], cfg: Settings) -> None:
    """POST candidates to Connect; retries transient callback failures."""
    if not cfg.CONNECT_CALLBACK_TOKEN:
        logger.error(
            "Missing callback token; cannot notify Connect.",
            extra={"procedure_id": procedure_id},
        )
        return

    callback_url = (
        f"{cfg.CONNECT_BASE_URL.rstrip('/')}"
        f"/api/v1/integrations/dometria-bulk-insert/procedures/{procedure_id}/candidates"
    )
    headers = {"Authorization": f"Bearer {cfg.CONNECT_CALLBACK_TOKEN}"}

    attempts = max(1, cfg.CONNECT_CALLBACK_MAX_RETRIES)
    for attempt in range(1, attempts + 1):
        try:
            response = httpx.post(
                callback_url,
                headers=headers,
                json=payload,
                timeout=cfg.CONNECT_CALLBACK_TIMEOUT_SECONDS,
            )
            if response.is_success:
                logger.info(
                    "Bulk candidates callback sent.",
                    extra={
                        "procedure_id": procedure_id,
                        "status_code": response.status_code,
                        "attempt": attempt,
                    },
                )
                return

            response.raise_for_status()
        except Exception as exc:
            logger.warning(
                "Bulk candidates callback attempt failed.",
                extra={
                    "procedure_id": procedure_id,
                    "attempt": attempt,
                    "attempts": attempts,
                    "error": str(exc),
                },
            )
            if attempt < attempts:
                time.sleep(cfg.CONNECT_CALLBACK_RETRY_BACKOFF_SECONDS * attempt)

    logger.error(
        "Bulk candidates callback failed after retries.",
        extra={"procedure_id": procedure_id, "attempts": attempts},
    )


def _extract_category_id(contract_id: str) -> int:
    direct_match = re.fullmatch(r"\d+", contract_id)
    if direct_match is not None:
        return int(contract_id)

    prefixed_match = re.match(r"^(\d+)[_-]", contract_id)
    if prefixed_match is not None:
        return int(prefixed_match.group(1))

    raise ValueError(f"contract_id '{contract_id}' does not contain a numeric category id")


def _asset_to_native_fields(asset: Asset, confidence_score: float | None) -> list[dict[str, Any]]:
    confidence_label = _confidence_label(confidence_score)
    native: list[dict[str, Any]] = []
    for key, value in asset.data.items():
        item: dict[str, Any] = {"key": key, "value": value}
        if value not in (None, "", [], {}) and confidence_label is not None:
            item["confidence"] = confidence_label
        native.append(item)
    return native


def _confidence_label(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 0.8:
        return "high"
    if score >= 0.5:
        return "medium"
    return "low"

