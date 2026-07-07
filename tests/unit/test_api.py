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


# ── Runtime model override (POST /config/model, GET /config/models) ─────────────


def test_get_models(client: TestClient) -> None:
    """C1 — GET /config/models reports the current three slugs."""
    response = client.get("/config/models")
    assert response.status_code == 200
    assert response.json() == {
        "lightweight": "test/lightweight",
        "heavy": "test/heavy",
        "embedding": "test/embedding",
    }


def test_set_heavy_model(client: TestClient) -> None:
    """C2 — swap the heavy slug; lightweight/embedding untouched."""
    response = client.post("/config/model", json={"role": "heavy", "model": "new/heavy"})
    assert response.status_code == 200
    assert response.json() == {
        "lightweight": "test/lightweight",
        "heavy": "new/heavy",
        "embedding": "test/embedding",
    }
    # Persisted for subsequent reads.
    assert client.get("/config/models").json()["heavy"] == "new/heavy"


def test_set_lightweight_model(client: TestClient) -> None:
    """C3 — swap the lightweight slug."""
    response = client.post("/config/model", json={"role": "lightweight", "model": "new/light"})
    assert response.status_code == 200
    assert response.json()["lightweight"] == "new/light"
    assert response.json()["heavy"] == "test/heavy"


def test_override_reflected_in_process_models(client: TestClient) -> None:
    """C4 — after a swap, /process reports the new slug under _models."""
    client.post("/config/model", json={"role": "heavy", "model": "swapped/heavy"})

    asset = _asset(confidence=0.9, completeness=1.0)
    with patch("bulk_insert.api.routes.process_document") as mock_process:
        mock_process.return_value = PipelineResult.success("doc-1", asset)
        response = client.post("/process", json={"id": "doc-1", "text": "hello"})

    assert response.status_code == 200
    assert response.json()["_models"]["heavy"] == "swapped/heavy"
    # process_document was called with the swapped Settings.
    called_cfg = mock_process.call_args.args[2]
    assert called_cfg.MODEL_HEAVY == "swapped/heavy"


def test_set_model_invalid_role(client: TestClient) -> None:
    """C5 — an unknown role fails validation (422)."""
    response = client.post("/config/model", json={"role": "embedding", "model": "x/y"})
    assert response.status_code == 422


def test_set_model_blank_model(client: TestClient) -> None:
    """C6 — whitespace-only model is stripped to empty -> 422."""
    response = client.post("/config/model", json={"role": "heavy", "model": "   "})
    assert response.status_code == 422


def test_set_model_disabled_returns_403() -> None:
    """C7 — with the flag off, POST is 403 but GET still works."""
    cfg = Settings(
        OPENROUTER_API_KEY="test",
        MODEL_LIGHTWEIGHT="test/lightweight",
        MODEL_HEAVY="test/heavy",
        EMBEDDING_MODEL="test/embedding",
        RUNTIME_MODEL_OVERRIDE_ENABLED=False,
    )
    app = create_app(cfg)
    app.state.ready = True
    disabled_client = TestClient(app)

    post = disabled_client.post("/config/model", json={"role": "heavy", "model": "x/y"})
    assert post.status_code == 403

    get = disabled_client.get("/config/models")
    assert get.status_code == 200
    assert get.json()["heavy"] == "test/heavy"


# ── Phase 9 — API-key auth ──────────────────────────────────────────────────────


@pytest.fixture
def secured_client() -> TestClient:
    cfg = Settings(
        OPENROUTER_API_KEY="test",
        MODEL_LIGHTWEIGHT="test/lightweight",
        MODEL_HEAVY="test/heavy",
        EMBEDDING_MODEL="test/embedding",
        API_KEY="s3cret",
    )
    app = create_app(cfg)
    app.state.ready = True
    return TestClient(app)


def test_process_without_key_is_401(secured_client: TestClient) -> None:
    """H1 — a configured key is required on /process."""
    response = secured_client.post("/process", json={"id": "doc-1", "text": "hello"})
    assert response.status_code == 401


def test_process_wrong_key_is_401(secured_client: TestClient) -> None:
    """H2 — a wrong key is rejected."""
    response = secured_client.post(
        "/process", json={"id": "doc-1", "text": "hello"}, headers={"X-API-Key": "nope"}
    )
    assert response.status_code == 401


def test_process_correct_key_passes(secured_client: TestClient) -> None:
    """H3 — the correct key lets the request through."""
    asset = _asset(confidence=0.9, completeness=1.0)
    with patch("bulk_insert.api.routes.process_document") as mock_process:
        mock_process.return_value = PipelineResult.success("doc-1", asset)
        response = secured_client.post(
            "/process", json={"id": "doc-1", "text": "hello"}, headers={"X-API-Key": "s3cret"}
        )
    assert response.status_code == 200


def test_config_endpoints_require_key(secured_client: TestClient) -> None:
    """H4 — /config/* is guarded too."""
    assert secured_client.get("/config/models").status_code == 401
    assert secured_client.get("/config/models", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_probes_are_open_when_key_configured(secured_client: TestClient) -> None:
    """H5 — /health and /ready never require a key."""
    assert secured_client.get("/health").status_code == 200
    assert secured_client.get("/ready").status_code == 200


def test_no_key_configured_means_open(client: TestClient) -> None:
    """H6 — with API_KEY unset, /process needs no header (default fixture)."""
    asset = _asset(confidence=0.9, completeness=1.0)
    with patch("bulk_insert.api.routes.process_document") as mock_process:
        mock_process.return_value = PipelineResult.success("doc-1", asset)
        response = client.post("/process", json={"id": "doc-1", "text": "hello"})
    assert response.status_code == 200


# ── Phase 9 — payload-size + shape guards ───────────────────────────────────────


def test_oversized_body_is_413() -> None:
    """H7 — a body larger than MAX_REQUEST_BYTES is rejected before parsing."""
    cfg = Settings(
        OPENROUTER_API_KEY="test",
        MODEL_LIGHTWEIGHT="test/lightweight",
        MODEL_HEAVY="test/heavy",
        EMBEDDING_MODEL="test/embedding",
        MAX_REQUEST_BYTES=50,
    )
    app = create_app(cfg)
    app.state.ready = True
    tiny_limit_client = TestClient(app)

    response = tiny_limit_client.post("/process", json={"id": "doc-1", "text": "x" * 500})
    assert response.status_code == 413


def test_request_id_header_echoed(client: TestClient) -> None:
    """H8 — the correlation id is echoed on the response header."""
    response = client.get("/health", headers={"X-Request-ID": "corr-123"})
    assert response.headers["X-Request-ID"] == "corr-123"


def test_text_over_max_length_rejected() -> None:
    """H9 — the static text char-cap is enforced at the schema layer."""
    from pydantic import ValidationError

    from bulk_insert.api.schemas import MAX_TEXT_CHARS, ProcessRequest

    ProcessRequest(id="ok", text="a" * MAX_TEXT_CHARS)  # at the cap — valid
    with pytest.raises(ValidationError):
        ProcessRequest(id="ok", text="a" * (MAX_TEXT_CHARS + 1))
