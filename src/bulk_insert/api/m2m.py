"""M2M adapter helpers for async dispatch and callback to Connect."""

from __future__ import annotations

import fcntl
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from bulk_insert.config import Settings
from bulk_insert.models.asset import Asset
from bulk_insert.models.pipeline import PipelineResult

logger = logging.getLogger(__name__)

_PROCEDURE_LOCKS_DIR = Path("/tmp/dometria-bulk-insert-procedure-locks")
_PROCEDURE_STATE_DIR = Path("/tmp/dometria-bulk-insert-procedure-state")
_MIN_CONFIDENCE_READY = 0.50
_MIN_COMPLETENESS_READY = 0.45


def run_procedure_callback_flow(
    procedure_id: int,
    source_text: str,
    user_prompt: str | None,
    cfg: Settings,
) -> None:
    """Run extraction for one procedure and push candidates to Connect."""
    with _procedure_lock(procedure_id) as lock_acquired:
        if not lock_acquired:
            logger.warning(
                "Async procedure %s — skipped because another worker is already processing it.",
                procedure_id,
                extra={"procedure_id": procedure_id, "reason": "duplicate_callback"},
            )
            return

        if _procedure_already_completed(procedure_id):
            logger.info(
                "Async procedure %s — skipped because it was already completed earlier.",
                procedure_id,
                extra={"procedure_id": procedure_id, "reason": "already_completed"},
            )
            return

        callback_sent = _run_procedure_callback_flow_locked(
            procedure_id=procedure_id,
            source_text=source_text,
            user_prompt=user_prompt,
            cfg=cfg,
        )
        if callback_sent:
            _mark_procedure_completed(procedure_id)


