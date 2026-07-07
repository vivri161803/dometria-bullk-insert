"""Grounding gate, completeness and assembly (Block 3 v2, Stage 8).

Final pass over the merged record:

* **Grounding gate** — a single LLM-as-judge call returns a **pass/fail** verdict
  on the entailment of each populated, evidence-bearing field's value by its
  verbatim quote.  Fields that fail are nulled and flagged ``ungrounded``.
* **Completeness** — any null **mandatory** field is routed to ``review_queue``.
* **Assemble** — produce ``{record, evidence, flags, review_queue}`` plus a
  scalar ``confidence`` (pass-rate × mandatory-completeness) for the ``Asset``.

The judge is skipped when there is nothing evidence-bearing to score, keeping the
core path cheap; low confidence never raises (fail-safe).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import instructor
from pydantic import BaseModel

from bulk_insert.blocks.extraction.client import build_instructor_client, document_text
from bulk_insert.blocks.extraction.extract import ExtractionOutput
from bulk_insert.blocks.extraction.tree import FieldNode, items_field_name
from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings
from bulk_insert.llm import sampling_params
from bulk_insert.models.chunk import Chunk

logger = logging.getLogger(__name__)

__all__: list[str] = ["GroundResult", "FieldAssessment", "GroundingReport", "ground_and_assemble"]


class FieldAssessment(BaseModel):
    """One field's pass/fail entailment judgement."""

    field: str
    passed: bool


class GroundingReport(BaseModel):
    """Instructor response model for the grounding gate."""

    assessments: list[FieldAssessment]


@dataclass
class GroundResult:
    """The assembled final result surfaced through :class:`Asset`."""

    record: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)
    review_queue: list[str] = field(default_factory=list)
    confidence: float = 0.0


_JUDGE_SYSTEM_PROMPT = (
    "You are a strict grounding judge. For each field below you are given the "
    "extracted value and the verbatim quote it was taken from. Decide whether the "
    "quote (in the context of the document) ENTAILS the value: passed=true if the "
    "value is fully supported by the quote, passed=false if it is unsupported or "
    "contradicted. Return one assessment per field, using the exact field names."
)


def _grounding_candidates(result: ExtractionOutput, nodes: list[FieldNode]) -> list[FieldNode]:
    """Populated, evidence-bearing, non-derived fields eligible for judging."""
    by_name = {n.name: n for n in nodes}
    candidates: list[FieldNode] = []
    for name, value in result.values.items():
        node = by_name.get(name)
        if node is None or node.is_derived or not node.needs_evidence:
            continue
        if value is not None and name in result.quotes:
            candidates.append(node)
    return candidates


def _judge_prompt(candidates: list[FieldNode], result: ExtractionOutput, text: str) -> str:
    lines = ["Document:", text, "", "Fields to assess:"]
    for node in candidates:
        lines.append(
            f"- {node.name}: value={result.values[node.name]!r} quote={result.quotes[node.name]!r}"
        )
    return "\n".join(lines)


def _run_grounding_gate(
    result: ExtractionOutput,
    nodes: list[FieldNode],
    text: str,
    cfg: Settings,
    client: instructor.Instructor | None,
) -> dict[str, bool]:
    """Return per-field pass/fail entailment verdicts; empty when nothing to judge."""
    candidates = _grounding_candidates(result, nodes)
    if not candidates:
        return {}
    llm = client or build_instructor_client(cfg)
    report: Any = llm.chat.completions.create(
        model=cfg.MODEL_HEAVY,
        response_model=GroundingReport,
        max_retries=3,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": _judge_prompt(candidates, result, text)},
        ],
        **sampling_params(cfg),
    )
    return {a.field: a.passed for a in report.assessments}


def ground_and_assemble(
    chunks: list[Chunk],
    result: ExtractionOutput,
    nodes: list[FieldNode],
    cfg: Settings | None = None,
    client: instructor.Instructor | None = None,
    run_judge: bool = True,
) -> GroundResult:
    """Stage 8 — gate, complete, and assemble the final result.

    Parameters
    ----------
    run_judge:
        When ``False``, the LLM grounding gate is skipped (no LLM call); useful
        for tests and cheap/offline paths.  Grounding then defaults to trusting
        the quote-validated extraction.
    """
    effective_cfg = cfg or _default_settings
    text = document_text(chunks)

    verdicts: dict[str, bool] = {}
    if run_judge:
        verdicts = _run_grounding_gate(result, nodes, text, effective_cfg, client)

    out = GroundResult()

    # Count-aggregate companions (§10) are internal: they collapse to the derived
    # count in `data`, and their per-item quotes surface as the scalar's evidence.
    companion_of = {items_field_name(n.name): n for n in nodes if n.aggregate is not None}

    # Copy flags forward (voting/derivation), then apply the boolean grounding gate.
    out.flags = dict(result.flags)
    for name, value in result.values.items():
        if name in companion_of:
            scalar = companion_of[name].name
            if isinstance(value, list) and value:
                out.evidence[scalar] = " | ".join(
                    str(it.get("quote", "")) for it in value if isinstance(it, dict)
                )
            continue
        grounded_value = value
        if name in verdicts and not verdicts[name]:
            grounded_value = None
            flag = dict(out.flags.get(name, {}))
            flag["ungrounded"] = True
            out.flags[name] = flag
            logger.warning("Field '%s' failed grounding gate (not entailed); nulled.", name)
        elif name in verdicts:
            flag = dict(out.flags.get(name, {}))
            flag["grounding"] = True
            out.flags[name] = flag
        out.record[name] = grounded_value
        if grounded_value is not None and name in result.quotes:
            out.evidence[name] = result.quotes[name]

    # Completeness — null mandatory fields go to the review queue.
    mandatory_total = 0
    mandatory_filled = 0
    for node in nodes:
        if not node.mandatory:
            continue
        mandatory_total += 1
        if out.record.get(node.name) is not None:
            mandatory_filled += 1
        else:
            out.review_queue.append(node.name)

    # Confidence — grounding pass-rate × mandatory-completeness.
    passed_count = sum(1 for passed in verdicts.values() if passed)
    grounding = passed_count / len(verdicts) if verdicts else 1.0
    completeness = mandatory_filled / mandatory_total if mandatory_total else 1.0
    out.confidence = max(0.0, min(1.0, grounding * completeness))

    logger.info(
        "Block 3 v2 — grounded: %d fields, %d flags, %d review-queue, confidence=%.3f.",
        len(out.record),
        len(out.flags),
        len(out.review_queue),
        out.confidence,
    )
    return out
