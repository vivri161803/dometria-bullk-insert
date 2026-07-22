"""Unit tests for the M2M adapter helpers."""

from __future__ import annotations

from unittest.mock import patch

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


def test_build_candidates_payload_maps_native_fields() -> None:
    cfg = _settings()
    asset = Asset(contract_id="42_residential", data={"title": "Villa", "surface": 240}, confidence=0.92)
    result = PipelineResult.success("doc-1", asset)

    payload = build_candidates_payload(procedure_id=321, result=result, cfg=cfg)

    assert list(payload.keys()) == ["data"]
    candidate = payload["data"][0]
    assert candidate["procedure_id"] == 321
    assert candidate["category_id"] == 42
    assert candidate["schema_version"] == "3"
    assert candidate["fields"]["native"][0]["key"] == "title"
    assert candidate["fields"]["native"][0]["confidence"] == "high"


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

    with (
        patch("bulk_insert.api.m2m._run_pipeline", return_value=failed_result),
        patch("bulk_insert.api.m2m.post_candidates_callback") as mock_callback,
    ):
        run_procedure_callback_flow(procedure_id=11, source_text="hello", cfg=cfg)

    assert mock_callback.call_count == 0


def test_run_procedure_callback_flow_posts_callback_on_success() -> None:
    cfg = _settings()
    asset = Asset(contract_id="42", data={"title": "Villa"}, confidence=0.7)
    success_result = PipelineResult.success("doc-1", asset)

    with (
        patch("bulk_insert.api.m2m._run_pipeline", return_value=success_result),
        patch("bulk_insert.api.m2m.post_candidates_callback") as mock_callback,
    ):
        run_procedure_callback_flow(procedure_id=11, source_text="hello", cfg=cfg)

    assert mock_callback.call_count == 1
    sent_payload = mock_callback.call_args.kwargs["payload"]
    assert sent_payload["data"][0]["procedure_id"] == 11


def test_post_candidates_callback_retries_then_succeeds() -> None:
    cfg = _settings()
    payload = {"data": [{"external_asset_id": "a1", "procedure_id": 9, "category_id": 1}]}

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

