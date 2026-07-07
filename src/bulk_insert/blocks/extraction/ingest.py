"""Offline ingest — build the field tree, registry, manifests and metadata sidecar.

This is an **offline build step** (§3 of the tree-driven spec), run once over an
**arbitrary directory of N contracts** — never hardcoding 97.  It produces the
static artifacts the runtime consumes (baked into the container image alongside
``contracts/``):

* ``field_registry.json`` — the union of every ``(key, lv_path)`` field, with the
  products that contain it.
* ``field_tree.json`` — the ``lv1 → lv2 → lv3`` category tree with each product's
  structural fields attached to its leaf.
* ``product_manifests.json`` — per-product ``lv_path`` + field-key selection, i.e.
  exactly what Block 2's ``Contract`` reproduces at runtime.
* ``field_metadata.yaml`` — the editable sidecar (§3.3): inferred fields filled,
  LLM/human fields stubbed with ``# TODO:`` markers and correct ``status`` values.

The **round-trip gate** (:func:`validate_round_trip`) asserts every original
contract is exactly reconstructable by selecting its manifest paths against the
tree — the Phase-0 acceptance criterion.

Run via ``uv run python scripts/build_artifacts.py`` (thin wrapper around
:func:`build_artifacts`).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

from bulk_insert.models.categories import CATEGORY_ANCESTRY

logger = logging.getLogger(__name__)

__all__: list[str] = [
    "build_artifacts",
    "validate_round_trip",
    "dump_metadata_yaml",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = 1

# Closed-vocabulary field types (candidate enums); everything else is open/numeric.
_ENUM_CANDIDATE_TYPES: frozenset[str] = frozenset({"select", "boolean"})
# Default boolean vocabulary when a boolean field omits an explicit option list.
_DEFAULT_BOOLEAN_OPTIONS: tuple[str, ...] = ("si", "no", "non dichiarato")
# Seeded topic per contract field ``source`` — a stub default (one topic per
# field, guaranteeing a clean partition); humans refine it in the sidecar later.
_TOPIC_BY_SOURCE: dict[str, str] = {
    "standard": "identity",
    "native_location": "location",
    "custom_fields": "attributes",
}


def _lv_path_for(lv3: str) -> tuple[str, str, str]:
    """Resolve a leaf ``lv3`` code to its full ``(lv1, lv2, lv3)`` path."""
    lv2 = str(CATEGORY_ANCESTRY[lv3]["parent"])
    lv1 = str(CATEGORY_ANCESTRY[lv2]["parent"])
    return (lv1, lv2, lv3)


def _lv3_from_stem(stem: str) -> str:
    """Contract filename stem ``{id}_{lv3}`` → the ``lv3`` code."""
    return stem.split("_", 1)[1]


def _options_for(field: dict[str, Any]) -> list[str]:
    """Return a field's enum candidate values (contract options / boolean default)."""
    constraints = field.get("constraints")
    if isinstance(constraints, dict):
        options = constraints.get("options")
        if isinstance(options, list) and options:
            return [str(o) for o in options]
    if field.get("type") == "boolean":
        return list(_DEFAULT_BOOLEAN_OPTIONS)
    return []


# ---------------------------------------------------------------------------
# Parse + union
# ---------------------------------------------------------------------------


