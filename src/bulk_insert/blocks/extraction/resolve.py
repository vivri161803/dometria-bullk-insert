"""OUT_OF_VOCAB resolution (Block 3 v2, Stage 4c, Phase 6.10 Part B.1).

When the extractor marks a compiled-enum field as :data:`OUT_OF_VOCAB` — present
but no listed value fits — a **scoped judge** infers the real value from the
field's own **verbatim quote + description only, never the raw document**,
re-mapping to the closest listed value when one genuinely fits and otherwise
emitting a faithful free-text value.  Because the value is inferred *from* the
quote, it passes the Stage-8 entailment gate by construction.

Reuses the isolated-LLM machinery of :mod:`derive` (same client, sampling, and
majority vote for mandatory/high-priority fields).  Disabled
(``OOV_ENABLED=False``) ⇒ any lingering sentinel falls back to today's behaviour
(normalised to ``None``).
"""

from __future__ import annotations

import logging
from typing import Any

import instructor
from pydantic import BaseModel, Field

from bulk_insert.blocks.extraction.client import build_instructor_client
from bulk_insert.blocks.extraction.derive import _majority
from bulk_insert.blocks.extraction.extract import ExtractionOutput
from bulk_insert.blocks.extraction.tree import FieldNode
from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings
from bulk_insert.llm import sampling_params

logger = logging.getLogger(__name__)

__all__: list[str] = ["OOVResolution", "resolve_out_of_vocab"]


class OOVResolution(BaseModel):
    """The scoped judge's verdict for one OUT_OF_VOCAB field."""

    value: str = Field(description="Resolved value: a listed value if one fits, else free-text.")
    in_vocab: bool = Field(description="True if `value` is one of the allowed listed values.")


_SYSTEM_PROMPT = (
    "A closed-vocabulary field was flagged present-but-unlisted. Using ONLY the "
    "field description and the verbatim quote provided — never any other document "
    "text, and never invented facts — determine the field's value. If the quote "
    "clearly matches one of the ALLOWED VALUES, return that exact value with "
    "in_vocab=true. Otherwise return a concise value IN ITALIAN faithful to the "
    "quote, with in_vocab=false."
)


def _prompt(node: FieldNode, quote: str) -> str:
    allowed = ", ".join(node.enum) if node.enum else "(none)"
    return (
        f"Field: {node.name}\n"
        f"Description: {node.prompt_context}\n"
        f"Allowed values: {allowed}\n"
        f"Evidence quote: {quote!r}"
    )


def resolve_out_of_vocab(
    result: ExtractionOutput,
    nodes: list[FieldNode],
    cfg: Settings | None = None,
    client: instructor.Instructor | None = None,
) -> None:
    """Stage 4c — resolve every OUT_OF_VOCAB field in place from its own quote."""
    effective_cfg = cfg or _default_settings
    token = effective_cfg.OOV_TOKEN

    if not effective_cfg.OOV_ENABLED:
        for name in list(result.values):
            if result.values[name] == token:
                result.values[name] = None
        return

    by_name = {n.name: n for n in nodes}
    targets = [
        by_name[name] for name, value in result.values.items() if value == token and name in by_name
    ]
    if not targets:
        return
    llm = client or build_instructor_client(effective_cfg)

    for node in targets:
        quote = result.quotes.get(node.name, "")
        high_value = node.mandatory and node.priority >= effective_cfg.HIGH_PRIORITY
        n = max(1, effective_cfg.SELF_CONSISTENCY_N) if high_value else 1

        votes: list[str] = []
        for _ in range(n):
            response: Any = llm.chat.completions.create(
                model=effective_cfg.MODEL_HEAVY,
                response_model=OOVResolution,
                max_retries=3,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _prompt(node, quote)},
                ],
                **sampling_params(effective_cfg),
            )
            votes.append(response.value)

        agreement: str | None = None
        value: Any = _majority(votes, n) if n > 1 else votes[0]
        if value is None:  # voted but no majority → keep the first, flag it
            value = votes[0]
            agreement = "disagreement"
        if not isinstance(value, str) or not value.strip():
            value = None  # unresolved → null (present-but-unknown)

        result.values[node.name] = value
        flag = dict(result.flags.get(node.name, {}))
        resolution: dict[str, Any] = {"out_of_vocab": True, "inferred": value}
        if agreement:
            resolution["agreement"] = agreement
        flag["resolution"] = resolution
        result.flags[node.name] = flag
        logger.info("Stage 4c — resolved OUT_OF_VOCAB '%s' → %r.", node.name, value)
