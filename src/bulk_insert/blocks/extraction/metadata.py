"""Metadata sidecar loading + enrichment (Block 3 v2, §3.3).

Loads the editable ``field_metadata.yaml`` and overlays it onto the structural
:class:`~bulk_insert.blocks.extraction.tree.TreeField` records to produce enriched
:class:`~bulk_insert.blocks.extraction.tree.FieldNode` s.

Runtime rules enforced here (§3.3):

* **Enum injection** — a field gets a closed vocabulary **only** when its
  ``enum.status == "compiled"``; ``pending`` / ``not_applicable`` fields are
  treated as free-text (typed by ``field_type`` downstream).  ``NOT_PRESENT`` is
  appended at *schema-gen* time, not here.
* **Description injection** — a ``null``/stub description falls back to the
  contract ``label`` (handled by :attr:`FieldNode.prompt_context`).
* **Derivation** — a ``null`` derivation means the field is extracted; otherwise a
  :class:`~bulk_insert.blocks.extraction.tree.Derivation` is built.

Tests may inject a :class:`FieldMetadata` built from an in-memory mapping (mock
enums/descriptions) via :func:`FieldMetadata.from_mapping`, standing in for the
deferred offline enum-compile step.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from bulk_insert.blocks.extraction.tree import (
    Aggregate,
    Derivation,
    DerivationKind,
    FieldNode,
    TreeField,
    items_field_name,
)
from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings

logger = logging.getLogger(__name__)

__all__: list[str] = [
    "FieldMetadata",
    "load_field_metadata",
    "enrich",
    "clear_field_metadata_cache",
]

# Source-based fallback topics, mirroring the ingest seed — only used when the
# sidecar is missing an entry for a field (defensive; keeps the partition clean).
_FALLBACK_TOPIC_BY_SOURCE: dict[str, str] = {
    "standard": "identity",
    "native_location": "location",
    "custom_fields": "attributes",
}


def _parse_derivation(raw: Any) -> Derivation | None:
    """Build a :class:`Derivation` from a sidecar mapping, or ``None``."""
    if not isinstance(raw, dict):
        return None
    kind = DerivationKind(str(raw.get("kind", "none")))
    if kind is DerivationKind.NONE:
        return None
    source_fields = tuple(str(s) for s in raw.get("source_fields", []))
    return Derivation(source_fields=source_fields, rule=str(raw.get("rule", "")), kind=kind)


def _parse_aggregate(raw: Any) -> Aggregate | None:
    """Build an :class:`Aggregate` from a sidecar mapping, or ``None`` (§10)."""
    if not isinstance(raw, dict):
        return None
    rule = str(raw.get("rule", "")).strip()
    if not rule:
        return None
    return Aggregate(
        rule=rule,
        item_hint=str(raw.get("item_hint", "")),
        kind=str(raw.get("kind", "computed")),
    )


class FieldMetadata:
    """The parsed metadata sidecar, keyed by field name.

    Optionally carries a topics dictionary (``id -> name``, from ``topics.yaml``)
    so a compiled integer ``topic`` id resolves back to its name (§6.8). String
    topics and unmapped ids fall back to the source-based default.
    """

    def __init__(
        self,
        fields: dict[str, dict[str, Any]],
        topics_by_id: dict[int, str] | None = None,
    ) -> None:
        self._fields = fields
        self._topics_by_id = topics_by_id or {}

    @classmethod
    def from_mapping(
        cls,
        fields: dict[str, dict[str, Any]],
        topics_by_id: dict[int, str] | None = None,
    ) -> FieldMetadata:
        """Build from an in-memory mapping (test injection of mock metadata)."""
        return cls(dict(fields), dict(topics_by_id) if topics_by_id else None)

    @classmethod
    def from_yaml(cls, path: Path, topics_path: Path | None = None) -> FieldMetadata:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        fields = payload.get("fields") or {}
        if not isinstance(fields, dict):
            raise ValueError(f"Malformed metadata sidecar {path}: 'fields' is not a mapping.")
        topics_by_id = _load_topics_by_id(topics_path) if topics_path is not None else {}
        return cls(fields, topics_by_id)

    def get(self, name: str) -> dict[str, Any]:
        return self._fields.get(name, {})

    def _resolve_topic(self, raw: Any, source: str) -> str:
        """Resolve a sidecar ``topic`` value → a topic name.

        An integer id maps through the topics dictionary; a non-empty string is
        used verbatim; anything else (unmapped id, missing) falls back to the
        source-based default, keeping the partition clean.
        """
        if isinstance(raw, bool):  # guard: bool is an int subclass
            pass
        elif isinstance(raw, int):
            name = self._topics_by_id.get(raw)
            if name:
                return name
        elif isinstance(raw, str) and raw:
            return raw
        return _FALLBACK_TOPIC_BY_SOURCE.get(source, source)

    def resolve(self, field: TreeField, lv_path: tuple[str, str, str]) -> FieldNode:
        """Overlay this sidecar onto *field* → an enriched :class:`FieldNode`."""
        meta = self.get(field.name)

        description = meta.get("description")
        description = str(description) if isinstance(description, str) and description else None

        topic = self._resolve_topic(meta.get("topic"), field.source)

        mandatory = bool(meta.get("mandatory", field.is_required))
        priority = int(meta.get("priority", 5 if mandatory else 3))
        needs_evidence = bool(meta.get("needs_evidence", True))

        enum_values: list[str] | None = None
        enum_block = meta.get("enum")
        if isinstance(enum_block, dict) and enum_block.get("status") == "compiled":
            values = enum_block.get("values") or []
            if isinstance(values, list) and values:
                enum_values = [str(v) for v in values]

        derivation = _parse_derivation(meta.get("derivation"))
        aggregate = _parse_aggregate(meta.get("aggregate"))
        # A count-aggregate marker (§10) makes the scalar a `computed` count over
        # its synthesized `{name}__items` companion. Synthesize that derivation
        # here when the sidecar didn't spell it out, so a single `aggregate:` key
        # is enough to opt in (the scalar then drops out of extraction).
        if aggregate is not None and derivation is None:
            kind_enum = (
                DerivationKind.JUDGED if aggregate.kind == "judged" else DerivationKind.COMPUTED
            )
            derivation = Derivation(
                source_fields=(items_field_name(field.name),),
                rule=aggregate.rule,
                kind=kind_enum,
            )

        default = meta.get("default")

        return FieldNode(
            name=field.name,
            lv_path=lv_path,
            field_type=field.field_type,
            label=field.label,
            source=field.source,
            constraints=field.constraints,
            description=description,
            topic=topic,
            mandatory=mandatory,
            priority=priority,
            enum=enum_values,
            needs_evidence=needs_evidence,
            derivation=derivation,
            aggregate=aggregate,
            default=default,
        )


def _load_topics_by_id(path: Path | None) -> dict[int, str]:
    """Load ``topics.yaml`` → ``{id: name}``; empty if the file is absent."""
    if path is None or not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    topics = payload.get("topics") or {}
    if not isinstance(topics, dict):
        raise ValueError(f"Malformed topics file {path}: 'topics' is not a mapping.")
    return {int(tid): str(name) for name, tid in topics.items()}


@lru_cache(maxsize=8)
def _load_field_metadata_cached(path_str: str, topics_path_str: str) -> FieldMetadata:
    path = Path(path_str)
    topics_path = Path(topics_path_str) if topics_path_str else None
    if not path.exists():
        logger.warning("Metadata sidecar not found at %s; using structural defaults.", path)
        return FieldMetadata({}, _load_topics_by_id(topics_path))
    return FieldMetadata.from_yaml(path, topics_path)


def load_field_metadata(cfg: Settings | None = None) -> FieldMetadata:
    """Load (and cache) the metadata sidecar (and topics dictionary) for *cfg*."""
    effective_cfg = cfg or _default_settings
    return _load_field_metadata_cached(
        str(effective_cfg.FIELD_METADATA_PATH),
        str(effective_cfg.TOPICS_PATH),
    )


def clear_field_metadata_cache() -> None:
    """Invalidate cached metadata sidecar after catalog swaps."""
    _load_field_metadata_cached.cache_clear()


def enrich(
    fields: list[TreeField],
    lv_path: tuple[str, str, str],
    cfg: Settings | None = None,
    metadata: FieldMetadata | None = None,
) -> list[FieldNode]:
    """Overlay the sidecar onto structural *fields* → enriched field nodes."""
    resolver = metadata or load_field_metadata(cfg)
    return [resolver.resolve(f, lv_path) for f in fields]
