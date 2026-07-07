"""Unit tests for structured logging (Phase 9)."""

from __future__ import annotations

import json
import logging

import pytest

from bulk_insert.config import Settings
from bulk_insert.logging_config import configure_logging, set_log_block, set_log_id


@pytest.fixture(autouse=True)
def _reset_context() -> None:
    """Reset the log-context vars so tests don't bleed into one another."""
    set_log_id("-")
    set_log_block("-")


def test_json_log_carries_id_block_and_duration(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(LOG_JSON=True, LOG_LEVEL="INFO"))
    set_log_id("doc-42")
    set_log_block("extract")

    logging.getLogger("bulk_insert.test").info("hello", extra={"duration_ms": 12.5})

    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["id"] == "doc-42"
    assert payload["block"] == "extract"
    assert payload["duration_ms"] == 12.5
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "bulk_insert.test"


def test_json_log_defaults_when_context_unset(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(LOG_JSON=True))

    logging.getLogger("bulk_insert.test").info("no-context")

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["id"] == "-"
    assert payload["block"] == "-"


def test_plain_log_format(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(LOG_JSON=False))
    set_log_id("d1")
    set_log_block("contract")

    logging.getLogger("bulk_insert.test").warning("boom")

    out = capsys.readouterr().out
    assert "[d1|contract]" in out
    assert "boom" in out
    # Not JSON in plain mode.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip().splitlines()[-1])


def test_configure_logging_is_idempotent() -> None:
    cfg = Settings(LOG_JSON=True)
    configure_logging(cfg)
    configure_logging(cfg)
    root = logging.getLogger()
    # Exactly one handler survives repeated configuration.
    assert len(root.handlers) == 1


def test_log_level_respected(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(LOG_JSON=True, LOG_LEVEL="WARNING"))
    logging.getLogger("bulk_insert.test").info("suppressed")
    logging.getLogger("bulk_insert.test").warning("kept")

    out = capsys.readouterr().out
    assert "suppressed" not in out
    assert "kept" in out
