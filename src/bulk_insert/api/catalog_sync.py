"""Periodic catalog sync loop (categories + per-L3 contracts)."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from bulk_insert.api.catalog_client import CatalogClient, CatalogClientError
from bulk_insert.api.catalog_store import (
    activate_snapshot_paths,
    build_category_snapshot_payload,
    build_staging_snapshot,
    publish_snapshot,
    read_active_snapshot_path,
    write_contract_snapshot_file,
    write_snapshot_manifest,
)
from bulk_insert.blocks.extraction.ingest import build_artifacts
from bulk_insert.blocks.extraction.metadata import clear_field_metadata_cache
from bulk_insert.blocks.extraction.tree import clear_field_tree_cache
from bulk_insert.config import Settings
from bulk_insert.models.categories import clear_categories_cache

__all__: list[str] = ["CatalogSyncLoop"]

logger = logging.getLogger(__name__)


def _iter_l3_categories(
    nodes: list[dict[str, Any]],
    *,
    lv1: str | None = None,
    lv2: str | None = None,
) -> list[dict[str, Any]]:
    leaves: list[dict[str, Any]] = []
    for node in nodes:
        level = int(node["level"])
        code = str(node["code"])
        children = list(node.get("children") or [])

        if level == 1:
            leaves.extend(_iter_l3_categories(children, lv1=code, lv2=lv2))
            continue
        if level == 2:
            leaves.extend(_iter_l3_categories(children, lv1=lv1, lv2=code))
            continue
        if level != 3:
            continue

        if lv1 is None or lv2 is None:
            raise ValueError(f"Invalid category ancestry for L3 code '{code}'.")
        leaves.append(
            {
                "id": int(node["id"]),
                "code": code,
                "name": str(node["name"]),
                "lv1": lv1,
                "lv2": lv2,
                "lv3": code,
            }
        )
    return leaves


def _configured_local_catalog_usable(cfg: Settings) -> bool:
    """True when CONTRACTS_DIR looks like a usable local/dev catalog."""
    contracts_dir = cfg.CONTRACTS_DIR
    if not contracts_dir.is_dir():
        return False
    return any(contracts_dir.glob("*.json"))


class CatalogSyncLoop:
    """Background full-sync loop; creates ephemeral cache at boot then periodically."""

    def __init__(self, cfg: Settings) -> None:
        self._cfg = cfg
        self._client = CatalogClient(cfg)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._sync_lock = threading.Lock()

    def prepare_startup_snapshot(self) -> None:
        """Create or attach a usable active snapshot before readiness is raised.

        Production containers ship without catalog cache. With sync enabled and
        startup blocking (defaults), the first Connect full-sync must succeed.
        There is no baked contracts/categories fallback in the image.
        """
        if not self._cfg.CATALOG_SYNC_ENABLED:
            active = read_active_snapshot_path(self._cfg)
            if active is not None:
                activate_snapshot_paths(self._cfg, active)
                logger.info("Catalog sync disabled; using existing active snapshot.")
                return
            if _configured_local_catalog_usable(self._cfg):
                logger.info(
                    "Catalog sync disabled; using configured CONTRACTS_DIR/ARTIFACTS_DIR."
                )
                return
            raise RuntimeError(
                "Catalog sync disabled and no usable catalog cache found "
                "(no active snapshot and no local contracts)."
            )

        if not self._cfg.CATALOG_SYNC_STARTUP_BLOCKING:
            try:
                if self.sync_once(reason="startup"):
                    return
            except Exception:
                logger.warning(
                    "Non-blocking startup catalog sync failed.",
                    exc_info=True,
                )
            active = read_active_snapshot_path(self._cfg)
            if active is not None:
                activate_snapshot_paths(self._cfg, active)
                logger.info(
                    "Catalog sync startup blocking disabled; continuing with active snapshot."
                )
                return
            if _configured_local_catalog_usable(self._cfg):
                logger.info(
                    "Catalog sync startup blocking disabled; using configured local catalog."
                )
                return
            raise RuntimeError(
                "No catalog snapshot available after non-blocking startup sync."
            )

        try:
            if self.sync_once(reason="startup"):
                return
            raise RuntimeError("startup sync returned unsuccessful status")
        except Exception:
            logger.error("Catalog startup sync failed; refusing to start without cache.")
            raise

    def start(self) -> None:
        if not self._cfg.CATALOG_SYNC_ENABLED:
            logger.info("Catalog sync loop disabled (CATALOG_SYNC_ENABLED=false).")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="catalog-sync-loop", daemon=True)
        self._thread.start()
        logger.info(
            "Catalog sync loop started (interval=%ss).",
            self._cfg.CATALOG_SYNC_INTERVAL_SECONDS,
            extra={"interval_seconds": self._cfg.CATALOG_SYNC_INTERVAL_SECONDS},
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Catalog sync loop stopped.")

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self._cfg.CATALOG_SYNC_INTERVAL_SECONDS):
            try:
                self.sync_once(reason="periodic")
            except Exception:
                logger.error("Periodic catalog sync crashed.", exc_info=True)

    def sync_once(self, *, reason: str) -> bool:
        """Perform one full catalog sync and atomically publish the snapshot."""
        with self._sync_lock:
            started = time.perf_counter()
            sync_id = f"sync-{int(time.time())}"
            logger.info("Catalog sync started (%s).", reason, extra={"reason": reason, "sync_id": sync_id})

            categories_tree = self._client.fetch_categories_tree()
            leaves = _iter_l3_categories(categories_tree)
            if not leaves:
                raise CatalogClientError("No level-3 categories returned by Connect.")

            staging = build_staging_snapshot(self._cfg, sync_id)
            category_payload = build_category_snapshot_payload(categories_tree, source="connect")
            categories_path = staging["snapshot_dir"] / "categories.json"
            categories_path.write_text(
                json.dumps(category_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            downloaded = 0
            for leaf in leaves:
                contract = self._client.fetch_contract(int(leaf["id"]))
                schema_version = str(contract.get("schema_version", ""))
                if schema_version != "3":
                    raise CatalogClientError(
                        f"Contract schema_version mismatch for category {leaf['id']}: {schema_version!r}"
                    )
                fields = contract.get("fields")
                if not isinstance(fields, dict) or not isinstance(fields.get("native"), list):
                    raise CatalogClientError(
                        f"Invalid contract fields for category {leaf['id']}: missing fields.native"
                    )
                contract["_bulk_insert_codes"] = {
                    "lv1": str(leaf["lv1"]),
                    "lv2": str(leaf["lv2"]),
                    "lv3": str(leaf["lv3"]),
                }
                write_contract_snapshot_file(
                    staging["contracts_dir"],
                    category_id=int(leaf["id"]),
                    category_code=str(leaf["code"]),
                    contract_data=contract,
                )
                downloaded += 1

            build_artifacts(staging["contracts_dir"], staging["artifacts_dir"])
            write_snapshot_manifest(
                staging["snapshot_dir"],
                {
                    "snapshot_id": sync_id,
                    "reason": reason,
                    "source": "connect",
                    "categories_l3_count": len(leaves),
                    "contracts_downloaded": downloaded,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )

            active_dir = publish_snapshot(self._cfg, sync_id)
            activate_snapshot_paths(self._cfg, active_dir)
            clear_field_tree_cache()
            clear_field_metadata_cache()
            clear_categories_cache()
            logger.info(
                "Catalog sync completed (%s): %d contracts, snapshot=%s (%.0f ms).",
                reason,
                downloaded,
                active_dir,
                (time.perf_counter() - started) * 1000,
                extra={
                    "reason": reason,
                    "contracts_downloaded": downloaded,
                    "snapshot_dir": str(active_dir),
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            return True

    def refresh_contract_for_lv3(self, lv3_code: str) -> bool:
        """On-demand contract refresh for a single lv3 code."""
        categories_path = Path(self._cfg.CATEGORY_CATALOG_PATH)
        if not categories_path.exists():
            return False
        payload = json.loads(categories_path.read_text(encoding="utf-8"))
        indices = payload.get("indices") or {}
        path_by_lv3 = indices.get("path_by_lv3_code") or {}
        entry = path_by_lv3.get(lv3_code)
        if not isinstance(entry, dict):
            return False
        category_id = entry.get("lv3_id")
        if not isinstance(category_id, int):
            return False

        with self._sync_lock:
            contract = self._client.fetch_contract(category_id)
            schema_version = str(contract.get("schema_version", ""))
            if schema_version != "3":
                return False
            active_snapshot = read_active_snapshot_path(self._cfg)
            if active_snapshot is None:
                return False
            write_contract_snapshot_file(
                active_snapshot / "contracts",
                category_id=category_id,
                category_code=lv3_code,
                contract_data=contract,
            )
            return True
