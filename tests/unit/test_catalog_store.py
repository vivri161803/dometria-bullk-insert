"""Unit tests for catalog snapshot store helpers."""

from __future__ import annotations

from pathlib import Path

from bulk_insert.api.catalog_store import (
    activate_snapshot_paths,
    build_category_snapshot_payload,
    build_staging_snapshot,
    publish_snapshot,
    read_active_snapshot_path,
    write_contract_snapshot_file,
    write_snapshot_manifest,
)
from bulk_insert.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        OPENROUTER_API_KEY="test",
        CATALOG_CACHE_ROOT=tmp_path / "cache",
    )


def test_build_category_snapshot_payload_builds_indices() -> None:
    tree = [
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

    payload = build_category_snapshot_payload(tree, source="connect")
    indices = payload["indices"]
    assert indices["by_code"]["villa"]["id"] == 3
    assert indices["category_tree"]["real-estate"]["residential"] == ["villa"]
    assert indices["path_by_lv3_code"]["villa"]["lv3_id"] == 3


def test_publish_snapshot_activates_paths(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    staging = build_staging_snapshot(cfg, "sync-test")
    write_contract_snapshot_file(
        staging["contracts_dir"],
        category_id=3,
        category_code="villa",
        contract_data={"schema_version": "3", "fields": {"native": []}},
    )
    write_snapshot_manifest(staging["snapshot_dir"], {"snapshot_id": "sync-test"})
    active = publish_snapshot(cfg, "sync-test")
    activate_snapshot_paths(cfg, active)

    assert read_active_snapshot_path(cfg) == active
    assert cfg.CONTRACTS_DIR == active / "contracts"
    assert cfg.CATEGORY_CATALOG_PATH == active / "categories.json"
    assert (active / "contracts" / "3_villa.json").exists()
