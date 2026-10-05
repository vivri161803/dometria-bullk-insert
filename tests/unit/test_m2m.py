"""Unit tests for the M2M adapter helpers."""

from __future__ import annotations

import json
from unittest.mock import patch

import bulk_insert.api.m2m as m2m
import pytest
from bulk_insert.api.m2m import build_candidates_payload, post_candidates_callback, run_procedure_callback_flow
from bulk_insert.config import Settings
from bulk_insert.models.asset import Asset
from bulk_insert.models.pipeline import PipelineResult


def _settings() -> Settings:
    return Settings(
        OPENROUTER_API_KEY="test",
        CONNECT_BASE_URL="https://connect.dometria.ai",
        CONNECT_CALLBACK_TOKEN="token-123",
        CONNECT_CALLBACK_TIMEOUT_SECONDS=1.0,
        CONNECT_CALLBACK_MAX_RETRIES=3,
        CONNECT_CALLBACK_RETRY_BACKOFF_SECONDS=0.0,
    )


@pytest.fixture(autouse=True)
def isolate_procedure_runtime_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(m2m, "_PROCEDURE_LOCKS_DIR", tmp_path / "locks")
    monkeypatch.setattr(m2m, "_PROCEDURE_STATE_DIR", tmp_path / "state")


def test_build_candidates_payload_maps_native_fields() -> None:
    cfg = _settings()
    asset = Asset(contract_id="42_residential", data={"title": "Villa", "surface": 240}, confidence=0.92)
    result = PipelineResult.success("doc-1", asset)

    payload = build_candidates_payload(procedure_id=321, result=result, cfg=cfg)

    assert list(payload.keys()) == ["meta", "data"]
    assert payload["meta"]["segment_count"] == 1
    assert payload["meta"]["source_request_id"].startswith("bulk-321-")
    candidate = payload["data"][0]
    assert candidate["procedure_id"] == 321
    assert candidate["category_id"] == 42
    assert candidate["schema_version"] == "3"
    assert candidate["fields"]["native"][0]["key"] == "title"
    assert candidate["fields"]["native"][0]["confidence"] == "high"
    assert candidate["meta"]["contract_id"] == "42_residential"
    assert candidate["meta"]["source_request_id"] == payload["meta"]["source_request_id"]
    assert "model_version" in candidate["meta"]


def test_build_candidates_payload_requires_numeric_category() -> None:
    cfg = _settings()
    asset = Asset(contract_id="contract_without_numeric_id", data={"title": "Villa"}, confidence=0.9)
    result = PipelineResult.success("doc-1", asset)

    try:
        build_candidates_payload(procedure_id=1, result=result, cfg=cfg)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_run_procedure_callback_flow_skips_callback_on_pipeline_error() -> None:
    cfg = _settings()
    failed_result = PipelineResult.failure("doc-1", "extract", "server", "boom")

    with patch("bulk_insert.api.m2m._run_pipeline", return_value=failed_result), patch(
        "bulk_insert.api.m2m.post_candidates_callback"
    ) as mock_callback:
        run_procedure_callback_flow(procedure_id=11, source_text="hello", user_prompt=None, cfg=cfg)

    assert mock_callback.call_count == 0


def test_run_procedure_callback_flow_posts_callback_on_success() -> None:
    cfg = _settings()
    asset = Asset(contract_id="42", data={"title": "Villa"}, confidence=0.7)
    success_result = PipelineResult.success("doc-1", asset)

    with patch("bulk_insert.api.m2m._run_pipeline", return_value=success_result), patch(
        "bulk_insert.api.m2m.post_candidates_callback"
    ) as mock_callback:
        run_procedure_callback_flow(procedure_id=11, source_text="hello", user_prompt="focus", cfg=cfg)

    assert mock_callback.call_count == 1
    sent_payload = mock_callback.call_args.kwargs["payload"]
    assert sent_payload["data"][0]["procedure_id"] == 11


def test_build_candidates_payload_supports_multiple_candidates() -> None:
    cfg = _settings()
    first = PipelineResult.success("doc-1", Asset(contract_id="42_one", data={"title": "A"}, confidence=0.92))
    second = PipelineResult.success("doc-2", Asset(contract_id="43_two", data={"title": "B"}, confidence=0.88))

    payload = build_candidates_payload(
        procedure_id=321,
        result=[(1, first), (2, second)],
        cfg=cfg,
    )

    assert len(payload["data"]) == 2
    assert payload["meta"]["segment_count"] == 2
    assert payload["meta"]["successful_segments"] == 2
    assert payload["data"][0]["external_asset_id"] == "321-1"
    assert payload["data"][1]["external_asset_id"] == "321-2"
    assert payload["data"][0]["category_id"] == 42
    assert payload["data"][1]["category_id"] == 43


