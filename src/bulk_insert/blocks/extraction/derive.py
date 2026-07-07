"""Derivation passes — computed (code) + judged (2nd LLM run) (Stages 5–7).

Two strictly separated derivation layers on top of the transcribed primitives:

* **Computed** (Stage 5) — deterministic arithmetic/temporal rules evaluated in
  **Python** from a field's ``source_fields``.  Provenance points at the source
  fields' quotes; computed fields are exempt from the verbatim-quote validator.
* **Reconcile** (Stage 7) — when a computed field's target was *also* extracted
  from text, cross-check the two; disagreement raises a flag (free verification).
* **Judged** (Stage 6) — the rare fields needing reasoning are handled by an
  isolated LLM run over **extracted primitives + their quotes only, never the raw
  document**; entailment-grounded, and voted when high-value.

The shipped metadata sidecar seeds **no** derivations (all ``null``), so these
passes are no-ops on the current artifacts; they are exercised via injected
metadata (tests) and become live when a human/offline step adds derivations.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import instructor
from pydantic import BaseModel, Field, create_model

from bulk_insert.blocks.extraction.client import build_instructor_client
from bulk_insert.blocks.extraction.extract import ExtractionOutput
from bulk_insert.blocks.extraction.schema import sanitize_model_name
from bulk_insert.blocks.extraction.tree import DerivationKind, FieldNode
from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings
from bulk_insert.llm import sampling_params

logger = logging.getLogger(__name__)

__all__: list[str] = [
    "COMPUTED_RULES",
    "derive_computed",
    "derive_judged",
]


# ---------------------------------------------------------------------------
# Computed rules (Stage 5)
# ---------------------------------------------------------------------------


def _nums(values: list[Any]) -> list[float] | None:
    """Coerce source values to floats; ``None`` if any is missing/non-numeric."""
    out: list[float] = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, int | float | str):
            return None
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            return None
    return out


def _rule_sum(values: list[Any]) -> Any:
    nums = _nums(values)
    return None if nums is None else sum(nums)


def _rule_difference(values: list[Any]) -> Any:
    nums = _nums(values)
    if nums is None or len(nums) != 2:
        return None
    return nums[0] - nums[1]


def _rule_product(values: list[Any]) -> Any:
    nums = _nums(values)
    if nums is None:
        return None
    total = 1.0
    for n in nums:
        total *= n
    return total


def _rule_ratio(values: list[Any]) -> Any:
    nums = _nums(values)
    if nums is None or len(nums) != 2 or nums[1] == 0:
        return None
    return nums[0] / nums[1]


def _rule_min(values: list[Any]) -> Any:
    nums = _nums(values)
    return None if nums is None or not nums else min(nums)


def _rule_max(values: list[Any]) -> Any:
    nums = _nums(values)
    return None if nums is None or not nums else max(nums)


def _rule_mean(values: list[Any]) -> Any:
    """Arithmetic mean of the sources (e.g. average rating/stars)."""
    nums = _nums(values)
    return None if nums is None or not nums else sum(nums) / len(nums)


def _rule_percent(values: list[Any]) -> Any:
    """``part / whole * 100`` (2 sources) — a percentage, not a bare ratio."""
    nums = _nums(values)
    if nums is None or len(nums) != 2 or nums[1] == 0:
        return None
    return nums[0] / nums[1] * 100.0


# Hectares ↔ square metres: 1 ha = 10_000 m². Single-source unit conversions.
_HA_IN_MQ = 10_000.0


def _rule_ha_to_mq(values: list[Any]) -> Any:
    nums = _nums(values)
    if nums is None or len(nums) != 1:
        return None
    return nums[0] * _HA_IN_MQ


def _rule_mq_to_ha(values: list[Any]) -> Any:
    nums = _nums(values)
    if nums is None or len(nums) != 1:
        return None
    return nums[0] / _HA_IN_MQ


def _rule_count(values: list[Any]) -> Any:
    """``len`` of a single transcribed companion list (§10).

    ``[items]`` → ``len(items)`` (empty list → ``0``); a missing/``None``
    companion → ``None`` (unknown, not zero). Not numeric like the other rules —
    it counts grounded items rather than summing sibling scalars.
    """
    if len(values) != 1:
        return None
    items = values[0]
    if items is None:
        return None
    return len(items) if isinstance(items, list) else None


# Deterministic computed-rule registry (``rule`` string → function).
#
# Every rule is a **pure** function of its ``source_fields`` values — no clock,
# no config, no I/O — so the same primitives always derive the same value (the
# pipeline's reproducibility guarantee). Temporal "age from *now*" is
# deliberately absent: it would depend on ``datetime.now()`` and drift day to
# day. Compute an age as ``difference(reference_year, year_built)`` with the
# reference year itself an extracted primitive instead.
COMPUTED_RULES: dict[str, Callable[[list[Any]], Any]] = {
    "sum": _rule_sum,
    "difference": _rule_difference,
    "product": _rule_product,
    "ratio": _rule_ratio,
    "min": _rule_min,
    "max": _rule_max,
    "mean": _rule_mean,
    "percent": _rule_percent,
    "ha_to_mq": _rule_ha_to_mq,
    "mq_to_ha": _rule_mq_to_ha,
    "count": _rule_count,
}


def derive_computed(
    result: ExtractionOutput,
    nodes: list[FieldNode],
    cfg: Settings | None = None,
) -> None:
    """Fill ``COMPUTED`` fields in place from their ``source_fields`` (Stages 5+7)."""
    _ = cfg  # reserved; computed derivation is deterministic and config-free
    for node in nodes:
        derivation = node.derivation
        if derivation is None or derivation.kind is not DerivationKind.COMPUTED:
            continue
        rule_fn = COMPUTED_RULES.get(derivation.rule)
        if rule_fn is None:
            logger.warning("Computed field '%s' has unknown rule '%s'.", node.name, derivation.rule)
            result.flags[node.name] = {"derived": "computed", "error": "unknown_rule"}
            continue

        source_values = [result.values.get(sf) for sf in derivation.source_fields]
        computed = rule_fn(source_values)

        flag: dict[str, Any] = {
            "derived": "computed",
            "rule": derivation.rule,
            "source_fields": list(derivation.source_fields),
            "source_quotes": {
                sf: result.quotes[sf] for sf in derivation.source_fields if sf in result.quotes
            },
        }

        # Stage 7 — reconcile against any independently-stated value.
        stated = result.values.get(node.name)
        if stated is not None and computed is not None and stated != computed:
            flag["reconcile"] = {"stated": stated, "computed": computed, "agreement": False}
            logger.warning(
                "Reconcile mismatch for '%s': stated=%r computed=%r.",
                node.name,
                stated,
                computed,
            )
        elif stated is not None and computed is not None:
            flag["reconcile"] = {"agreement": True}

        result.values[node.name] = computed
        result.flags[node.name] = flag


# ---------------------------------------------------------------------------
# Judged derivation (Stage 6)
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM_PROMPT = (
    "You derive a single field by reasoning ONLY over the extracted primitives "
    "and their verbatim quotes provided below. You must NOT assume any fact that "
    "is not entailed by these inputs. First reason step by step, then output the "
    "derived value and a short justification citing the inputs. If the value "
    "cannot be entailed from the inputs, return null."
)


def _judge_response_model(node: FieldNode) -> type[BaseModel]:
    annotation: Any = str if node.field_type in ("text", "select", "boolean") else float
    if node.field_type in ("integer", "year"):
        annotation = int
    model: type[BaseModel] = create_model(
        sanitize_model_name(f"Judged_{node.name}"),
        value=(annotation | None, Field(default=None)),
        justification=(str, Field(...)),
    )
    return model


def _primitives_prompt(node: FieldNode, result: ExtractionOutput) -> str:
    lines = [f"Derive the field '{node.name}' ({node.prompt_context}).", "", "Primitives:"]
    for sf in node.derivation.source_fields if node.derivation else ():
        value = result.values.get(sf)
        quote = result.quotes.get(sf)
        lines.append(f"- {sf} = {value!r}" + (f"  (quote: {quote!r})" if quote else ""))
    return "\n".join(lines)


def derive_judged(
    result: ExtractionOutput,
    nodes: list[FieldNode],
    cfg: Settings | None = None,
    client: instructor.Instructor | None = None,
) -> None:
    """Fill ``JUDGED`` fields in place via an isolated primitives-only LLM run."""
    effective_cfg = cfg or _default_settings
    judged = [
        n for n in nodes if n.derivation is not None and n.derivation.kind is DerivationKind.JUDGED
    ]
    if not judged:
        return
    llm = client or build_instructor_client(effective_cfg)

    for node in judged:
        model = _judge_response_model(node)
        prompt = _primitives_prompt(node, result)
        high_value = node.mandatory and node.priority >= effective_cfg.HIGH_PRIORITY
        n = max(1, effective_cfg.SELF_CONSISTENCY_N) if high_value else 1

        votes: list[Any] = []
        justification: str | None = None
        for _ in range(n):
            response: Any = llm.chat.completions.create(
                model=effective_cfg.MODEL_HEAVY,
                response_model=model,
                max_retries=3,
                messages=[
                    {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                **sampling_params(effective_cfg),
            )
            votes.append(response.value)
            if justification is None:
                justification = response.justification

        value = _majority(votes, n)
        result.values[node.name] = value
        flag: dict[str, Any] = {
            "derived": "judged",
            "source_fields": list(node.derivation.source_fields) if node.derivation else [],
            "justification": justification,
        }
        if value is None and any(v is not None for v in votes):
            flag["agreement"] = "disagreement"
        result.flags[node.name] = flag


def _majority(votes: list[Any], n: int) -> Any:
    """Majority value across judged votes; ``None`` if no strict majority."""
    from collections import Counter

    counts = Counter(v for v in votes)
    if not counts:
        return None
    winner, top = counts.most_common(1)[0]
    if top > n / 2:
        return winner
    return None
