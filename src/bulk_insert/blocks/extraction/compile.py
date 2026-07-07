"""Offline enum/description compile for the metadata sidecar (Phase 6.8).

A **one-off, offline** step (gated behind an API key like the integration tests)
that fills the deferred ``field_metadata.yaml`` stubs the ingest seeded:

* an **Italian** ``description`` (extraction-prompt context) for every pending field;
* for closed-vocab (``select`` / ``boolean``) candidates, a refined **Italian**
  ``enum.values`` list (blessing/refining the seeded options, never inventing wildly)
  plus an ``applicable`` decision — ``applicable=False`` ⇒ ``enum.status:
  not_applicable``;
* a proposed ``topic`` name, deduped into a compact **topics dictionary**
  (``topic name → integer id``) written to ``artifacts/topics.yaml``; each field's
  ``topic`` is rewritten to that id (the runtime resolves id → name in
  :mod:`bulk_insert.blocks.extraction.metadata`);
* a boolean ``unsure`` self-flag so a reviewer can inspect only the low-confidence
  subset (``grep 'unsure: true'``).

Compiling a field is **approved by default**: it writes ``description`` +
``enum.values``, flips ``enum.status`` to ``compiled`` (or ``not_applicable``), and
records ``unsure`` immediately — no separate approval step, the runtime picks up
``compiled`` enums on the next load.  The enum/description parts require **no**
runtime change; the topic-id scheme adds one small resolution lookup.

This module is the **pure, testable core** (mocked-Instructor unit tests); the CLI
wrapper is ``scripts/compile_metadata.py``.  It reuses
:func:`bulk_insert.blocks.extraction.client.build_instructor_client` and
:func:`bulk_insert.llm.sampling_params`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import instructor
import yaml
from pydantic import BaseModel, Field

from bulk_insert.blocks.extraction.client import build_instructor_client
from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings
from bulk_insert.llm import sampling_params

logger = logging.getLogger(__name__)

__all__: list[str] = [
    "CompiledField",
    "CompileBatch",
    "CompilePlan",
    "load_registry",
    "load_topics",
    "compile_batch",
    "plan_compile",
    "dump_topics_yaml",
    "NOT_PRESENT",
    "ENUM_CANDIDATE_TYPES",
]

# Abstention sentinel injected into every compiled enum at *runtime* (schema-gen);
# the compile step must never emit it as a value.
NOT_PRESENT = "NOT_PRESENT"

# Contract field types that carry a closed vocabulary (mirrors ingest seeding).
ENUM_CANDIDATE_TYPES: frozenset[str] = frozenset({"select", "boolean"})

_SYSTEM_PROMPT = (
    "You compile field metadata for a data-extraction schema over Italian "
    "real-estate and business documents. For EACH field you receive, produce, "
    "STRICTLY IN ITALIAN:\n"
    "- description: one concise sentence describing what the field captures, usable "
    "as extraction-prompt context.\n"
    "- topic: a short grouping tag (one or two lowercase Italian words) that groups "
    "semantically related fields (e.g. 'identità', 'ubicazione', 'superfici', "
    "'prezzo'). Reuse the same topic name across related fields.\n"
    "- For enum-candidate fields ONLY: values — a cleaned, de-duplicated, closed "
    "vocabulary IN ITALIAN that blesses/refines the seeded options. Do NOT invent "
    "options unrelated to the seeds. Set applicable=false when the field is really "
    "free-text or numeric with no closed vocabulary. For non-candidate fields leave "
    "values empty and applicable=true.\n"
    "- unsure: true when you are not confident about this field's description, topic, "
    "or values.\n"
    "NEVER include the sentinel 'NOT_PRESENT' among values (it is added later). "
    "Echo each field's 'name' EXACTLY as given."
)


class CompiledField(BaseModel):
    """One field's compiled metadata, as returned by the LLM."""

    name: str = Field(description="The field key, echoed verbatim from the input.")
    description: str = Field(description="Concise Italian extraction-prompt description.")
    topic: str = Field(description="Short Italian grouping topic name.")
    applicable: bool = Field(
        default=True,
        description="For enum candidates: whether a closed vocabulary applies.",
    )
    values: list[str] = Field(
        default_factory=list,
        description="Refined Italian enum values (enum candidates only).",
    )
    unsure: bool = Field(default=False, description="Low-confidence self-flag.")


class CompileBatch(BaseModel):
    """Instructor response model — the compiled metadata for a batch of fields."""

    fields: list[CompiledField]