def _run_procedure_callback_flow_locked(
    procedure_id: int,
    source_text: str,
    user_prompt: str | None,
    cfg: Settings,
) -> bool:
    """Run extraction for one procedure with lock already acquired."""
    text_chars = len(source_text)
    started = time.perf_counter()
    logger.info(
        "Async procedure %s — starting pipeline (%d chars).",
        procedure_id,
        text_chars,
        extra={"procedure_id": procedure_id, "text_chars": text_chars},
    )
    segments = _segment_source_text(source_text)
    if not segments:
        logger.error(
            "Async procedure %s — no non-empty source segments produced.",
            procedure_id,
            extra={"procedure_id": procedure_id},
        )
        return False

    successful_results: list[tuple[int, PipelineResult]] = []
    for index, segment in enumerate(segments, start=1):
        pipeline_input = _merge_user_prompt(segment, user_prompt)
        result = _run_pipeline(f"{procedure_id}-{index}", pipeline_input, cfg)
        if result.ok and result.asset is not None:
            successful_results.append((index, result))
            continue

        logger.error(
            "Async procedure %s — segment %d failed after dispatch accepted: %s",
            procedure_id,
            index,
            result.error.model_dump() if result.error is not None else None,
            extra={
                "procedure_id": procedure_id,
                "segment_index": index,
                "error": result.error.model_dump() if result.error is not None else None,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )

    if not successful_results:
        logger.error(
            "Async procedure %s — all segments failed. Callback skipped.",
            procedure_id,
            extra={"procedure_id": procedure_id, "segment_count": len(segments)},
        )
        return False

    lead_result = successful_results[0][1]
    logger.info(
        "Async procedure %s — %d segment(s) succeeded; building Connect callback payload.",
        procedure_id,
        len(successful_results),
        extra={
            "procedure_id": procedure_id,
            "contract_id": lead_result.asset.contract_id if lead_result.asset is not None else None,
            "confidence": lead_result.asset.confidence if lead_result.asset is not None else None,
            "completeness": lead_result.asset.completeness if lead_result.asset is not None else None,
            "segment_count": len(segments),
            "successful_segments": len(successful_results),
        },
    )

    try:
        payload = build_candidates_payload(procedure_id=procedure_id, result=successful_results, cfg=cfg)
    except ValueError as exc:
        logger.error(
            "Async procedure %s — cannot build callback payload: %s",
            procedure_id,
            exc,
            extra={"procedure_id": procedure_id, "reason": str(exc)},
        )
        return False

    candidate_count = len(payload.get("data", [])) if isinstance(payload.get("data"), list) else 0
    logger.info(
        "Async procedure %s — posting %d candidate(s) to Connect.",
        procedure_id,
        candidate_count,
        extra={"procedure_id": procedure_id, "candidate_count": candidate_count},
    )
    callback_sent = post_candidates_callback(procedure_id=procedure_id, payload=payload, cfg=cfg)
    if not callback_sent:
        logger.error(
            "Async procedure %s — callback delivery failed; completion marker not written.",
            procedure_id,
            extra={"procedure_id": procedure_id},
        )
        return False
    logger.info(
        "Async procedure %s — callback flow finished (%.0f ms).",
        procedure_id,
        (time.perf_counter() - started) * 1000,
        extra={
            "procedure_id": procedure_id,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    return True


def _run_pipeline(procedure_id: int | str, source_text: str, cfg: Settings) -> PipelineResult:
    from bulk_insert.pipeline import process_document

    return process_document(str(procedure_id), source_text, cfg)


def build_candidates_payload(
    procedure_id: int,
    result: PipelineResult | list[tuple[int, PipelineResult]],
    cfg: Settings,
    source_request_id: str | None = None,
) -> dict[str, Any]:
    """Build Connect callback body in envelope form `{data: [...]}`."""
    candidate_results: list[tuple[int, PipelineResult]]
    if isinstance(result, PipelineResult):
        candidate_results = [(1, result)]
    else:
        candidate_results = result

    callback_source_request_id = source_request_id or f"bulk-{procedure_id}-{uuid4().hex}"
    candidates: list[dict[str, Any]] = []
    for candidate_index, pipeline_result in candidate_results:
        if pipeline_result.asset is None:
            raise ValueError("missing asset on successful pipeline result")

        category_id = _extract_category_id(pipeline_result.asset.contract_id)
        native_fields = _asset_to_native_fields(pipeline_result.asset, pipeline_result.confidence, cfg)
        semantic_warnings = _semantic_warning_codes(
            pipeline_result.asset,
            native_fields,
            pipeline_result.confidence,
        )
        review_queue = _normalize_string_list(pipeline_result.asset.review_queue)
        quality_gate_ready = _is_ready_for_conversion(
            asset=pipeline_result.asset,
            confidence_score=pipeline_result.confidence,
            semantic_warnings=semantic_warnings,
        )
        if not quality_gate_ready and "state_not_eligible" not in semantic_warnings:
            semantic_warnings.append("state_not_eligible")
        callback_meta = {
            "extraction_score": pipeline_result.confidence,
            "source_file_name": pipeline_result.id,
            "contract_id": pipeline_result.asset.contract_id,
            "completeness": pipeline_result.asset.completeness,
            "review_queue": review_queue,
            "semantic_warnings": semantic_warnings,
            "ready_for_conversion": quality_gate_ready,
            "source_request_id": callback_source_request_id,
            "model_version": _model_version(cfg),
            "extraction_notes": [],
        }
        candidate = {
            "external_asset_id": f"{procedure_id}-{candidate_index}",
            "procedure_id": procedure_id,
            "category_id": category_id,
            "contract_version": cfg.M2M_CONTRACT_VERSION,
            "schema_version": cfg.M2M_SCHEMA_VERSION,
            "fields": {"native": native_fields},
            "meta": callback_meta,
        }
        candidates.append(candidate)

    return {
        "meta": {
            "segment_count": len(candidate_results),
            "successful_segments": len(candidates),
            "semantic_warning_count": sum(
                len(_normalize_string_list(candidate.get("meta", {}).get("semantic_warnings")))
                for candidate in candidates
            ),
            "source_request_id": callback_source_request_id,
            "generated_at": datetime.now(UTC).isoformat(),
        },
        "data": candidates,
    }


def post_candidates_callback(procedure_id: int, payload: dict[str, Any], cfg: Settings) -> bool:
    """POST candidates to Connect; retries transient callback failures."""
    if not cfg.CONNECT_CALLBACK_TOKEN:
        logger.error(
            "Missing callback token; cannot notify Connect.",
            extra={"procedure_id": procedure_id},
        )
        return False

    callback_url = (
        f"{cfg.CONNECT_BASE_URL.rstrip('/')}"
        f"/api/v1/integrations/dometria-bulk-insert/procedures/{procedure_id}/candidates"
    )
    headers = {"Authorization": f"Bearer {cfg.CONNECT_CALLBACK_TOKEN}"}
    source_request_id = str(payload.get("meta", {}).get("source_request_id", "")).strip()
    if source_request_id != "":
        headers["X-Request-Id"] = source_request_id

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
                return True

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
    return False


def _extract_category_id(contract_id: str) -> int:
    direct_match = re.fullmatch(r"\d+", contract_id)
    if direct_match is not None:
        return int(contract_id)

    prefixed_match = re.match(r"^(\d+)[_-]", contract_id)
    if prefixed_match is not None:
        return int(prefixed_match.group(1))

    raise ValueError(f"contract_id '{contract_id}' does not contain a numeric category id")


def _asset_to_native_fields(asset: Asset, confidence_score: float | None, cfg: Settings) -> list[dict[str, Any]]:
    confidence_label = _confidence_label(confidence_score)
    contract_fields = _contract_native_fields_by_key(asset.contract_id, cfg)
    strict_by_contract = contract_fields != {}
    native: list[dict[str, Any]] = []
    for key, value in asset.data.items():
        if strict_by_contract and key not in contract_fields:
            continue

        sanitized_value = _sanitize_value_against_contract_rules(key, value, contract_fields)
        if sanitized_value is None:
            continue
        item: dict[str, Any] = {"key": key, "value": sanitized_value}
        if sanitized_value not in (None, "", [], {}) and confidence_label is not None:
            item["confidence"] = confidence_label
        native.append(item)
    return native


def _sanitize_value_against_contract_rules(
    key: str,
    value: Any,
    contract_fields: dict[str, dict[str, Any]],
) -> Any | None:
    field_constraints = contract_fields.get(key)
    if not isinstance(field_constraints, dict):
        return _sanitize_value_by_type(value=value, field_type="")

    options = field_constraints.get("options")
    field_type = str(field_constraints.get("type", "")).strip().lower()
    if isinstance(options, list) and options:
        option_map = {
            str(option).strip().lower(): str(option)
            for option in options
            if str(option).strip() != ""
        }
        if option_map:
            if field_type == "multiselect":
                values = value if isinstance(value, list) else [value]
                normalized_values: list[str] = []
                for item in values:
                    normalized = option_map.get(str(item).strip().lower())
                    if normalized is not None:
                        normalized_values.append(normalized)

                if not normalized_values:
                    return None

                return list(dict.fromkeys(normalized_values))

            normalized_value = option_map.get(str(value).strip().lower())
            if normalized_value is None:
                return None

            return normalized_value

    if field_type == "multiselect":
        if not isinstance(value, list):
            return None
        normalized_values = [
            str(item).strip()
            for item in value
            if isinstance(item, str) and str(item).strip() != ""
        ]
        if not normalized_values:
            return None
        return list(dict.fromkeys(normalized_values))

    return _sanitize_value_by_type(value=value, field_type=field_type)


def _sanitize_value_by_type(value: Any, field_type: str) -> Any | None:
    normalized_type = field_type.strip().lower()
    if normalized_type in ("text", "date", "iso_alpha2", "select"):
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped if stripped != "" else None

    if normalized_type in ("integer", "year"):
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and re.fullmatch(r"[-+]?\d+", value.strip()):
            return int(value.strip())
        return None

    if normalized_type in ("decimal", "currency", "percentage"):
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            compact = value.strip().replace(" ", "").replace(",", ".")
            if compact == "":
                return None
            try:
                return float(compact)
            except ValueError:
                return None
        return None

    if normalized_type == "boolean":
        if isinstance(value, bool):
            return value
        return None

    if value in (None, "", [], {}):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return None


def _contract_native_fields_by_key(contract_id: str, cfg: Settings) -> dict[str, dict[str, Any]]:
    contract_path = Path(cfg.CONTRACTS_DIR) / f"{contract_id}.json"
    if not contract_path.exists():
        return {}

    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}

    data = payload.get("data")
    if not isinstance(data, dict):
        return {}

    fields = data.get("fields")
    if not isinstance(fields, dict):
        return {}

    native_fields = fields.get("native")
    if not isinstance(native_fields, list):
        return {}

    constraints_by_key: dict[str, dict[str, Any]] = {}
    for field in native_fields:
        if not isinstance(field, dict):
            continue
        key = field.get("key")
        if not isinstance(key, str) or key.strip() == "":
            continue
        constraints_by_key[key] = {
            "type": field.get("type"),
            "options": (
                field.get("constraints", {}).get("options")
                if isinstance(field.get("constraints"), dict)
                else None
            ),
        }

    return constraints_by_key


def _segment_source_text(source_text: str) -> list[str]:
    text = source_text.strip()
    if text == "":
        return []

    paragraph_segments = [
        segment.strip()
        for segment in re.split(r"\n\s*\n+", text)
        if segment.strip() != ""
    ]
    if len(paragraph_segments) > 1:
        return paragraph_segments

    return [text]


def _merge_user_prompt(source_text: str, user_prompt: str | None) -> str:
    if not isinstance(user_prompt, str):
        return source_text

    normalized_prompt = user_prompt.strip()
    if normalized_prompt == "":
        return source_text

    return f"{source_text}\n\n[User prompt]\n{normalized_prompt}"


def _confidence_label(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 0.8:
        return "high"
    if score >= 0.5:
        return "medium"
    return "low"


def _normalize_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []

    return [
        str(item).strip()
        for item in values
        if isinstance(item, str) and str(item).strip() != ""
    ]


def _semantic_warning_codes(
    asset: Asset, native_fields: list[dict[str, Any]], confidence_score: float | None
) -> list[str]:
    warning_codes: list[str] = []
    if asset.review_queue:
        warning_codes.append("review_queue_not_empty")
    if isinstance(asset.completeness, (float, int)) and float(asset.completeness) < _MIN_COMPLETENESS_READY:
        warning_codes.append("low_completeness")
    if isinstance(confidence_score, (float, int)) and float(confidence_score) < _MIN_CONFIDENCE_READY:
        warning_codes.append("low_confidence")

    native_field_values = {
        str(entry.get("key")).strip(): entry.get("value")
        for entry in native_fields
        if isinstance(entry, dict) and isinstance(entry.get("key"), str) and str(entry.get("key")).strip() != ""
    }
    name_value = native_field_values.get("name")
    if not isinstance(name_value, str) or name_value.strip() == "":
        warning_codes.append("missing_name")

    if isinstance(asset.flags, dict) and asset.flags != {}:
        warning_codes.append("semantic_field_mismatch")

    return list(dict.fromkeys(warning_codes))


def _is_ready_for_conversion(
    asset: Asset, confidence_score: float | None, semantic_warnings: list[str]
) -> bool:
    if "semantic_field_mismatch" in semantic_warnings:
        return False
    if "review_queue_not_empty" in semantic_warnings:
        return False
    if isinstance(confidence_score, (float, int)) and float(confidence_score) < _MIN_CONFIDENCE_READY:
        return False
    if isinstance(asset.completeness, (float, int)) and float(asset.completeness) < _MIN_COMPLETENESS_READY:
        return False

    return True


def _model_version(cfg: Settings) -> str:
    return (
        f"schema:{cfg.M2M_SCHEMA_VERSION};"
        f"light:{cfg.MODEL_LIGHTWEIGHT};"
        f"heavy:{cfg.MODEL_HEAVY};"
        f"embedding:{cfg.EMBEDDING_MODEL}"
    )


class _ProcedureFileLock:
    def __init__(self, procedure_id: int) -> None:
        self._procedure_id = procedure_id
        self._handle: Any | None = None
        self.acquired = False

    def __enter__(self) -> bool:
        _PROCEDURE_LOCKS_DIR.mkdir(parents=True, exist_ok=True)
        lock_file = _PROCEDURE_LOCKS_DIR / f"{self._procedure_id}.lock"
        self._handle = lock_file.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.acquired = False
            return False
        self.acquired = True
        return True

    def __exit__(self, exc_type: Any, exc: Any, exc_tb: Any) -> bool:
        if self._handle is not None:
            if self.acquired:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()

        return False


def _procedure_lock(procedure_id: int) -> _ProcedureFileLock:
    return _ProcedureFileLock(procedure_id)


def _procedure_state_path(procedure_id: int) -> Path:
    return _PROCEDURE_STATE_DIR / f"{procedure_id}.completed"


def _procedure_already_completed(procedure_id: int) -> bool:
    return _procedure_state_path(procedure_id).exists()


def _mark_procedure_completed(procedure_id: int) -> None:
    _PROCEDURE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    marker_path = _procedure_state_path(procedure_id)
    marker_path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")

