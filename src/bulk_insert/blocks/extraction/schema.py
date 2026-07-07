"""Runtime schema generation (Block 3 v2, Stage 2, §4).

Builds the per-product Pydantic extraction model from the **extracted**
(non-derived) field nodes:

* Each ``needs_evidence`` field gets a paired ``{name}_quote: Optional[str]``.
* Each ``compiled`` enum becomes a closed ``Literal`` vocabulary that always
  includes the :data:`NOT_PRESENT` abstention sentinel.
* A generic ``@model_validator(mode="after")`` rejects any non-null, non-abstained
  field lacking its verbatim quote — this drives Instructor's bounded retry.
* Derived fields and non-LLM-derivable fields (excluded types / non-text
  ``native_location``) are excluded from the extraction schema entirely.

Generated models are cached by a stable **signature** so the ≤ 97 products share
at most that many compiled models.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model, model_validator

from bulk_insert.blocks.extraction.tree import FieldNode, items_field_name
from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings

logger = logging.getLogger(__name__)

__all__: list[str] = [
    "NOT_PRESENT",
    "OUT_OF_VOCAB",
    "AggregateItem",
    "NativeQuoteModel",
    "build_extraction_model",
    "is_extractable",
    "quote_field_name",
    "sanitize_model_name",
]

# Abstention sentinel injected into every compiled enum and accepted by every
# field: the model returns it (instead of guessing) when a value is absent.
NOT_PRESENT = "NOT_PRESENT"

# Long-tail escape hatch (§9): the model emits this on a compiled enum when the
# field is clearly present but no listed value fits. Unlike NOT_PRESENT it is NOT
# validator-exempt, so it *requires* a verbatim quote; a scoped judge (Stage 4c)
# then resolves it to a real value. Kept in sync with ``cfg.OOV_TOKEN``.
OUT_OF_VOCAB = "OUT_OF_VOCAB"


class AggregateItem(BaseModel):
    """One transcribed item in a count-aggregate companion list (§10)."""

    value: str = Field(description="The item as stated in the document.")
    quote: str = Field(description="Verbatim source text for this item.")


def quote_field_name(name: str) -> str:
    """The paired evidence-quote field name for a value field."""
    return f"{name}_quote"


class NativeQuoteModel(BaseModel):
    """Base for generated extraction models: enforces the evidence invariant.

    For every value field ``f`` that has a sibling ``f_quote``, a non-null,
    non-``NOT_PRESENT`` value **must** carry a non-empty verbatim quote.  A
    violation raises ``ValueError`` → Instructor re-prompts (bounded retry).
    """

    @model_validator(mode="after")
    def _require_quotes(self) -> NativeQuoteModel:
        missing: list[str] = []
        for name in type(self).model_fields:
            quote_name = quote_field_name(name)
            if quote_name not in type(self).model_fields:
                continue  # `name` is itself a quote field or needs no evidence
            value = getattr(self, name)
            if value is None or value == NOT_PRESENT:
                continue
            quote = getattr(self, quote_name)
            if not isinstance(quote, str) or not quote.strip():
                missing.append(name)
        if missing:
            raise ValueError(
                "Non-null fields missing a verbatim source quote: "
                + ", ".join(sorted(missing))
                + f". Provide the exact supporting text in each '{{field}}_quote', "
                f"or set the field to '{NOT_PRESENT}' if it is absent."
            )
        return self


# ---------------------------------------------------------------------------
# Field inclusion + annotation
# ---------------------------------------------------------------------------

_SCALAR_ANNOTATION: dict[str, Any] = {
    "text": str,
    "iso_alpha2": str,
    "date": str,
    "integer": int,
    "year": int,
    "decimal": float,
    "currency": float,
    "percentage": float,
    "boolean": str,
    "select": str,
}


def is_extractable(node: FieldNode, excluded_types: tuple[str, ...]) -> bool:
    """Whether *node* belongs in the LLM extraction schema.

    Excludes derived fields, excluded field types (``foreign_id``,
    ``decimal_pair``), and non-text ``native_location`` fields (geo ids /
    coordinates) — matching the Phase-4 exclusion rules.
    """
    if node.is_derived:
        return False
    if node.field_type in excluded_types:
        return False
    if node.source == "native_location" and node.field_type != "text":
        return False
    return True


def _annotation_for(node: FieldNode, oov_enabled: bool) -> Any:
    """Map a field node to its (non-optional) extraction annotation."""
    if node.enum:
        # Compiled closed vocabulary → Literal incl. the abstention sentinel and
        # (when enabled) the OUT_OF_VOCAB long-tail escape hatch.
        sentinels = [NOT_PRESENT, OUT_OF_VOCAB] if oov_enabled else [NOT_PRESENT]
        values = tuple(dict.fromkeys([*node.enum, *sentinels]))
        return Literal[values]
    return _SCALAR_ANNOTATION.get(node.field_type, str)


def _has_companion(node: FieldNode) -> bool:
    """Whether *node* contributes a synthesized ``__items`` companion (§10)."""
    return node.aggregate is not None


def _included_nodes(nodes: list[FieldNode], excluded_types: tuple[str, ...]) -> list[FieldNode]:
    """Nodes contributing a field to the extraction model.

    An ordinary extractable field contributes its scalar; a count-aggregate node
    (derived, so not itself extractable) contributes only its ``__items`` companion.
    """
    return [n for n in nodes if is_extractable(n, excluded_types) or _has_companion(n)]


def _signature(nodes: list[FieldNode], oov_enabled: bool) -> tuple[Any, ...]:
    """Stable cache key capturing everything that shapes the generated model."""
    return (oov_enabled,) + tuple(
        (
            n.name,
            n.field_type,
            tuple(n.enum) if n.enum else None,
            n.needs_evidence,
            n.prompt_context,
            n.aggregate.item_hint if n.aggregate else None,
        )
        for n in nodes
    )


# Manual cache (signatures are lists of unhashable-friendly tuples, but building
# the model is the cost we memoise; keyed by the signature tuple).
_MODEL_CACHE: dict[tuple[Any, ...], type[BaseModel]] = {}

# The model's ``__name__`` becomes the Instructor tool/function name, which some
# OpenRouter upstreams (e.g. Azure) constrain to ``^[a-zA-Z0-9_.-]+$``. Topic-derived
# names carry spaces and accented Italian characters, so sanitise before create_model.
_INVALID_MODEL_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_.-]+")


def sanitize_model_name(name: str) -> str:
    """Coerce *name* into a valid LLM tool name (``^[a-zA-Z0-9_.-]+$``)."""
    cleaned = _INVALID_MODEL_NAME_CHARS.sub("_", name).strip("_")
    return cleaned or "ExtractedAsset"


def build_extraction_model(
    nodes: list[FieldNode],
    model_name: str = "ExtractedAsset",
    cfg: Settings | None = None,
) -> type[BaseModel]:
    """Build (and cache) the extraction model for the extractable *nodes*.

    Returns
    -------
    type[BaseModel]
        A Pydantic model subclassing :class:`NativeQuoteModel`, with an optional
        field per extractable node plus a paired ``{name}_quote`` for every
        ``needs_evidence`` field.
    """
    effective_cfg = cfg or _default_settings
    oov_enabled = effective_cfg.OOV_ENABLED
    model_name = sanitize_model_name(model_name)
    included = _included_nodes(nodes, effective_cfg.EXTRACTION_EXCLUDED_TYPES)

    signature = _signature(included, oov_enabled)
    cached = _MODEL_CACHE.get(signature)
    if cached is not None:
        return cached

    definitions: dict[str, Any] = {}
    for node in included:
        if _has_companion(node):
            # Count-aggregate (§10): synthesize a grounded item list instead of
            # the scalar (which is derived and filled by the `count` rule in code).
            hint = node.aggregate.item_hint if node.aggregate else ""
            desc = f"Transcribe each {hint or 'item'} as a value + verbatim quote (else null)."
            definitions[items_field_name(node.name)] = (
                list[AggregateItem] | None,
                Field(default=None, description=desc),
            )
            continue
        annotation = _annotation_for(node, oov_enabled)
        definitions[node.name] = (
            annotation | None,
            Field(default=None, description=node.prompt_context),
        )
        if node.needs_evidence:
            definitions[quote_field_name(node.name)] = (
                str | None,
                Field(
                    default=None,
                    description=f"Verbatim source text supporting '{node.name}' (else null).",
                ),
            )

    model: type[BaseModel] = create_model(
        model_name,
        __base__=NativeQuoteModel,
        **definitions,
    )
    logger.info(
        "Block 3 v2 — extraction model '%s': %d field(s), %d with evidence quotes.",
        model_name,
        len(included),
        sum(1 for n in included if n.needs_evidence and not _has_companion(n)),
    )
    _MODEL_CACHE[signature] = model
    return model