def test_build_candidates_payload_includes_semantic_warning_codes() -> None:
    cfg = _settings()
    asset = Asset(contract_id="42_one", data={"title": "A"}, confidence=0.55, completeness=0.3)
    asset.review_queue = ["surface_sqm"]
    asset.flags = {"title": {"mismatch": True}}
    payload = build_candidates_payload(
        procedure_id=600,
        result=PipelineResult.success("doc-1", asset),
        cfg=cfg,
    )

    candidate_meta = payload["data"][0]["meta"]
    assert "low_completeness" in candidate_meta["semantic_warnings"]
    assert "review_queue_not_empty" in candidate_meta["semantic_warnings"]
    assert "semantic_field_mismatch" in candidate_meta["semantic_warnings"]
    assert "state_not_eligible" in candidate_meta["semantic_warnings"]
    assert candidate_meta["review_queue"] == ["surface_sqm"]
    assert candidate_meta["ready_for_conversion"] is False


def test_build_candidates_payload_enforces_contract_field_coherence(tmp_path) -> None:
    cfg = _settings().model_copy(update={"CONTRACTS_DIR": tmp_path})
    contract_payload = {
        "data": {
            "fields": {
                "native": [
                    {"key": "title", "type": "text"},
                    {"key": "rooms", "type": "integer"},
                    {
                        "key": "kind",
                        "type": "select",
                        "constraints": {"options": ["bar", "restaurant"]},
                    },
                    {
                        "key": "services",
                        "type": "multiselect",
                        "constraints": {"options": ["wifi", "parking"]},
                    },
                ]
            }
        }
    }
    (tmp_path / "42_residential.json").write_text(json.dumps(contract_payload), encoding="utf-8")
    asset = Asset(
        contract_id="42_residential",
        data={
            "title": " Bar Centrale ",
            "rooms": "5",
            "kind": "hotel",
            "services": ["wifi", "invalid"],
            "unknown_field": "must be dropped",
        },
        confidence=0.92,
    )

    payload = build_candidates_payload(
        procedure_id=702,
        result=PipelineResult.success("doc-1", asset),
        cfg=cfg,
    )

    native = payload["data"][0]["fields"]["native"]
    by_key = {entry["key"]: entry["value"] for entry in native}
    assert by_key["title"] == "Bar Centrale"
    assert by_key["rooms"] == 5
    assert by_key["services"] == ["wifi"]
    assert "kind" not in by_key
    assert "unknown_field" not in by_key


def test_post_candidates_callback_retries_then_succeeds() -> None:
    cfg = _settings()
    payload = {
        "meta": {"source_request_id": "bulk-source-9"},
        "data": [{"external_asset_id": "a1", "procedure_id": 9, "category_id": 1}],
    }

    class _Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.is_success = 200 <= status_code < 300

        def raise_for_status(self) -> None:
            if not self.is_success:
                raise RuntimeError(f"status {self.status_code}")

    with patch(
        "bulk_insert.api.m2m.httpx.post",
        side_effect=[_Response(500), _Response(200)],
    ) as mock_post:
        post_candidates_callback(procedure_id=9, payload=payload, cfg=cfg)

    assert mock_post.call_count == 2
    headers = mock_post.call_args.kwargs["headers"]
    assert headers["X-Request-Id"] == "bulk-source-9"


def test_run_procedure_callback_flow_is_idempotent_after_first_success() -> None:
    cfg = _settings()
    success_result = PipelineResult.success(
        "doc-1",
        Asset(contract_id="42", data={"title": "Villa"}, confidence=0.7),
    )

    with patch("bulk_insert.api.m2m._run_pipeline", return_value=success_result) as mock_pipeline, patch(
        "bulk_insert.api.m2m.post_candidates_callback", return_value=True
    ) as mock_callback:
        run_procedure_callback_flow(procedure_id=77, source_text="hello", user_prompt=None, cfg=cfg)
        run_procedure_callback_flow(procedure_id=77, source_text="hello", user_prompt=None, cfg=cfg)

    assert mock_pipeline.call_count == 1
    assert mock_callback.call_count == 1

