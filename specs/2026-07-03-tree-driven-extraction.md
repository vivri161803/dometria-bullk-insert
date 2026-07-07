# Tree-Driven Asset Extraction — Consolidated Spec (Block 3 v2, variance-reduction)

> **Status:** proposed · **Date:** 2026-07-03 · **Supersedes:** the three source drafts
> `coldrag_tree_extraction_plan.md`, `claude_code_prompt_tree_extraction.md`,
> `tree_driven.md` (now folded into this single file).
>
> **Roadmap slot:** new **Phase 6 — Tree-Driven Extraction** (see `specs/roadmap.md`),
> inserted *before* the FastAPI service. It reworks the already-shipped Phase 4
> Block 3 implementation; the FastAPI/containerization phases renumber after it.
>
> This is a **documentation-only** spec. No code is produced here — it defines the
> data structures, artifacts, pipeline, phasing, and acceptance gates that a later
> implementation phase will build.

---

## 1. Why this exists — the variance problem

The pipeline's output must be reproducible: the same document processed twice should
yield the same asset. `scripts/measure_variance.py` already quantifies where the
instability lives, splitting it in two:

- **Schema-level** — does Block 2 pick the same contract (`lv3`) every run? A flip
  changes the whole schema. **This is a Block 2 concern and is out of scope here.**
- **Field-level** — within runs that agree on the contract, how often does each
  field produce the same value? This isolates **Block 3 value drift** — and it is
  exactly what this spec targets.

Today's Block 3 asks a single heavy-model call to populate an all-optional dynamic
model. It works, but it drifts: the model paraphrases, infers unstated values,
and silently varies wording run-to-run. The fix is **not** a bigger model or more
sampling knobs (those are already centralised in `llm.py:sampling_params` and give
diminishing returns). The fix is an **architecture that constrains the model to
transcribe evidence-grounded primitives and does everything else in code or in an
isolated second pass.**

### 1.1 Core principle — transcribe, then derive

Three strictly separated layers:

1. **Transcription** — the model extracts only evidence-grounded primitives, and
   abstains correctly (`NOT_PRESENT`) when a value is absent. Every non-null
   extracted field carries a **verbatim source quote**.
2. **Deterministic derivation** — arithmetic/temporal/cross-field values are
   computed in **Python**, never asked of the model.
3. **Judgment derivation** — the rare fields that genuinely need reasoning over
   primitives are handled by an **isolated second LLM run** that sees the extracted
   primitives *only*, never the raw document.

Variance falls because the model's job shrinks to grounded transcription with
abstention; every path that used to invite paraphrase or inference is either
removed, moved to code, or bounded by evidence and voting.

### 1.2 Faithfulness machinery (non-negotiable, retained in full)

