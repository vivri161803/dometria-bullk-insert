"""Unit tests for the FastAPI API layer (Phase 7)."""

from __future__ import annotations

import logging
from unittest.mock import patch

import httpx
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
        CONNECT_CALLBACK_TOKEN="test-callback-token",
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


def test_process_missing_required_env_returns_503() -> None:
    """T2b — missing processing env vars are rejected immediately."""
    cfg = Settings(
        OPENROUTER_API_KEY="",
        MODEL_LIGHTWEIGHT="test/lightweight",
        MODEL_HEAVY="test/heavy",
        EMBEDDING_MODEL="test/embedding",
    )
    app = create_app(cfg)
    app.state.ready = True
    misconfigured_client = TestClient(app)

    with patch("bulk_insert.api.routes.process_document") as mock_process:
        response = misconfigured_client.post("/process", json={"id": "doc-123", "text": "hello"})

    assert response.status_code == 503
    assert "OPENROUTER_API_KEY" in response.json()["detail"]
    assert mock_process.call_count == 0


def test_procedures_happy_path_returns_202(client: TestClient) -> None:
    """M1 — valid M2M dispatch is accepted and scheduled."""
    with patch("bulk_insert.api.routes.run_procedure_callback_flow") as mock_flow:
        response = client.post(
            "/procedures",
            json={
                "procedure_id": 123,
                "source_text": "hello world",
                "user_prompt": "focus on square meters",
                "meta": {
                    "original_filename": "test.pdf",
                    "mime_type": "application/pdf",
                },
            },
        )

    assert response.status_code == 202
    assert response.json() == {"procedure_id": 123, "status": "accepted"}
    assert mock_flow.call_count == 1
    assert mock_flow.call_args.args[0] == 123
    assert mock_flow.call_args.args[1] == "hello world"


def test_procedures_accepts_missing_user_prompt(client: TestClient) -> None:
    """M2 — user_prompt is optional in V1."""
    with patch("bulk_insert.api.routes.run_procedure_callback_flow"):
        response = client.post(
            "/procedures",
            json={
                "procedure_id": 124,
                "source_text": "hello world",
                "meta": {
                    "original_filename": "test.pdf",
                    "mime_type": "application/pdf",
                },
            },
        )

    assert response.status_code == 202


def test_procedures_missing_required_env_returns_503() -> None:
    """M2b — async dispatch fails fast when callback env vars are missing."""
    cfg = Settings(
        OPENROUTER_API_KEY="test",
        MODEL_LIGHTWEIGHT="test/lightweight",
        MODEL_HEAVY="test/heavy",
        EMBEDDING_MODEL="test/embedding",
        CONNECT_CALLBACK_TOKEN="",
        CONNECT_BASE_URL="",
    )
    app = create_app(cfg)
    app.state.ready = True
    misconfigured_client = TestClient(app)

    with patch("bulk_insert.api.routes.run_procedure_callback_flow") as mock_flow:
        response = misconfigured_client.post(
            "/procedures",
            json={
                "procedure_id": 124,
                "source_text": "hello world",
                "meta": {
                    "original_filename": "test.pdf",
                    "mime_type": "application/pdf",
                },
            },
        )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "CONNECT_CALLBACK_TOKEN" in detail
    assert "CONNECT_BASE_URL" in detail
    assert mock_flow.call_count == 0


def test_procedures_validation_missing_source_text(client: TestClient) -> None:
    """M3 — missing source_text is rejected at schema layer."""
    response = client.post(
        "/procedures",
        json={
            "procedure_id": 123,
            "meta": {
                "original_filename": "test.pdf",
                "mime_type": "application/pdf",
            },
        },
    )
    assert response.status_code == 422


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


def test_procedures_bearer_auth_passes(secured_client: TestClient) -> None:
    """H1b — M2M /procedures accepts shared secret via Bearer token."""
    with patch("bulk_insert.api.routes.run_procedure_callback_flow"):
        response = secured_client.post(
            "/procedures",
            json={
                "procedure_id": 200,
                "source_text": "hello",
                "meta": {
                    "original_filename": "test.pdf",
                    "mime_type": "application/pdf",
                },
            },
            headers={"Authorization": "Bearer s3cret"},
        )
    assert response.status_code == 202


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


def test_probe_paths_skip_access_log(client: TestClient) -> None:
    """Probe traffic must not emit bulk_insert.api.access INFO lines."""
    with patch("bulk_insert.api.middleware.logger") as mock_logger:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
        assert mock_logger.info.call_count == 0

        client.get("/config/models")
        assert mock_logger.info.call_count == 1