def _parse_contracts(
    contracts_dir: Path,
) -> tuple[
    dict[str, dict[str, dict[str, list[dict[str, Any]]]]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    """Parse every contract JSON → (tree, manifests, registry-by-name).

    Returns
    -------
    tree:
        ``lv1 → lv2 → lv3 → [structural field dict]``.
    manifests:
        ``product_id → {lv_path, field_keys}``.
    registry_by_name:
        ``field_name → aggregated registry entry`` (union/dedup across products).
    """
    tree: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    manifests: dict[str, dict[str, Any]] = {}
    registry_by_name: dict[str, dict[str, Any]] = {}

    paths = sorted(contracts_dir.glob("*.json"))
    if not paths:
        raise FileNotFoundError(f"No contract JSON files found in {contracts_dir}.")

    for path in paths:
        product_id = path.stem
        lv3 = _lv3_from_stem(product_id)
        lv_path = _lv_path_for(lv3)
        lv1, lv2, _ = lv_path

        data = json.loads(path.read_text(encoding="utf-8"))["data"]
        native = data["fields"]["native"]

        leaf: list[dict[str, Any]] = []
        field_keys: list[str] = []
        for raw in native:
            key = raw["key"]
            structural = {
                "name": key,
                "label": raw.get("label") or key,
                "field_type": raw["type"],
                "is_required": bool(raw.get("is_required", False)),
                "source": raw.get("source") or "standard",
                "constraints": raw.get("constraints"),
            }
            leaf.append(structural)
            field_keys.append(key)

            entry = registry_by_name.setdefault(
                key,
                {
                    "key": key,
                    "label": structural["label"],
                    "field_type": structural["field_type"],
                    "source": structural["source"],
                    "is_required": False,
                    "options": set(),
                    "products": [],
                },
            )
            entry["is_required"] = bool(entry["is_required"]) or structural["is_required"]
            entry["options"].update(_options_for(raw))
            entry["products"].append(product_id)

        tree.setdefault(lv1, {}).setdefault(lv2, {})[lv3] = leaf
        manifests[product_id] = {"lv_path": list(lv_path), "field_keys": field_keys}

    return tree, manifests, registry_by_name


# ---------------------------------------------------------------------------
# Metadata sidecar
# ---------------------------------------------------------------------------


def _seed_metadata(registry_by_name: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Seed the per-field metadata sidecar from the aggregated registry (§3.3)."""
    fields_meta: dict[str, dict[str, Any]] = {}
    for name in sorted(registry_by_name):
        entry = registry_by_name[name]
        field_type = entry["field_type"]
        mandatory = bool(entry["is_required"])
        options = sorted(entry["options"])

        if field_type in _ENUM_CANDIDATE_TYPES:
            # Closed-vocab candidate: values seeded from contract options, but the
            # status stays `pending` until the offline enum-compile step refines
            # and blesses them (runtime treats `pending` as free-text until then).
            enum_block = {"status": "pending", "values": options}
        else:
            enum_block = {"status": "not_applicable", "values": []}

        fields_meta[name] = {
            "description": None,  # TODO: llm-compile
            "topic": _TOPIC_BY_SOURCE.get(entry["source"], entry["source"]),
            "mandatory": mandatory,  # seeded from contract is_required
            "priority": 5 if mandatory else 3,  # TODO: human
            "needs_evidence": True,
            "enum": enum_block,
            "derivation": None,  # null=extracted; else {source_fields, rule, kind}
        }
    return fields_meta


_YAML_HEADER = """\
# field_metadata.yaml — human- and LLM-editable metadata sidecar (§3.3).
#
# Injected at runtime and fully modifiable thereafter (never baked into Python).
# The runtime reads enums, descriptions, topics, priorities and derivations here.
#
# Stub conventions:
#   * description: null            -> TODO: llm-compile (falls back to contract label)
#   * enum.status: pending         -> TODO: llm-compile values (runtime = free-text)
#   * enum.status: compiled        -> runtime builds a str-Enum (+ NOT_PRESENT)
#   * enum.status: not_applicable  -> open-vocab / numeric field, never enumed
#   * topic / priority             -> TODO: human — seeded defaults, refine as needed
"""


def dump_metadata_yaml(
    fields_meta: dict[str, dict[str, Any]],
    enum_compile_status: str = "pending",
) -> str:
    """Emit the sidecar YAML with a header, meta block and per-field TODO markers.

    State-driven so it round-trips both the seed (all stubs) and the post-compile
    sidecar: a field only carries a ``TODO: llm-compile`` marker while its
    ``description`` is still ``null`` / its enum ``status`` is still ``pending``.
    Compiled fields drop those markers; a per-field ``unsure`` line (added by the
    compile step, §6.8) is preserved verbatim by ``safe_dump``.
    """
    lines: list[str] = [_YAML_HEADER]
    meta_block = {
        "meta": {"schema_version": SCHEMA_VERSION, "enum_compile_status": enum_compile_status},
    }
    lines.append(yaml.safe_dump(meta_block, sort_keys=False, allow_unicode=True).rstrip())
    lines.append("")
    lines.append("fields:")
    for name, meta in fields_meta.items():
        todos: list[str] = ["TODO: human-confirm topic/priority"]
        if meta.get("description") is None:
            todos.append("TODO: llm-compile description")
        enum_block = meta.get("enum")
        if isinstance(enum_block, dict) and enum_block.get("status") == "pending":
            todos.append("TODO: llm-compile enum values")
        lines.append(f"  # {'; '.join(todos)}")
        block = yaml.safe_dump({name: meta}, sort_keys=False, allow_unicode=True)
        lines.extend(f"  {line}" for line in block.rstrip().splitlines())
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Build + round-trip validation
# ---------------------------------------------------------------------------


def _serialize_registry(registry_by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    for name in sorted(registry_by_name):
        entry = registry_by_name[name]
        fields.append(
            {
                "key": entry["key"],
                "label": entry["label"],
                "field_type": entry["field_type"],
                "source": entry["source"],
                "is_required": bool(entry["is_required"]),
                "options": sorted(entry["options"]),
                "products": sorted(set(entry["products"])),
            }
        )
    return {"schema_version": SCHEMA_VERSION, "fields": fields}


def validate_round_trip(
    tree: dict[str, dict[str, dict[str, list[dict[str, Any]]]]],
    manifests: dict[str, dict[str, Any]],
) -> None:
    """Assert every contract is exactly reconstructable from its manifest (§3.4).

    Selecting a product's ``field_keys`` against its ``lv_path`` leaf must return
    exactly the product's original field set.

    Raises
    ------
    ValueError
        On any reconstruction mismatch.
    """
    for product_id, manifest in manifests.items():
        lv1, lv2, lv3 = manifest["lv_path"]
        leaf = tree[lv1][lv2][lv3]
        tree_keys = {f["name"] for f in leaf}
        manifest_keys = set(manifest["field_keys"])
        if tree_keys != manifest_keys:
            missing = manifest_keys - tree_keys
            extra = tree_keys - manifest_keys
            raise ValueError(
                f"Round-trip failed for {product_id}: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )
        if len(manifest["field_keys"]) != len(leaf):
            raise ValueError(
                f"Round-trip failed for {product_id}: field count "
                f"{len(manifest['field_keys'])} != leaf {len(leaf)} (duplicate keys?)."
            )


def _assert_one_topic_per_field(fields_meta: dict[str, dict[str, Any]]) -> None:
    """Each field maps to exactly one topic (a mapping value cannot be multivalued)."""
    for name, meta in fields_meta.items():
        topic = meta["topic"]
        if not isinstance(topic, str) or not topic:
            raise ValueError(f"Field {name!r} has an invalid topic: {topic!r}.")


def build_artifacts(contracts_dir: Path, artifacts_dir: Path) -> dict[str, Path]:
    """Build all four artifacts from *contracts_dir* into *artifacts_dir*.

    Returns
    -------
    dict[str, Path]
        Mapping of artifact name → written path.
    """
    tree, manifests, registry_by_name = _parse_contracts(contracts_dir)

    # Phase-0 gates.
    validate_round_trip(tree, manifests)
    fields_meta = _seed_metadata(registry_by_name)
    _assert_one_topic_per_field(fields_meta)

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    registry_path = artifacts_dir / "field_registry.json"
    registry_path.write_text(
        json.dumps(_serialize_registry(registry_by_name), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    written["field_registry.json"] = registry_path

    tree_path = artifacts_dir / "field_tree.json"
    tree_path.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "tree": tree}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    written["field_tree.json"] = tree_path

    manifests_path = artifacts_dir / "product_manifests.json"
    manifests_path.write_text(
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "products": manifests},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    written["product_manifests.json"] = manifests_path

    metadata_path = artifacts_dir / "field_metadata.yaml"
    metadata_path.write_text(dump_metadata_yaml(fields_meta), encoding="utf-8")
    written["field_metadata.yaml"] = metadata_path

    logger.info(
        "Ingest complete: %d products, %d distinct fields → %s",
        len(manifests),
        len(fields_meta),
        artifacts_dir,
    )
    return written