- Paired `evidence_quote` per extracted field.
- `NOT_PRESENT` injected into **every** enum.
- A `@model_validator` that rejects any non-null field lacking its verbatim quote
  (drives Instructor's bounded retry).
- Self-consistency voting on mandatory / high-value fields.
- An LLM-as-judge entailment gate over the assembled record.

---

## 2. Where this lands in the pipeline

**This replaces Block 3 only.** Block 1 (preprocessing) and Block 2 (contract
determination) are unchanged. Block 2's `Contract(lv1, lv2, lv3)` **is** the
`classifier_output` the tree-driven framework consumes.

### 2.1 Public entrypoint — unchanged signature

The orchestrator (`pipeline.py:process_document`) and `PipelineResult` stay stable.
Block 3 keeps its public contract:

```python
extract_asset(chunks: list[Chunk], contract: Contract, cfg: Settings | None = None) -> Asset
```

- **Input** `chunks` — Block 1's scored, coref-resolved chunks (the source text).
- **Input** `contract` — Block 2's `(lv1, lv2, lv3)` = the active category path.
- **Output** `Asset` — extended to carry the richer result (§2.2).

### 2.2 Output mapping — richer result inside `Asset`

The internal framework produces `{record, evidence, flags, review_queue}` (the
component contract, §5). This maps onto the existing `Asset{contract_id, data,
confidence}` so nothing downstream breaks:

| Framework output | Asset placement |
|---|---|
| `record` (validated field values) | `Asset.data` |
| grounding/agreement → scalar | `Asset.confidence` (0–1; low confidence never raises) |
| `evidence` (per-field quotes) | `Asset` gains optional `evidence: dict[str, str] \| None` |
| `flags` (agreement/grounding) | `Asset` gains optional `flags: dict[str, Any] \| None` |
| `review_queue` (failed mandatory) | `Asset` gains optional `review_queue: list[str] \| None` |

Extending `Asset` with **optional, defaulted** fields keeps existing serialization
and tests green. The FastAPI response (Phase 7) can expose or hide the extras.

### 2.3 Invariants

- Every non-derived, non-null field carries a **verbatim source quote**.
- Every derived field carries **input-provenance** (its source fields + rule/quotes).
- No mandatory field is **silently null** — failures route to `review_queue`.
- **Fail-safe preserved:** low confidence / missing optional fields never raise;
  only structural problems (schema/contract not found) raise, as today.

---

## 3. Offline build — the field tree & metadata sidecar (built once)

The 97 standalone contract JSONs in `contracts/` are the *source* from which a
**master field tree** and per-product manifests are built. This is an **offline
build step**, run once, producing static artifacts the runtime consumes (baked
into the container image alongside `contracts/`).

> **Note on count:** the source drafts said "92 products"; this project has **97**
> `lv3` leaves / 97 contract files. This spec standardises on **97**. The ingest
> must run over an **arbitrary directory of N contracts** — never hardcode 97 (or 2).

### 3.1 Ingest order

1. **Parse.** Load each contract JSON. Walk `data.fields.native[]`; for every field
   emit `{key, label, lv_path=(lv1,lv2,lv3), type, is_required, constraints, product_id}`.
   `lv_path` comes from the contract's nested `data.category` chain.
2. **Union & dedup.** Merge across all contracts into a **field registry**: one entry
   per distinct `(key, lv_path)`, tracking which products contain it.
3. **Assemble the tree.** Build the `lv1→lv2→lv3` category tree (reuse
   `models/categories.py:CATEGORY_TREE`); attach each registry field as a leaf.
4. **Emit per-product manifests.** For each product, record its active `lv_path` set
   — the exact selection Block 2's `Contract` reproduces at runtime.
5. **Seed metadata sidecar.** For each field, fill what is *inferable* from the
   contract JSON (`type`, `is_required` → `mandatory`, `constraints.options` as an
   enum candidate) and stub the rest as human/LLM-editable (see §3.3).
6. **Validate round-trip.** Assert every original contract is exactly reconstructable
   by selecting its manifest paths against the tree. **This is the Phase-0 gate.**

**Artifacts produced (in `artifacts/` or similar, baked into the image):**
`field_registry.json`, `field_tree.json`, `product_manifests.json`,
`field_metadata.yaml`.

### 3.2 Field-node schema

```python
class DerivationKind(str, Enum):
    NONE = "none"          # extracted from text
    COMPUTED = "computed"  # deterministic rule, done in Python
    JUDGED = "judged"      # reasoning over primitives, 2nd LLM run

@dataclass
class Derivation:
    source_fields: list[str]   # field keys this value derives from
    rule: str                  # code ref (computed) or instruction (judged)
    kind: DerivationKind

@dataclass
class FieldNode:
    name: str                              # field key
    description: str                       # extraction prompt context (LLM-compiled, §4)
    lv_path: tuple[str, str, str]
    topic: str                             # grouping tag — exactly ONE per field
    mandatory: bool
    priority: int                          # concurrency/voting tiebreak
    enum: Optional[list[str]] = None       # closed vocab; NOT_PRESENT auto-added
    needs_evidence: bool = True
    derivation: Optional[Derivation] = None
```

### 3.3 The metadata sidecar — `field_metadata.yaml` (runtime-injected, always editable)

Metadata that **cannot** be inferred from the contract JSON lives in an
**editable YAML sidecar**, keyed by field, injected at runtime and **fully
modifiable thereafter** — never baked into Python. The runtime reads enums,
descriptions, topics, priorities, and derivations from this file.

> **⚠️ Deferred enum & description compilation (key decision).**
> The **enum vocabularies and field descriptions are to be compiled offline in a
> future step using an LLM via API call.** This spec does **not** perform that
> compilation. It only **prepares the ground**: the data structures, the tree, the
> full runtime pipeline, and the YAML shape — with every enum/description that
> needs LLM compilation left as a clearly-marked **stub**. Until the compile step
> runs, tests use **mock enums/descriptions**. When the compile step runs later, it
> writes back into this same YAML; nothing in the runtime changes.

Proposed per-field shape (easy to specify enum + description per field that needs it):

```yaml
# field_metadata.yaml — human- and LLM-editable; injected at runtime.
meta:
  schema_version: 1
  enum_compile_status: pending      # pending | compiled  (global marker)

fields:
  cuisine_type:                      # keyed by field name (registry key)
    description: null                # TODO: llm-compile — extraction prompt context
    topic: menu                      # TODO: human — grouping tag (exactly one)
    mandatory: false                 # seeded from contract is_required
    priority: 3                      # TODO: human — voting/concurrency tiebreak
    needs_evidence: true
    enum:
      status: pending                # pending | compiled | not_applicable
      values: []                     # TODO: llm-compile — NOT_PRESENT auto-added at runtime
    derivation: null                 # null=extracted; else {source_fields, rule, kind}

  seats_total:
    description: null
    topic: capacity
    mandatory: true                  # is_required: true
    priority: 5
    needs_evidence: true
    enum:
      status: not_applicable         # free-text / numeric field, no closed vocab
      values: []
    derivation: null
```

Rules the runtime enforces over the sidecar:

- **Enum injection:** when `enum.status == compiled`, the runtime builds a `str`-Enum
  from `values` **plus** an auto-added `NOT_PRESENT`. When `pending`, the field is
  treated as free-text (typed by `type`) and tests may inject a **mock enum**.
- **`not_applicable`** means the field is genuinely open-vocab/numeric — never enumed.
- **Description injection:** `description` is spread into the field's prompt context
  at schema-gen time; a `null`/stub description falls back to the contract `label`.
- **Excluded types unchanged:** fields with `type ∈ EXTRACTION_EXCLUDED_TYPES`
  (`foreign_id`, `decimal_pair`) and non-text `native_location` fields (geo ids /
  coordinates) are excluded from the extraction model, exactly as today.

### 3.4 Phase-0 acceptance (tree construction)

- [ ] Master tree built from the union of all N contract field sets.
- [ ] Each field node has **exactly one** `topic` tag (verify: no field maps to two).
- [ ] Every `compiled` enum contains (or is auto-augmented with) `NOT_PRESENT`.
- [ ] **Round-trip:** every original contract is exactly reconstructable from its
      manifest paths against the tree.
- [ ] Fields with `derivation != None` have `source_fields` that all exist in the tree.
- [ ] `field_metadata.yaml` emitted with inferred fields filled and LLM/human fields
      stubbed with `# TODO:` markers and correct `status` values.

---

## 4. Runtime pipeline (new Block 3 internals) — sequential, DAG-free

**No DAG, no topological sort, no workflow engine.** Field *selection* is driven by
the static tree; field *grouping* by the topic tag; groups extract **sequentially**
(see §4.4). Derivation dependencies live in post-extraction passes, not the
extraction path.

> **Concurrency decision:** groups run **sequentially within a request**, consistent
> with the project's stateless-synchronous-container philosophy (`tech-stack.md`:
> blocking calls, concurrency pushed to the platform via replicas). The source
> drafts' `asyncio.gather` fan-out is **not** adopted. `priority` therefore orders
> the *sequential* queue and breaks ties for the voting tier — it is **not** an
> execution-dependency (max-in-group semantics, never a sum).

