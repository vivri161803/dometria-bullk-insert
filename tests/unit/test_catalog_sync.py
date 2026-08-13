"""Unit tests for periodic catalog sync loop."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bulk_insert.api.catalog_sync import CatalogSyncLoop
from bulk_insert.api.catalog_store import read_active_snapshot_path
from bulk_insert.config import Settings


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "OPENROUTER_API_KEY": "test",
        "CONNECT_CALLBACK_TOKEN": "token",
        "CATALOG_CACHE_ROOT": tmp_path / "cache",
        "CATALOG_SYNC_ENABLED": True,
        "CATALOG_SYNC_MAX_RETRIES": 1,
        "CATALOG_SYNC_STARTUP_BLOCKING": True,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _categories_tree() -> list[dict]:
    return [
        {
            "id": 1,
            "code": "real-estate",
            "name": "Immobili",
            "level": 1,
            "children": [
                {
                    "id": 2,
                    "code": "residential",
                    "name": "Residenziale",
                    "level": 2,
                    "children": [
                        {
                            "id": 3,
                            "code": "villa",
                            "name": "Villa",
                            "level": 3,
                            "children": [],
                        }
                    ],
                }
            ],
        }
    ]


def _contract_payload() -> dict:
    return {
        "schema_version": "3",
        "category": {"id": 3, "level": 3, "name": "Villa"},
        "fields": {
            "native": [
                {
                    "key": "name",
                    "label": "Denominazione",
                    "source": "standard",
                    "type": "text",
                    "is_required": True,
                    "constraints": None,
                }
            ]
        },
    }


def test_sync_once_builds_and_publishes_snapshot(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    loop = CatalogSyncLoop(cfg)

    with (
        patch.object(loop._client, "fetch_categories_tree", return_value=_categories_tree()),
        patch.object(loop._client, "fetch_contract", return_value=_contract_payload()),
    ):
        assert loop.sync_once(reason="test") is True

    active = read_active_snapshot_path(cfg)
    assert active is not None
    assert (active / "contracts" / "3_villa.json").exists()
    assert (active / "artifacts" / "field_tree.json").exists()
    assert (active / "categories.json").exists()
    assert cfg.CATEGORY_CATALOG_PATH == active / "categories.json"


def test_prepare_startup_snapshot_requires_successful_sync(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    loop = CatalogSyncLoop(cfg)
    with patch.object(loop, "sync_once", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            loop.prepare_startup_snapshot()
    assert read_active_snapshot_path(cfg) is None


def test_prepare_startup_snapshot_sync_disabled_uses_local_contracts(tmp_path: Path) -> None:
    contracts_dir = tmp_path / "contracts"
    artifacts_dir = tmp_path / "artifacts"
    contracts_dir.mkdir()
    artifacts_dir.mkdir()
    (contracts_dir / "1_leaf.json").write_text(
        '{"data":{"schema_version":"3","fields":{"native":[]}}}',
        encoding="utf-8",
    )
    cfg = _settings(
        tmp_path,
        CATALOG_SYNC_ENABLED=False,
        CONTRACTS_DIR=contracts_dir,
        ARTIFACTS_DIR=artifacts_dir,
    )
    loop = CatalogSyncLoop(cfg)
    loop.prepare_startup_snapshot()
    assert cfg.CONTRACTS_DIR == contracts_dir