@dataclass
class CompilePlan:
    """The result of planning a compile run (before any write)."""

    fields_meta: dict[str, dict[str, Any]]  # full sidecar fields, post-compile
    topics: dict[str, int]  # topic name -> id (stable, extended in place)
    enum_compile_status: str  # global marker: "pending" | "compiled"
    compiled: list[str] = field(default_factory=list)  # names touched this run
    before: dict[str, dict[str, Any]] = field(default_factory=dict)  # pre-compile snapshots


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_registry(cfg: Settings | None = None) -> dict[str, dict[str, Any]]:
    """Load ``field_registry.json`` → ``{field_name: registry entry}``."""
    effective_cfg = cfg or _default_settings
    path = effective_cfg.ARTIFACTS_DIR / "field_registry.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {entry["key"]: entry for entry in payload.get("fields", [])}


def load_topics(cfg: Settings | None = None) -> dict[str, int]:
    """Load the topics dictionary (``topic name -> id``); empty if absent."""
    effective_cfg = cfg or _default_settings
    path = Path(effective_cfg.TOPICS_PATH)
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    topics = payload.get("topics") or {}
    if not isinstance(topics, dict):
        raise ValueError(f"Malformed topics file {path}: 'topics' is not a mapping.")
    return {str(name): int(tid) for name, tid in topics.items()}


# ---------------------------------------------------------------------------
# Selection + context
# ---------------------------------------------------------------------------


def _needs_compile(meta: dict[str, Any]) -> bool:
    """A field is pending compilation while its description or enum is still a stub."""
    if meta.get("description") is None:
        return True
    enum_block = meta.get("enum")
    return isinstance(enum_block, dict) and enum_block.get("status") == "pending"


def _is_enum_candidate(name: str, registry: dict[str, dict[str, Any]]) -> bool:
    entry = registry.get(name, {})
    return str(entry.get("field_type")) in ENUM_CANDIDATE_TYPES


