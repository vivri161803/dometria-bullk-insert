"""Unit tests for the FastAPI API layer (Phase 7)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bulk_insert.api.app import create_app
from bulk_insert.config import Settings
from bulk_insert.models.asset import Asset
from bulk_insert.models.pipeline import PipelineResult


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        OPENROUTER_API_KEY="test",
        MODEL_LIGHTWEIGHT="test/lightweight",
        MODEL_HEAVY="test/heavy",
        EMBEDDING_MODEL="test/embedding",
    )


@pytest.fixture
def client(test_settings: Settings) -> TestClient:
    app = create_app(test_settings)
    app.state.ready = True  # Mock lifespan warm-up success for routes
    return TestClient(app)


def _asset(confidence: float = 0.9, completeness: float | None = None) -> Asset:
    return Asset(
        contract_id="test_contract",
        data={"test": "value"},
        confidence=confidence,
        completeness=completeness,
    )


def test_process_happy_path(client: TestClient) -> None:
    """T1 — happy path with full Asset."""
    asset = _asset(confidence=0.95, completeness=1.0)
    asset.review_queue = []
    asset.evidence = {"test": "quote"}
    asset.flags = {"test": {"flag": True}}

    with patch("bulk_insert.api.routes.process_document") as mock_process:
        mock_process.return_value = PipelineResult.success("doc-123", asset)

        response = client.post("/process", json={"id": "doc-123", "text": "hello"})

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "doc-123"
        assert data["confidence"] == 0.95
        assert data["completeness"] == 1.0

        # Check models metadata
        models = data["_models"]
        assert models["lightweight"] == "test/lightweight"
        assert models["heavy"] == "test/heavy"
        assert models["embedding"] == "test/embedding"

        # Check asset nesting
        resp_asset = data["asset"]
        assert resp_asset["contract_id"] == "test_contract"
        assert resp_asset["review_queue"] == []
        assert resp_asset["evidence"] == {"test": "quote"}
        assert resp_asset["flags"] == {"test": {"flag": True}}
        assert resp_asset["completeness"] == 1.0


def test_process_non_empty_review_queue_is_200(client: TestClient) -> None:
    """T2 — non-empty review_queue is still 200."""
    asset = _asset(confidence=0.5, completeness=0.5)
    asset.review_queue = ["missing_field"]

    with patch("bulk_insert.api.routes.process_document") as mock_process:
        mock_process.return_value = PipelineResult.success("doc-123", asset)

        response = client.post("/process", json={"id": "doc-123", "text": "hello"})

        assert response.status_code == 200
        data = response.json()
        assert data["asset"]["review_queue"] == ["missing_field"]


def test_process_completeness_lifted(client: TestClient) -> None:
    """T3 — completeness present and lifted."""
    asset = _asset(confidence=0.9, completeness=0.667)

    with patch("bulk_insert.api.routes.process_document") as mock_process:
        mock_process.return_value = PipelineResult.success("doc-123", asset)

        response = client.post("/process", json={"id": "doc-123", "text": "hello"})

        assert response.status_code == 200
        data = response.json()
        assert data["completeness"] == 0.667
        assert data["asset"]["completeness"] == 0.667


def test_process_bad_input(client: TestClient) -> None:
    """T4 — bad_input -> 400."""
    with patch("bulk_insert.api.routes.process_document") as mock_process:
        mock_process.return_value = PipelineResult.failure("doc-123", "input", "bad_input", "msg")

        response = client.post("/process", json={"id": "doc-123", "text": "hello"})

        assert response.status_code == 400
        data = response.json()
        assert data["id"] == "doc-123"
        assert data["error"]["category"] == "bad_input"


def test_process_unprocessable(client: TestClient) -> None:
    """T5 — unprocessable -> 422."""
    with patch("bulk_insert.api.routes.process_document") as mock_process:
        mock_process.return_value = PipelineResult.failure(
            "doc-123", "contract", "unprocessable", "msg"
        )

        response = client.post("/process", json={"id": "doc-123", "text": "hello"})

        assert response.status_code == 422
        assert response.json()["id"] == "doc-123"


def test_process_retryable(client: TestClient) -> None:
    """T6 — retryable -> 502."""
    with patch("bulk_insert.api.routes.process_document") as mock_process:
        mock_process.return_value = PipelineResult.failure("doc-123", "extract", "retryable", "msg")

        response = client.post("/process", json={"id": "doc-123", "text": "hello"})

        assert response.status_code == 502
        assert response.json()["id"] == "doc-123"


def test_process_server_error(client: TestClient) -> None:
    """T7 — server -> 500."""
    with patch("bulk_insert.api.routes.process_document") as mock_process:
        mock_process.return_value = PipelineResult.failure("doc-123", "extract", "server", "msg")

        response = client.post("/process", json={"id": "doc-123", "text": "hello"})

        assert response.status_code == 500
        assert response.json()["id"] == "doc-123"


def test_health_probe(client: TestClient) -> None:
    """T9 — GET /health."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_probe(client: TestClient) -> None:
    """T10 — GET /ready."""
    # App fixture sets state.ready = True
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}

    # Test not ready
    import typing

    from fastapi import FastAPI

    typing.cast(FastAPI, client.app).state.ready = False
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not ready"}


def test_request_validation_missing_text(client: TestClient) -> None:
    """T12 — missing text -> 422."""
    response = client.post("/process", json={"id": "doc-123"})
    assert response.status_code == 422


def test_request_validation_empty_text(client: TestClient) -> None:
    """T13 — empty text -> 422 (Pydantic min_length=1)."""
    response = client.post("/process", json={"id": "doc-123", "text": ""})
    assert response.status_code == 422


def test_request_validation_missing_id(client: TestClient) -> None:
    """T14 — missing id -> 422."""
    response = client.post("/process", json={"text": "hello"})
    assert response.status_code == 422