### Stage 1 — Select
Traverse the tree over the `Contract`'s active `lv_path`; gather this product's
field nodes. Static pruning — irrelevant fields never enter the pipeline. **Cache**
the gathered set by a stable **signature** (sorted field names); ≤ 97 signatures.

### Stage 2 — Generate schema
Build the per-product Pydantic model at runtime from the **extracted (non-derived)**
nodes via `pydantic.create_model`:
- For each `needs_evidence` field, auto-synthesize a paired `{name}_quote: Optional[str]`.
- For each `compiled` enum, build a `str`-Enum including `NOT_PRESENT`.
- Attach a `@model_validator(mode="after")` that raises if any non-null field lacks
  its verbatim quote → drives Instructor per-node retry.
- **Derived fields are excluded** from the extraction schema entirely.
- **Cache** generated models by signature.

### Stage 3 — Partition
Split the extracted fields into **groups by `topic`** — a clean partition guaranteed
by the one-topic-per-field invariant (§3.4).

### Stage 4 — Extract (sequential, strategy-routed)
Process groups one after another. Per-group strategy — hard rule:

```
if group has any field where (mandatory AND priority >= HIGH_PRIORITY):
    strategy = SELF_CONSISTENCY   # N runs, vote; disagreement -> null + flag
else:
    strategy = STANDARD           # single structured-output call
```