def _field_context(name: str, registry: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build the LLM input context for one field from the registry entry."""
    entry = registry.get(name, {})
    return {
        "name": name,
        "label": entry.get("label") or name,
        "field_type": entry.get("field_type"),
        "enum_candidate": _is_enum_candidate(name, registry),
        "seeded_options": list(entry.get("options") or []),
        "example_products": list(entry.get("products") or [])[:8],
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _dedupe(values: list[str]) -> list[str]:
    """Strip, drop the ``NOT_PRESENT`` sentinel, and de-dupe case-insensitively."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw).strip()
        if not text or text.upper() == NOT_PRESENT:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned


def _clean_values(values: list[str], seeded: list[str]) -> list[str]:
    """Clean the model's refined vocabulary, falling back to the seeded options.

    The model may *refine* (drop/rename) the seeded options, so its list wins when
    it is non-empty; only when it returns nothing usable do we fall back to the
    seeded options, so a blessed enum never collapses to empty (faithfulness, §6.8).
    """
    refined = _dedupe(values)
    return refined if refined else _dedupe(seeded)


def _validate_compiled(result: CompiledField, label: str) -> CompiledField:
    """Enforce the minimal Italian-output invariants (non-empty; flag if not)."""
    description = result.description.strip()
    unsure = result.unsure
    if not description:
        # Reject an empty description: fall back to the label and flag for review.
        description = label
        unsure = True
    topic = result.topic.strip().casefold()
    if not topic:
        topic = "generico"
        unsure = True
    return result.model_copy(update={"description": description, "topic": topic, "unsure": unsure})


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def _compile_model(cfg: Settings) -> str:
    """The compile model slug — ``MODEL_COMPILE`` if set, else ``MODEL_HEAVY``."""
    return cfg.MODEL_COMPILE or cfg.MODEL_HEAVY


def _batched(items: list[str], size: int) -> list[list[str]]:
    size = max(1, size)
    return [items[i : i + size] for i in range(0, len(items), size)]


def compile_batch(
    names: list[str],
    registry: dict[str, dict[str, Any]],
    cfg: Settings | None = None,
    client: instructor.Instructor | None = None,
) -> dict[str, CompiledField]:
    """Compile *names* via batched Instructor calls → ``{name: CompiledField}``.

    Missing fields in a response fall back to a flagged label-only compilation, so
    the caller always gets an entry per requested name.
    """
    effective_cfg = cfg or _default_settings
    if not names:
        return {}
    llm = client or build_instructor_client(effective_cfg, model=_compile_model(effective_cfg))

    results: dict[str, CompiledField] = {}
    for batch in _batched(names, effective_cfg.COMPILE_BATCH_SIZE):
        contexts = [_field_context(n, registry) for n in batch]
        user_msg = (
            "Compile the following fields. Respond with one entry per field, "
            "echoing each 'name' exactly.\n\n" + json.dumps(contexts, ensure_ascii=False, indent=2)
        )
        response: Any = llm.chat.completions.create(
            model=_compile_model(effective_cfg),
            response_model=CompileBatch,
            max_retries=3,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            **sampling_params(effective_cfg),
        )
        by_name = {f.name: f for f in response.fields}
        for name in batch:
            label = str(registry.get(name, {}).get("label") or name)
            compiled = by_name.get(name)
            if compiled is None:
                logger.warning("Compile response missing field %r; flagging unsure.", name)
                compiled = CompiledField(
                    name=name, description=label, topic="generico", unsure=True
                )
            results[name] = _validate_compiled(compiled, label)
    return results


# ---------------------------------------------------------------------------
# Plan (apply compiled results onto the sidecar + topics dict)
# ---------------------------------------------------------------------------


def _allocate_topic(name: str, topics: dict[str, int]) -> int:
    """Return the id for topic *name*, allocating a fresh stable id if new."""
    if name in topics:
        return topics[name]
    next_id = (max(topics.values()) + 1) if topics else 1
    topics[name] = next_id
    return next_id


def _apply(
    name: str,
    meta: dict[str, Any],
    compiled: CompiledField,
    registry: dict[str, dict[str, Any]],
    topics: dict[str, int],
) -> dict[str, Any]:
    """Build the post-compile meta block for one field (approved by default)."""
    seeded = list((registry.get(name, {}) or {}).get("options") or [])
    enum_candidate = _is_enum_candidate(name, registry)

    if enum_candidate and compiled.applicable:
        values = _clean_values(compiled.values, seeded)
        if values:
            enum_block = {"status": "compiled", "values": values}
        else:  # nothing usable → treat as open-vocab
            enum_block = {"status": "not_applicable", "values": []}
    else:
        # Non-candidate field, or the model judged the vocabulary inapplicable.
        enum_block = {"status": "not_applicable", "values": []}

    topic_id = _allocate_topic(compiled.topic, topics)

    # Canonical key order, preserving human-owned fields (priority/mandatory/derivation).
    return {
        "description": compiled.description,
        "topic": topic_id,
        "mandatory": bool(meta.get("mandatory", False)),
        "priority": int(meta.get("priority", 3)),
        "needs_evidence": bool(meta.get("needs_evidence", True)),
        "enum": enum_block,
        "derivation": meta.get("derivation"),
        "unsure": bool(compiled.unsure),
    }


def plan_compile(
    fields_meta: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    topics: dict[str, int],
    cfg: Settings | None = None,
    client: instructor.Instructor | None = None,
    only_field: str | None = None,
    force: bool = False,
) -> CompilePlan:
    """Plan a compile run over *fields_meta* (no write).

    Selects fields still pending (or all matching, with ``force``), compiles them
    via the LLM, and overlays the results onto a **copy** of the sidecar plus the
    topics dictionary.  The global ``enum_compile_status`` flips to ``compiled``
    once no field remains pending.
    """
    new_fields: dict[str, dict[str, Any]] = {n: dict(m) for n, m in fields_meta.items()}
    new_topics: dict[str, int] = dict(topics)

    candidates = [
        name
        for name, meta in fields_meta.items()
        if (only_field is None or name == only_field) and (force or _needs_compile(meta))
    ]

    compiled_map = compile_batch(candidates, registry, cfg, client)

    before: dict[str, dict[str, Any]] = {}
    compiled_names: list[str] = []
    for name in candidates:
        result = compiled_map.get(name)
        if result is None:  # pragma: no cover - compile_batch always fills candidates
            continue
        before[name] = dict(fields_meta[name])
        new_fields[name] = _apply(name, fields_meta[name], result, registry, new_topics)
        compiled_names.append(name)

    still_pending = any(_needs_compile(m) for m in new_fields.values())
    status = "pending" if still_pending else "compiled"

    logger.info(
        "Planned compile: %d field(s), %d topic(s); global status → %s",
        len(compiled_names),
        len(new_topics),
        status,
    )
    return CompilePlan(
        fields_meta=new_fields,
        topics=new_topics,
        enum_compile_status=status,
        compiled=compiled_names,
        before=before,
    )


# ---------------------------------------------------------------------------
# Emit topics.yaml
# ---------------------------------------------------------------------------

_TOPICS_HEADER = """\
# topics.yaml — grouping-topic cluster dictionary (representative name -> id), §6.8.
#
# Proposed by the offline compile step, then consolidated: near-duplicate topics
# are clustered with the embedding model and each cluster is kept under its most-used
# representative name. Each field's `topic` in field_metadata.yaml is one of these
# ids; the runtime (metadata.py) resolves the id back to the name. Because ids come
# from clustering, they may change if topics, the embedding model, or the threshold
# change — treat them as derived, not hand-assigned.
"""


def dump_topics_yaml(topics: dict[str, int]) -> str:
    """Emit the topics dictionary as ``name: id`` YAML, ordered by id."""
    ordered = dict(sorted(topics.items(), key=lambda kv: kv[1]))
    block = {"meta": {"schema_version": 1}, "topics": ordered}
    return _TOPICS_HEADER + yaml.safe_dump(block, sort_keys=False, allow_unicode=True)
