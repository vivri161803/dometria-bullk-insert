"""Local snapshot store for category/contract catalog sync."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from bulk_insert.config import Settings

__all__: list[str] = [
    "CATEGORY_SNAPSHOT_SCHEMA_VERSION",
    "build_category_snapshot_payload",
    "build_staging_snapshot",
    "write_contract_snapshot_file",
    "write_snapshot_manifest",
    "read_active_snapshot_path",
    "activate_snapshot_paths",
    "publish_snapshot",
]


CATEGORY_SNAPSHOT_SCHEMA_VERSION = 1
_CURRENT_SNAPSHOT_FILE = "current_snapshot.json"


def _cache_root(cfg: Settings) -> Path:
    return cfg.CATALOG_CACHE_ROOT


def _snapshot_path(cfg: Settings, snapshot_id: str) -> Path:
    return _cache_root(cfg) / "snapshots" / snapshot_id


def _atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}-{int(time.time() * 1000)}")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _slug_safe_code(code: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in code)


def _walk_categories(
    nodes: list[dict[str, Any]],
    *,
    by_id: dict[str, dict[str, Any]],
    by_code: dict[str, dict[str, Any]],
    category_names: dict[str, str],
    category_ancestry: dict[str, dict[str, Any]],
    category_tree: dict[str, dict[str, list[str]]],
    path_by_lv3_code: dict[str, dict[str, Any]],
    parent: dict[str, Any] | None = None,
    roots_by_id: dict[str, dict[str, Any]],
) -> None:
    for node in nodes:
        category_id = int(node["id"])
        category_code = str(node["code"])
        category_name = str(node["name"])
        level = int(node["level"])
        children = list(node.get("children") or [])

        parent_id = int(parent["id"]) if parent is not None else None
        parent_code = str(parent["code"]) if parent is not None else None

        by_id[str(category_id)] = {
            "id": category_id,
            "code": category_code,
            "name": category_name,
            "level": level,
            "parent_id": parent_id,
            "parent_code": parent_code,
        }
        by_code[category_code] = {
            "id": category_id,
            "code": category_code,
            "name": category_name,
            "level": level,
            "parent_id": parent_id,
            "parent_code": parent_code,
        }
        category_names[category_code] = category_name
        category_ancestry[category_code] = {
            "parent": parent_code,
            "level": level,
            "id": category_id,
        }
        if level == 1:
            roots_by_id[str(category_id)] = node

        if level == 3:
            if parent is None:
                continue
            lv2_code = str(parent["code"])
            lv1_code = str(parent["parent"]["code"])
            category_tree.setdefault(lv1_code, {}).setdefault(lv2_code, []).append(category_code)
            path_by_lv3_code[category_code] = {
                "lv1": lv1_code,
                "lv2": lv2_code,
                "lv3": category_code,
                "lv3_id": category_id,
            }

        if children:
            for child in children:
                child_parent = {
                    "id": category_id,
                    "code": category_code,
                    "parent": parent,
                }
                _walk_categories(
                    [child],
                    by_id=by_id,
                    by_code=by_code,
                    category_names=category_names,
                    category_ancestry=category_ancestry,
                    category_tree=category_tree,
                    path_by_lv3_code=path_by_lv3_code,
                    parent=child_parent,
                    roots_by_id=roots_by_id,
                )


def build_category_snapshot_payload(
    categories_tree: list[dict[str, Any]],
    *,
    source: str,
) -> dict[str, Any]:
    by_id: dict[str, dict[str, Any]] = {}
    by_code: dict[str, dict[str, Any]] = {}
    category_names: dict[str, str] = {}
    category_ancestry: dict[str, dict[str, Any]] = {}
    category_tree: dict[str, dict[str, list[str]]] = {}
    path_by_lv3_code: dict[str, dict[str, Any]] = {}
    roots_by_id: dict[str, dict[str, Any]] = {}
    _walk_categories(
        categories_tree,
        by_id=by_id,
        by_code=by_code,
        category_names=category_names,
        category_ancestry=category_ancestry,
        category_tree=category_tree,
        path_by_lv3_code=path_by_lv3_code,
        parent=None,
        roots_by_id=roots_by_id,
    )
    return {
        "schema_version": CATEGORY_SNAPSHOT_SCHEMA_VERSION,
        "source": source,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tree": categories_tree,
        "indices": {
            "by_id": by_id,
            "by_code": by_code,
            "roots": sorted(roots_by_id.keys(), key=int),
            "lv3_ids": sorted(
                (entry["lv3_id"] for entry in path_by_lv3_code.values()),
                key=int,
            ),
            "path_by_lv3_code": path_by_lv3_code,
            "category_names": category_names,
            "category_tree": category_tree,
            "category_ancestry": category_ancestry,
        },
    }


def build_staging_snapshot(cfg: Settings, snapshot_id: str) -> dict[str, Path]:
    base = _snapshot_path(cfg, snapshot_id)
    if base.exists():
        shutil.rmtree(base)
    contracts_dir = base / "contracts"
    artifacts_dir = base / "artifacts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    return {
        "snapshot_dir": base,
        "contracts_dir": contracts_dir,
        "artifacts_dir": artifacts_dir,
    }


def write_contract_snapshot_file(
    contracts_dir: Path,
    *,
    category_id: int,
    category_code: str,
    contract_data: dict[str, Any],
) -> Path:
    filename = f"{category_id}_{_slug_safe_code(category_code)}.json"
    path = contracts_dir / filename
    payload = {"data": contract_data}
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return path


def write_snapshot_manifest(snapshot_dir: Path, manifest: dict[str, Any]) -> Path:
    path = snapshot_dir / "manifest.json"
    _atomic_write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return path


def read_active_snapshot_path(cfg: Settings) -> Path | None:
    pointer = _cache_root(cfg) / _CURRENT_SNAPSHOT_FILE
    if not pointer.exists():
        return None
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    snapshot_id = str(payload.get("snapshot_id", "")).strip()
    if not snapshot_id:
        return None
    snapshot_dir = _snapshot_path(cfg, snapshot_id)
    if not snapshot_dir.exists():
        return None
    return snapshot_dir


def activate_snapshot_paths(cfg: Settings, snapshot_dir: Path) -> None:
    cfg.CONTRACTS_DIR = snapshot_dir / "contracts"
    cfg.ARTIFACTS_DIR = snapshot_dir / "artifacts"
    cfg.FIELD_METADATA_PATH = cfg.ARTIFACTS_DIR / "field_metadata.yaml"
    cfg.TOPICS_PATH = cfg.ARTIFACTS_DIR / "topics.yaml"
    cfg.CATEGORY_CATALOG_PATH = snapshot_dir / "categories.json"


def publish_snapshot(cfg: Settings, snapshot_id: str) -> Path:
    snapshot_dir = _snapshot_path(cfg, snapshot_id)
    if not snapshot_dir.exists():
        raise FileNotFoundError(f"Snapshot directory not found: {snapshot_dir}")
    pointer = _cache_root(cfg) / _CURRENT_SNAPSHOT_FILE
    payload = {
        "snapshot_id": snapshot_id,
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _atomic_write_text(pointer, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return snapshot_dir
