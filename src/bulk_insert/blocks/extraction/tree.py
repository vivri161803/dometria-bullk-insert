"""Field-tree data structures and Stage-1 selection (Block 3 v2).

The offline ingest (:mod:`bulk_insert.blocks.extraction.ingest`) builds a master
**field tree** — the union of every contract's fields, attached to the
``lv1 → lv2 → lv3`` category leaves.  At runtime, Stage 1 *selects* the field set
for the active :class:`~bulk_insert.models.contract.Contract` by traversing that
tree over the contract's ``(lv1, lv2, lv3)`` path.

Two representations live here:

* :class:`TreeField` — the **structural** field record persisted in
  ``field_tree.json`` (name, type, label, required flag, source, constraints).
* :class:`FieldNode` — the **enriched** runtime node (§3.2 of the spec):
  structural bits plus the metadata-sidecar overlay (topic, priority, mandatory,
  enum, description, derivation).  Produced by
  :mod:`bulk_insert.blocks.extraction.metadata`.

Selection is cached by a stable **signature** (the sorted field names), so the
≤ 97 distinct products share at most 97 cached selections.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings
from bulk_insert.models.contract import Contract

__all__: list[str] = [
    "DerivationKind",
    "Derivation",
    "TreeField",
    "FieldNode",
    "load_field_tree",
    "select_tree_fields",
    "signature_for",
]


class DerivationKind(StrEnum):
    """How a field's value is produced."""

    NONE = "none"  # extracted from text
    COMPUTED = "computed"  # deterministic rule, done in Python
    JUDGED = "judged"  # reasoning over primitives, 2nd LLM run


@dataclass(frozen=True)
class Derivation:
    """Provenance rule for a derived field (§3.2)."""

    source_fields: tuple[str, ...]  # field keys this value derives from
    rule: str  # code ref (computed) or instruction (judged)
    kind: DerivationKind


@dataclass(frozen=True)
class TreeField:
    """Structural field record as stored in ``field_tree.json``.

    Everything here is inferable from the contract JSON; the metadata sidecar
    (topic/priority/enum/description/derivation) is layered on top at runtime to
    yield a :class:`FieldNode`.
    """

    name: str
    label: str
    field_type: str
    is_required: bool
    source: str
    constraints: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TreeField:
        return cls(
            name=str(raw["name"]),
            label=str(raw.get("label") or raw["name"]),
            field_type=str(raw["field_type"]),
            is_required=bool(raw["is_required"]),
            source=str(raw.get("source") or "standard"),
            constraints=raw.get("constraints"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "field_type": self.field_type,
            "is_required": self.is_required,
            "source": self.source,
            "constraints": self.constraints,
        }


@dataclass
class FieldNode:
    """Enriched runtime field node (§3.2): structural bits + metadata overlay."""

    name: str
    lv_path: tuple[str, str, str]
    field_type: str
    label: str
    source: str
    constraints: dict[str, Any] | None
    # Metadata-sidecar overlay:
    description: str | None
    topic: str
    mandatory: bool
    priority: int
    enum: list[str] | None = None  # closed vocab; NOT_PRESENT auto-added downstream
    needs_evidence: bool = True
    derivation: Derivation | None = None

    @property
    def is_derived(self) -> bool:
        return self.derivation is not None and self.derivation.kind is not DerivationKind.NONE

    @property
    def prompt_context(self) -> str:
        """Extraction-prompt context: the compiled description, else the label."""
        return self.description or self.label


# ---------------------------------------------------------------------------
# Tree loading + Stage-1 selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FieldTree:
    """Parsed ``field_tree.json`` — nested lv1 → lv2 → lv3 → [TreeField]."""

    schema_version: int
    tree: dict[str, dict[str, dict[str, tuple[TreeField, ...]]]] = field(default_factory=dict)


@lru_cache(maxsize=8)
def _load_field_tree_cached(path_str: str) -> _FieldTree:
    path = Path(path_str)
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_tree: dict[str, Any] = payload["tree"]
    tree: dict[str, dict[str, dict[str, tuple[TreeField, ...]]]] = {}
    for lv1, lv2_map in raw_tree.items():
        tree[lv1] = {}
        for lv2, lv3_map in lv2_map.items():
            tree[lv1][lv2] = {}
            for lv3, fields in lv3_map.items():
                tree[lv1][lv2][lv3] = tuple(TreeField.from_dict(f) for f in fields)
    return _FieldTree(schema_version=int(payload.get("schema_version", 1)), tree=tree)


def load_field_tree(cfg: Settings | None = None) -> _FieldTree:
    """Load (and cache) the field tree artifact for *cfg*."""
    effective_cfg = cfg or _default_settings
    path = effective_cfg.ARTIFACTS_DIR / "field_tree.json"
    return _load_field_tree_cached(str(path))


def signature_for(fields: list[TreeField]) -> tuple[str, ...]:
    """Stable selection signature: the sorted tuple of field names."""
    return tuple(sorted(f.name for f in fields))


def select_tree_fields(contract: Contract, cfg: Settings | None = None) -> list[TreeField]:
    """Stage 1 — gather the product's structural field nodes for *contract*.

    Traverses the field tree over the contract's ``(lv1, lv2, lv3)`` path.
    Irrelevant fields never enter the pipeline (static pruning).

    Raises
    ------
    KeyError
        If the contract's category path is absent from the tree.
    """
    field_tree = load_field_tree(cfg)
    try:
        leaf = field_tree.tree[contract.lv1][contract.lv2][contract.lv3]
    except KeyError as exc:
        raise KeyError(
            f"Category path ({contract.lv1!r}, {contract.lv2!r}, {contract.lv3!r}) "
            "not found in the field tree."
        ) from exc
    return list(leaf)