def test_text_over_max_length_rejected() -> None:
    """H9 — the static text char-cap is enforced at the schema layer."""
    from pydantic import ValidationError

    from bulk_insert.api.schemas import MAX_TEXT_CHARS, ProcessRequest

    ProcessRequest(id="ok", text="a" * MAX_TEXT_CHARS)  # at the cap — valid
    with pytest.raises(ValidationError):
        ProcessRequest(id="ok", text="a" * (MAX_TEXT_CHARS + 1))


# ── Connection-test loop ─────────────────────────────────────────────────────────


def test_connection_test_ping_success(caplog: pytest.LogCaptureFixture) -> None:
    """CT1 — successful 2xx response is logged at INFO level."""
    from unittest.mock import MagicMock

    from bulk_insert.api.connection_test import ConnectionTestLoop

    cfg = Settings(
        OPENROUTER_API_KEY="test",
        MODEL_LIGHTWEIGHT="test/lightweight",
        MODEL_HEAVY="test/heavy",
        EMBEDDING_MODEL="test/embedding",
        CONNECT_BASE_URL="https://connect.dometria.ai",
        CONNECT_CALLBACK_TOKEN="test-token",
        CONNECT_PING_ENABLED=True,
        CONNECT_PING_INTERVAL_SECONDS=60,
    )
    loop = ConnectionTestLoop(cfg)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.is_success = True
    mock_response.text = '{"status":"ok","service":"dometria-connect"}'

    with patch("bulk_insert.api.connection_test.httpx.get", return_value=mock_response) as mock_get:
        with caplog.at_level(logging.INFO, logger="bulk_insert.api.connection_test"):
            loop._ping()

    mock_get.assert_called_once()
    call_url = mock_get.call_args.args[0]
    assert "/api/v1/integrations/dometria-bulk-insert/connection-test" in call_url
    assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert any("Connection test OK" in r.message for r in caplog.records)
    assert any("200" in r.message for r in caplog.records)


def test_connection_test_ping_non_2xx(caplog: pytest.LogCaptureFixture) -> None:
    """CT2 — non-2xx response is logged at WARNING level with body."""
    from unittest.mock import MagicMock

    from bulk_insert.api.connection_test import ConnectionTestLoop

    cfg = Settings(
        OPENROUTER_API_KEY="test",
        MODEL_LIGHTWEIGHT="test/lightweight",
        MODEL_HEAVY="test/heavy",
        EMBEDDING_MODEL="test/embedding",
        CONNECT_BASE_URL="https://connect.dometria.ai",
        CONNECT_CALLBACK_TOKEN="test-token",
    )
    loop = ConnectionTestLoop(cfg)

    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.is_success = False
    mock_response.text = '{"message":"forbidden"}'

    with patch("bulk_insert.api.connection_test.httpx.get", return_value=mock_response):
        with caplog.at_level(logging.WARNING, logger="bulk_insert.api.connection_test"):
            loop._ping()

    assert any("403" in r.message for r in caplog.records)
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_connection_test_ping_network_error(caplog: pytest.LogCaptureFixture) -> None:
    """CT3 — network exception is caught and logged at ERROR level."""
    from bulk_insert.api.connection_test import ConnectionTestLoop

    cfg = Settings(
        OPENROUTER_API_KEY="test",
        MODEL_LIGHTWEIGHT="test/lightweight",
        MODEL_HEAVY="test/heavy",
        EMBEDDING_MODEL="test/embedding",
        CONNECT_BASE_URL="https://connect.dometria.ai",
        CONNECT_CALLBACK_TOKEN="test-token",
    )
    loop = ConnectionTestLoop(cfg)

    with patch(
        "bulk_insert.api.connection_test.httpx.get",
        side_effect=httpx.ConnectTimeout("timed out"),
    ):
        with caplog.at_level(logging.ERROR, logger="bulk_insert.api.connection_test"):
            loop._ping()

    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert any("Connection test failed" in r.message for r in caplog.records)


def test_connection_test_disabled_does_not_spawn_thread() -> None:
    """CT4 — when CONNECT_PING_ENABLED=False, no thread is created."""
    from bulk_insert.api.connection_test import ConnectionTestLoop

    cfg = Settings(
        OPENROUTER_API_KEY="test",
        MODEL_LIGHTWEIGHT="test/lightweight",
        MODEL_HEAVY="test/heavy",
        EMBEDDING_MODEL="test/embedding",
        CONNECT_PING_ENABLED=False,
    )
    loop = ConnectionTestLoop(cfg)
    loop.start()
    assert loop._thread is None
    loop.stop()  # safe to call even when not started