- Both strategies use Instructor + native structured output + evidence spans +
  bounded validation/retry (reuse the block's `_build_instructor_client(cfg)` and
  `llm.py:sampling_params(cfg)`).
- **Reason-then-format is omitted on this path in v1** (frontier `MODEL_HEAVY`
  shows little format tax). Re-introduce per-group only if eval flags it.
- Source-text routing v1: the concatenated scored chunks (as today). Chunk-by-offset
  per topic is a later cost/hallucination optimisation (§7).

### Stage 5 — Derive: computed
In Python, fill `kind == COMPUTED` fields from their `source_fields` using
deterministic arithmetic/temporal rules. Provenance = pointers to the source fields'
quotes. **Exempt** from the verbatim-quote validator.

### Stage 6 — Derive: judged (only if any exist)
A **second LLM run** over **extracted primitives + their quotes only — never the raw
document**. Reason-then-format shape. Grounded by **entailment from inputs**, not a
source span. Vote (N) if the judged field is mandatory/high-value.

### Stage 7 — Reconcile
Where a field is **both** stated in text and independently derivable, cross-check
extracted vs. recomputed. Disagreement → error flag. This redundancy is free
verification — do not collapse such fields.

### Stage 8 — Ground & assemble
- LLM-as-judge **entailment gate** over the merged record; drop/flag below
  `JUDGE_THRESHOLD` (~0.7).
- Merge into the final record → `Asset` (§2.2).
- Mandatory-completeness check: any null mandatory field → `review_queue`.

---

## 5. Component contract (internal)

**Input:** `(chunks, contract)` — Block 1 chunks + Block 2 `Contract`; assumes the
prebuilt field tree + metadata sidecar.

**Output:** `{record, evidence, flags, review_queue}`, surfaced through the extended
`Asset` (§2.2).

**Invariant:** every non-derived non-null field carries a verbatim source quote;
every derived field carries input-provenance; no mandatory field is silently null.

---

## 6. Configuration (new `Settings` knobs)

All tunable from `config.py` / `.env`, read from `Settings`, never hardcoded:

| Setting | Default (proposed) | Purpose |
|---|---|---|
| `HIGH_PRIORITY` | `4` | `priority` cutoff that (with `mandatory`) triggers self-consistency voting. **Flag for human confirm.** |
| `SELF_CONSISTENCY_N` | `3` | Votes per high-value group (3–5). |
| `JUDGE_THRESHOLD` | `0.7` | LLM-as-judge entailment gate cutoff. |
| `FIELD_METADATA_PATH` | `field_metadata.yaml` | Runtime-injected sidecar path (overridable). |
| model tier | reuse `MODEL_HEAVY` | Frontier assumption holds → reason-then-format dropped on the standard path. The judge/judged passes may use `MODEL_HEAVY` or a dedicated slug (configurable). |

Voting and the judge gate multiply LLM calls per document; the sequential design and
phasing (§8) keep the core path cheap and layer the expensive tiers on top.

---

## 7. Resolved decisions & open items

**Resolved (from spec review):**
- **Block scope** → replaces **Block 3 only**; Blocks 1 & 2 unchanged.
- **Concurrency** → **sequential** group extraction; no asyncio fan-out.
- **v1 depth** → phased, **core-first**; full pipeline structured now, enum/description
  **compilation deferred** to a future offline LLM step, mocked until then.
