"""Offline enum/description compile for the metadata sidecar (Phase 6.8).

Fills the deferred ``field_metadata.yaml`` stubs — Italian ``description``, enum
``values``, and a ``topic`` id into ``artifacts/topics.yaml`` — via LLM API calls,
then consolidates the proposed topics into embedding clusters (offline, no LLM) so
``topics.yaml`` is a compact cluster dictionary. Mirrors ``scripts/build_artifacts.py``;
gated behind ``OPENROUTER_API_KEY`` like the integration tests.

    uv run python scripts/compile_metadata.py                 # dry-run diff (default)
    uv run python scripts/compile_metadata.py --write         # apply + write back
    uv run python scripts/compile_metadata.py --field seats_total --write
    uv run python scripts/compile_metadata.py --force --write  # recompile everything

Topic-clustering hyperparameters override config per-run:

    ... --cluster-threshold 0.62 --cluster-linkage average --write
    ... --no-cluster --write                                   # skip consolidation

``--dry-run`` (the default) prints what *would* change and writes nothing.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from bulk_insert.blocks.extraction.compile import (
    dump_topics_yaml,
    load_registry,
    load_topics,
    plan_compile,
)
from bulk_insert.blocks.extraction.ingest import dump_metadata_yaml
from bulk_insert.blocks.extraction.topics_cluster import consolidate_topics
from bulk_insert.config import settings


def _load_sidecar(path: Path) -> dict[str, dict[str, Any]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fields = payload.get("fields") or {}
    if not isinstance(fields, dict):
        raise ValueError(f"Malformed sidecar {path}: 'fields' is not a mapping.")
    return fields


def _print_diff(plan: Any) -> None:
    if not plan.compiled:
        print("No pending fields to compile (use --force to recompile).")
        return
    for name in plan.compiled:
        before = plan.before.get(name, {})
        after = plan.fields_meta[name]
        print(f"\n{name}:")
        print(f"  description: {before.get('description')!r} -> {after['description']!r}")
        print(f"  topic:       {before.get('topic')!r} -> {after['topic']!r}")
        b_enum = before.get("enum") or {}
        a_enum = after["enum"]
        print(f"  enum.status: {b_enum.get('status')!r} -> {a_enum['status']!r}")
        if a_enum["status"] == "compiled":
            print(f"  enum.values: {a_enum['values']}")
        print(f"  unsure:      {after['unsure']}")
    print(
        f"\n{len(plan.compiled)} field(s) compiled; "
        f"{len(plan.topics)} topic(s); global status -> {plan.enum_compile_status}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--field", type=str, default=None, help="Compile only this field.")
    parser.add_argument(
        "--force", action="store_true", help="Recompile already-compiled fields too."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Print the diff and write nothing (default).",
    )
    group.add_argument("--write", action="store_true", help="Apply the compile and write back.")

    cluster = parser.add_argument_group("topic clustering (overrides config per-run)")
    cluster.add_argument(
        "--cluster",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable/disable post-compile topic clustering (default: TOPIC_CLUSTER_ENABLED).",
    )
    cluster.add_argument(
        "--cluster-threshold",
        type=float,
        default=None,
        metavar="FLOAT",
        help="Cosine-similarity merge cutoff; higher = more, tighter clusters "
        "(default: TOPIC_CLUSTER_THRESHOLD).",
    )
    cluster.add_argument(
        "--cluster-linkage",
        choices=("average", "complete", "single"),
        default=None,
        help="Agglomerative linkage (default: TOPIC_CLUSTER_LINKAGE).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Apply per-run CLI overrides onto the settings singleton (only where provided).
    overrides: dict[str, object] = {}
    if args.cluster is not None:
        overrides["TOPIC_CLUSTER_ENABLED"] = args.cluster
    if args.cluster_threshold is not None:
        overrides["TOPIC_CLUSTER_THRESHOLD"] = args.cluster_threshold
    if args.cluster_linkage is not None:
        overrides["TOPIC_CLUSTER_LINKAGE"] = args.cluster_linkage
    cfg = settings.model_copy(update=overrides) if overrides else settings

    sidecar_path = Path(cfg.FIELD_METADATA_PATH)
    topics_path = Path(cfg.TOPICS_PATH)

    fields_meta = _load_sidecar(sidecar_path)
    registry = load_registry(cfg)
    topics = load_topics(cfg)

    plan = plan_compile(
        fields_meta,
        registry,
        topics,
        cfg=cfg,
        only_field=args.field,
        force=args.force,
    )

    # Consolidate near-duplicate proposed topics into embedding clusters (offline),
    # so topics.yaml is a compact cluster dictionary and every field's topic id is
    # remapped to its cluster id.
    if cfg.TOPIC_CLUSTER_ENABLED:
        new_fields, new_topics = consolidate_topics(plan.fields_meta, plan.topics, cfg)
        plan = replace(plan, fields_meta=new_fields, topics=new_topics)

    _print_diff(plan)

    if not args.write:
        print("\n(dry-run — no files written; pass --write to apply)")
        return

    sidecar_path.write_text(
        dump_metadata_yaml(plan.fields_meta, enum_compile_status=plan.enum_compile_status),
        encoding="utf-8",
    )
    topics_path.write_text(dump_topics_yaml(plan.topics), encoding="utf-8")
    print(f"\nwrote {sidecar_path}")
    print(f"wrote {topics_path}")


if __name__ == "__main__":
    main()
