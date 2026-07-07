"""Partition + sequential, strategy-routed extraction (Block 3 v2, Stages 3–4).

Fields are partitioned into groups by ``topic`` (a clean partition guaranteed by
the one-topic-per-field invariant).  Groups are extracted **sequentially** — no
asyncio fan-out — consistent with the stateless-synchronous-container design.

Per-group strategy (hard rule, §4.4)::

    if group has any field where (mandatory AND priority >= HIGH_PRIORITY):
        SELF_CONSISTENCY   # N runs, vote; disagreement -> null + flag
    else:
        STANDARD           # single structured-output call

Both strategies use Instructor + native structured output + evidence spans +
bounded validation retry (the generated model's quote validator drives the
retry).  ``NOT_PRESENT`` is normalised to ``None`` in the returned record.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import instructor
from pydantic import BaseModel

from bulk_insert.blocks.extraction.client import build_instructor_client, document_text
from bulk_insert.blocks.extraction.schema import (
    NOT_PRESENT,
    build_extraction_model,
    is_extractable,
    quote_field_name,
)
from bulk_insert.blocks.extraction.tree import FieldNode
from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings
from bulk_insert.llm import sampling_params
from bulk_insert.models.chunk import Chunk

logger = logging.getLogger(__name__)

__all__: list[str] = [
    "ExtractionOutput",
    "partition_by_topic",
    "group_strategy",
    "extract_fields",
    "STANDARD",
    "SELF_CONSISTENCY",
]

STANDARD = "STANDARD"
SELF_CONSISTENCY = "SELF_CONSISTENCY"

_EXTRACTION_SYSTEM_PROMPT = (
    "You are an expert Italian real-estate and business document extractor.\n\n"
    "From the document content below, populate the structured fields. Rules:\n"
    "- Extract a value ONLY when it is explicitly supported by the document.\n"
    f"- If a field is absent or cannot be grounded, set it to '{NOT_PRESENT}' "
    "(for free-text/numeric fields, leave it null).\n"
    "- For every populated field, copy the exact verbatim supporting text into "
    "its paired '<field>_quote'. Do not paraphrase the quote.\n"
    "- For fields with a fixed set of allowed values, return one of those exact "
    "values.\n"
    "- Never invent identifiers, coordinates, or figures that are not present."
)


@dataclass
class ExtractionOutput:
    """The merged result of the extraction stage across all topic groups."""

    values: dict[str, Any] = field(default_factory=dict)
    quotes: dict[str, str] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)

    def merge(self, other: ExtractionOutput) -> None:
        self.values.update(other.values)
        self.quotes.update(other.quotes)
        self.flags.update(other.flags)


def partition_by_topic(nodes: list[FieldNode]) -> dict[str, list[FieldNode]]:
    """Split *nodes* into groups keyed by ``topic`` (order-preserving)."""
    groups: dict[str, list[FieldNode]] = {}
    for node in nodes:
        groups.setdefault(node.topic, []).append(node)
    return groups


def group_strategy(group: list[FieldNode], cfg: Settings) -> str:
    """Route a topic group to ``SELF_CONSISTENCY`` or ``STANDARD`` (§4.4)."""
    for node in group:
        if node.mandatory and node.priority >= cfg.HIGH_PRIORITY:
            return SELF_CONSISTENCY
    return STANDARD


def _unpack(instance: BaseModel, group: list[FieldNode]) -> tuple[dict[str, Any], dict[str, str]]:
    """Extract {name: value} and {name: quote} from a populated group model.

    ``NOT_PRESENT`` is normalised to ``None``; quotes are only kept for populated
    values.
    """
    values: dict[str, Any] = {}
    quotes: dict[str, str] = {}
    fields = type(instance).model_fields
    for node in group:
        if node.name not in fields:
            continue  # excluded from the extraction schema
        raw = getattr(instance, node.name)
        value = None if raw == NOT_PRESENT else raw
        values[node.name] = value
        if value is not None and node.needs_evidence:
            quote = getattr(instance, quote_field_name(node.name), None)
            if isinstance(quote, str) and quote.strip():
                quotes[node.name] = quote
    return values, quotes


def _call(
    client: instructor.Instructor,
    response_model: type[BaseModel],
    text: str,
    cfg: Settings,
) -> BaseModel:
    """One Instructor structured-output call (validator drives bounded retry)."""
    result: BaseModel = client.chat.completions.create(
        model=cfg.MODEL_HEAVY,
        response_model=response_model,
        max_retries=3,
        messages=[
            {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        **sampling_params(cfg),
    )
    return result


def _extract_standard(
    client: instructor.Instructor,
    group: list[FieldNode],
    text: str,
    cfg: Settings,
    model_name: str,
) -> ExtractionOutput:
    model = build_extraction_model(group, model_name=model_name, cfg=cfg)
    instance = _call(client, model, text, cfg)
    values, quotes = _unpack(instance, group)
    return ExtractionOutput(values=values, quotes=quotes)


def _extract_self_consistency(
    client: instructor.Instructor,
    group: list[FieldNode],
    text: str,
    cfg: Settings,
    model_name: str,
) -> ExtractionOutput:
    """Run N votes; per field take the majority value, else null + flag (§4.4)."""
    model = build_extraction_model(group, model_name=model_name, cfg=cfg)
    n = max(1, cfg.SELF_CONSISTENCY_N)
    samples: list[tuple[dict[str, Any], dict[str, str]]] = []
    for _ in range(n):
        instance = _call(client, model, text, cfg)
        samples.append(_unpack(instance, group))

    out = ExtractionOutput()
    for node in group:
        if node.name not in model.model_fields:
            continue
        votes = [values.get(node.name) for values, _ in samples]
        counts = Counter(_hashable(v) for v in votes)
        winner_key, top_count = counts.most_common(1)[0]
        if top_count == n:
            out.values[node.name] = _from_hashable(winner_key)
        elif top_count > n / 2:
            out.values[node.name] = _from_hashable(winner_key)
            out.flags[node.name] = {"vote": f"{top_count}/{n}", "agreement": "majority"}
        else:
            out.values[node.name] = None
            out.flags[node.name] = {"vote": f"{top_count}/{n}", "agreement": "disagreement"}
        # Attach the quote from the first sample that produced the winning value.
        winning_value = out.values[node.name]
        if winning_value is not None:
            for values, quotes in samples:
                if values.get(node.name) == winning_value and node.name in quotes:
                    out.quotes[node.name] = quotes[node.name]
                    break
    return out


def _hashable(value: Any) -> Any:
    """Make a field value hashable for vote counting."""
    if isinstance(value, list):
        return ("__list__", tuple(value))
    if isinstance(value, dict):
        return ("__dict__", tuple(sorted(value.items())))
    return value


def _from_hashable(key: Any) -> Any:
    if isinstance(key, tuple) and key and key[0] == "__list__":
        return list(key[1])
    if isinstance(key, tuple) and key and key[0] == "__dict__":
        return dict(key[1])
    return key


def extract_fields(
    chunks: list[Chunk],
    nodes: list[FieldNode],
    cfg: Settings | None = None,
    client: instructor.Instructor | None = None,
) -> ExtractionOutput:
    """Stages 3–4: partition by topic and extract every group sequentially.

    Parameters
    ----------
    chunks:
        Block 1's scored, coref-resolved chunks (the source text).
    nodes:
        The enriched field nodes for the active product (Stage-1 selection).
    cfg:
        Optional settings override.
    client:
        Optional pre-built Instructor client (injected in tests).
    """
    effective_cfg = cfg or _default_settings
    text = document_text(chunks)
    llm = client or build_instructor_client(effective_cfg)

    # Only extractable (non-derived, non-excluded) fields reach the LLM.
    extractable = [n for n in nodes if is_extractable(n, effective_cfg.EXTRACTION_EXCLUDED_TYPES)]
    groups = partition_by_topic(extractable)

    result = ExtractionOutput()
    for topic in sorted(groups):
        group = groups[topic]
        strategy = group_strategy(group, effective_cfg)
        model_name = f"Group_{topic}"
        logger.info(
            "Block 3 v2 — extracting topic '%s' (%d fields) via %s.",
            topic,
            len(group),
            strategy,
        )
        if strategy == SELF_CONSISTENCY:
            group_out = _extract_self_consistency(llm, group, text, effective_cfg, model_name)
        else:
            group_out = _extract_standard(llm, group, text, effective_cfg, model_name)
        result.merge(group_out)
    return result