- **Roadmap** → new **Phase 6**, before the FastAPI service.
- **Contract count** → **97**, but ingest is N-agnostic.

**Open (confirm before/at implementation):**
- Exact `HIGH_PRIORITY` integer and `SELF_CONSISTENCY_N`.
- Which inferred enum candidates are actually free-text (`not_applicable`) — decided
  by the offline enum-compile step, not now.
- Source-text routing per group: full-document vs chunk-by-offset (recommend
  full-document v1, revisit per group).
- Any field whose `derivation_kind` is ambiguous (`computed` vs `judged`) — expected
  single digits of judged fields; if larger, revisit whether some are `computed`.

**Non-goals:** no DAG / topological sort / dependency edges / workflow engine; no
reason-then-format on the standard path; no in-prompt cross-field arithmetic; no
hardcoding to specific contracts.

---

## 8. Phased delivery (maps to roadmap Phase 6)

Ship **6.0–6.2 first** — they deliver the largest reliability gain. Voting,
derivation, and the judge gate layer on top without touching the core path.

| Sub-phase | Deliverable | Acceptance |
|---|---|---|
| **6.0** | Offline ingest → field tree + registry + manifests + metadata sidecar | §3.4 criteria; N-contract round-trip; sidecar stubs marked, enum `status` correct |
| **6.1** | Select + schema generation | Correct cached Pydantic model per product; quote fields + validator present; `NOT_PRESENT` in every compiled enum; derived fields excluded; **mock enums** stand in for `pending` fields |
| **6.2** | Partition + sequential standard extraction | One-topic-per-field asserted; groups extracted in order; evidence quotes enforced; Instructor retry on missing quote |
| **6.3** | Self-consistency tier | Voting fires **only** on `mandatory AND priority>=HIGH_PRIORITY` groups; disagreement → null + flag |
| **6.4** | Computed derivation + reconcile | `computed` fields filled in code from `source_fields`; reconciliation flags stated-vs-derived disagreements |
| **6.5** | Judged derivation (if needed) | 2nd run sees primitives + quotes only, never raw doc; entailment-grounded; votes if high-value |
| **6.6** | Grounding gate + completeness | Judge threshold enforced; null mandatory fields → `review_queue`; result mapped into extended `Asset` |
| **6.7** | Eval / variance harness | Extend `scripts/measure_variance.py`-style metrics: per-field schema-pass, accuracy, hallucination rate, abstention correctness; demonstrate field-level agreement improvement vs current Block 3 |

> **Enum-compile handoff (separate, later):** an offline tool makes LLM API calls to
> fill `description` + `enum.values` in `field_metadata.yaml` and flip `status` to
> `compiled`. It is out of scope for Phase 6 implementation — Phase 6 only builds the
> ground it writes into. Until it runs, 6.1+ use mock enum/description fixtures.

---

## 9. Suggested module layout (for the implementation phase)

```
src/bulk_insert/blocks/extraction/     # Block 3 v2 (or extend extraction.py)
  ingest.py     # §3 offline: parse → union → tree → manifests → round-trip
  tree.py       # FieldNode, tree load/traverse, select-by-lv-path (Stage 1)
  metadata.py   # load/inject field_metadata.yaml; enum + description resolution
  schema.py     # runtime Pydantic gen: quote fields, NOT_PRESENT, validator (Stage 2)
  extract.py    # partition by topic + sequential run + strategy routing (Stages 3–4)
  derive.py     # computed (code) + judged (2nd LLM run) passes (Stages 5–6)
  ground.py     # reconcile + LLM-judge gate + completeness (Stages 7–8)
  # extract_asset(chunks, contract, cfg) -> Asset  remains the public entrypoint (§2.1)
artifacts/      # field_registry.json, field_tree.json, product_manifests.json, field_metadata.yaml
tests/unit/     # round-trip; schema has quote fields + validator + NOT_PRESENT;
                # partition correctness; voting fires only on mandatory+high-priority;
                # MOCK enums/descriptions pending offline compile
```

Prefer stdlib + Pydantic + Instructor; add nothing heavier without justification.
Keep helpers importable for unit testing and declare explicit `__all__` per module,
as the existing blocks do.
