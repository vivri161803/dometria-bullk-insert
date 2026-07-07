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

from bulk_insert.blocks.extraction.tree import FieldNode
from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings

logger = logging.getLogger(__name__)

__all__: list[str] = [
    "NOT_PRESENT",
    "NativeQuoteModel",
    "build_extraction_model",
    "is_extractable",
    "quote_field_name",
    "sanitize_model_name",
]

# Abstention sentinel injected into every compiled enum and accepted by every
# field: the model returns it (instead of guessing) when a value is absent.
NOT_PRESENT = "NOT_PRESENT"


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


def _annotation_for(node: FieldNode) -> Any:
    """Map a field node to its (non-optional) extraction annotation."""
    if node.enum:
        # Compiled closed vocabulary → Literal incl. the abstention sentinel.
        values = tuple(dict.fromkeys([*node.enum, NOT_PRESENT]))
        return Literal[values]
    return _SCALAR_ANNOTATION.get(node.field_type, str)


def _extractable_nodes(nodes: list[FieldNode], excluded_types: tuple[str, ...]) -> list[FieldNode]:
    return [n for n in nodes if is_extractable(n, excluded_types)]


def _signature(nodes: list[FieldNode]) -> tuple[Any, ...]:
    """Stable cache key capturing everything that shapes the generated model."""
    return tuple(
        (
            n.name,
            n.field_type,
            tuple(n.enum) if n.enum else None,
            n.needs_evidence,
            n.prompt_context,
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
    model_name = sanitize_model_name(model_name)
    extractable = _extractable_nodes(nodes, effective_cfg.EXTRACTION_EXCLUDED_TYPES)

    signature = _signature(extractable)
    cached = _MODEL_CACHE.get(signature)
    if cached is not None:
        return cached

    definitions: dict[str, Any] = {}
    for node in extractable:
        annotation = _annotation_for(node)
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
        len(extractable),
        sum(1 for n in extractable if n.needs_evidence),
    )
    _MODEL_CACHE[signature] = model
    return model
