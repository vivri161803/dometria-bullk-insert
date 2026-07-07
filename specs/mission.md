# Mission — Dometria Bulk Insert Pipeline

## Purpose

Provide a **stateless, resilient extraction service** that transforms raw documents received from *Dometria Connect* into fully populated, schema-compliant JSON assets — ready for downstream consumption by the Dometria platform.

The service is packaged as a **single self-contained container**: it receives a document (an `id` + raw `text`), runs the full extraction pipeline synchronously, and returns the compiled contract JSON tagged with the same `id`. One container processes one document at a time per request; throughput scales **horizontally** by running more replicas.

## Problem Statement

Dometria Connect ingests documents (text-heavy PDFs, reports, contracts) at scale. Extracting structured data from these documents is:

1. **Computationally expensive** — NLP preprocessing, embedding-based schema matching, and high-capacity LLM calls introduce significant latency (a single document can take tens of seconds to a couple of minutes).
2. **Non-deterministic** — LLM outputs must be constrained to deterministic schemas to guarantee data quality. Beyond schema-shape, the *field values themselves* drift run-to-run: the model paraphrases, infers unstated values, and varies wording. Reducing this **field-level extraction variance** is a first-class goal — addressed by the tree-driven Block 3 rework (evidence-grounded transcription; see `specs/2026-07-03-tree-driven-extraction.md`).
3. **Fragile without resilience** — network failures, API rate limits, and transient LLM errors must be absorbed gracefully without losing the work already done within a request.

The original design tackled the latency problem with an in-app async task queue (Celery + Redis + Postgres) behind a `202 Accepted` API. **This has been deliberately simplified.** The pipeline blocks are pure functions, so concurrency, queueing, and retries are pushed *down to the deployment platform* rather than reimplemented in the application:

- **Parallelism** → run more container replicas; the platform scheduler distributes load.
- **Retries** → the caller (or load balancer) resubmits the `id` on a retryable failure.
- **Queueing / backpressure** → handled by infrastructure placed *in front* of the container (load balancer, ingress, or an external queue), not by the app.

This keeps the application a small, testable, stateless unit and lets the system engineer own the scaling topology.

## Target Audience

**Internal only** — Dometria Connect calls this service programmatically over HTTP. There are no external-facing APIs or end-user interfaces.

## Interface

A single synchronous HTTP endpoint plus health probes (see `tech-stack.md` for status-code semantics):

| Method & Path | Purpose | Request | Response |
|---|---|---|---|
| `POST /process` | Run the full pipeline on one document. | `{ "id": "<doc-id>", "text": "<raw text>" }` | `200` → `{ "id", "asset": {...}, "confidence": <0–1> }` |
| `GET /health` | Liveness — process is up. | — | `200` |
| `GET /ready` | Readiness — spaCy + embedding model are warmed and the container can serve. | — | `200` once warm |

The `id` is opaque to the service and is **echoed back on every response**, success or error, so the caller can correlate results without the service holding any state. **The service persists nothing** — storing the compiled JSON is the caller's responsibility.

## Core Capabilities

| Capability | Description |
|---|---|
| **Chunking & Scoring** (Block 1) | Split raw text into semantically meaningful, overlapping chunks via spaCy; resolve coreferences and score each chunk's relevance in a single **lightweight LLM** call (see [Models](#models)). |
| **Contract Determination** (Block 2) | Derive the target contract category path (`lv1/lv2/lv3`) using a two-stage hybrid approach: (1) a constrained **lightweight LLM** call classifies the document into one of 3 level-1 macro-categories, then (2) cosine similarity between **embedding-model** vectors of the chunks and the candidate category labels — weighted by each chunk's relevance score — selects the winning `(lv2, lv3)` pair via majority vote. No knowledge graph is used; the category hierarchy is a simple 3-level tree traversed via dictionary lookup. |
| **Asset Extraction** (Block 3) | **Tree-driven, evidence-grounded extraction** targeting field-level variance. Select this product's fields from a prebuilt **master field tree** (built offline from the union of all contracts) using Block 2's `(lv1/lv2/lv3)` path; generate a per-product Pydantic model — every extracted field paired with a **verbatim evidence quote**, every closed vocabulary carrying a `NOT_PRESENT` abstention option — and populate it with the **heavy LLM** via Instructor. Fields are partitioned by a **topic** tag and extracted **sequentially**; mandatory/high-priority groups use **self-consistency voting**. Arithmetic/cross-field values are **derived in code**, not asked of the model; a final LLM-as-judge entailment gate and mandatory-completeness check route failures to a review queue. Enum vocabularies, field descriptions, and topic tags live in a runtime-injected, fully-editable `field_metadata.yaml` sidecar (+ `topics.yaml`), **populated by an offline compile step** (see [Offline metadata compile](#offline-metadata-compile-phase-68)). See `specs/2026-07-03-tree-driven-extraction.md`. |
| **Synchronous Orchestration** | A thin `process_document(id, text)` orchestrator chains Block 1 → 2 → 3 in-process and returns the result on the same HTTP request. No broker, no worker, no polling. |
| **Resilience** | Each LLM call retries internally (Instructor `max_retries`), and Block 3's evidence-quote `@model_validator` drives a bounded re-prompt when a populated field lacks its verbatim quote; mandatory/high-priority groups are re-run under **self-consistency voting**. Low confidence and missing/ungrounded fields never raise — they are nulled, flagged, and routed to a `review_queue`. On unrecoverable failure the service returns a structured error carrying the `id` so the platform can retry the whole document. |

## Models

The pipeline uses **three** models across its blocks: two LLMs (lightweight + heavy) and one embedding model. All three are configured in `config.py` (`pydantic-settings`) and overridable via `.env` / environment — providers or models swap with **no code change**. The two LLMs are served through **OpenRouter** (OpenAI-compatible); the model string sent is the bare OpenRouter slug (e.g. `openai/gpt-4o-mini`, `mistralai/ministral-3b`).

| Role | Setting | Default | Used by | Purpose |
|---|---|---|---|---|
| **Lightweight LLM** | `MODEL_LIGHTWEIGHT` | `openai/gpt-4o-mini` | Block 1 — relevance scoring + coreference resolution (one call per chunk); Block 2 — `lv1` macro-category classification | High-volume, low-complexity calls where speed and cost dominate. |
| **Heavy LLM** | `MODEL_HEAVY` | `openai/gpt-4o` | Block 3 — asset extraction: grounded per-topic transcription, self-consistency voting (`SELF_CONSISTENCY_N`), judged derivation, LLM-as-judge grounding gate | The quality-critical step: high-capacity reasoning over a possibly-large schema. **Output stability is dominated by this model's capability** — a small model varies run-to-run even at temperature 0. The tree-driven Block 3 spends *more* heavy calls per document (voting + judge passes) deliberately, to buy reproducibility; the standard path assumes a frontier model (reason-then-format dropped). |
| **Embedding model** | `EMBEDDING_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | Block 2 — `(lv2, lv3)` leaf selection | Not an LLM: a multilingual sentence-transformer for cosine similarity between chunk texts and candidate category labels. Baked into the container image. |

> The Block 3 schema machinery — the **offline ingest** (`ingest.py`: contracts → master field tree + `artifacts/`) and the **runtime schema generation** (`schema.py:build_extraction_model`: field nodes → per-product Pydantic model with paired quote fields + `NOT_PRESENT` enums) — plus the spaCy sentence segmentation (Block 1) involve **no model**: they are deterministic Python / NLP and are intentionally excluded from the table above. (The enum/description/topic *compile* step, which does call an LLM, is a separate **offline** tool — not part of the runtime; see below.)

### Offline metadata compile (Phase 6.8)

The `field_metadata.yaml` sidecar is **seeded** by the ingest with stubs (`description: null`, enum `status: pending`, `# TODO: llm-compile` markers) and then **filled offline** by `blocks/extraction/compile.py` (CLI: `scripts/compile_metadata.py`). This is a **one-off, build-time** step — gated behind `OPENROUTER_API_KEY` like the integration tests — and is **not part of the request path**; the runtime service never calls it.

Per pending field, batched Instructor calls produce, **strictly in Italian**: a concise `description` (extraction-prompt context), a proposed grouping `topic` name, and — for closed-vocab (`select`/`boolean`) candidates — a refined enum `values` list that *blesses/refines* the seeded contract options (never invents wildly; `NOT_PRESENT` is stripped, added back only at runtime), plus an `applicable` decision (`applicable=false` ⇒ `enum.status: not_applicable`). Each compiled field also records a boolean **`unsure`** self-flag so a reviewer can inspect only the low-confidence subset (`grep 'unsure: true'`).

Proposed topic names are deduped into a compact **topics dictionary** (`topic → id`) written to `artifacts/topics.yaml` (user-editable); each field's `topic` is rewritten to that integer id, which `metadata.py` resolves back to a name at runtime (string / unmapped-id topics fall back to the source default). This id-resolution lookup is the **only** runtime change the compile introduces — enum and description consumption were already runtime-ready.

Compiling is **approved-by-default** (writes `description` + `values`, flips `enum.status` to `compiled`/`not_applicable`, records `unsure` — no separate approval step) and **idempotent** (skips already-compiled fields unless `--force`; the global `enum_compile_status` flips to `compiled` once none remain pending; re-runs reuse existing `topics.yaml` ids). The CLI defaults to `--dry-run` (prints the diff, writes nothing); `--write` applies it, `--field <name>` scopes to one field. The compile model is `MODEL_COMPILE` (empty → falls back to `MODEL_HEAVY`), with `COMPILE_BATCH_SIZE` fields per call. Until a field is compiled, the runtime treats its `pending` enum as free-text.

### Sampling

To keep extraction reproducible, the same two sampling parameters are applied to **every** LLM call (scoring, `lv1`, extraction):

| Setting | Default | Effect |
|---|---|---|
| `LLM_TEMPERATURE` | `0.0` | Deterministic sampling. The tree-driven Block 3 has no confidence retry loop: the standard path is one call per topic group, and self-consistency voting deliberately runs `SELF_CONSISTENCY_N` calls on high-value groups regardless of temperature. |
| `LLM_SEED` | `None` | Optional best-effort reproducibility seed, forwarded only when the provider supports it. |

> Provider, model, and sampling rationale live in `tech-stack.md` (§ *LLM Integration*); this section is the at-a-glance map of **which model does what**.

## Guiding Principles

1. **Schema-first** — Every LLM interaction is constrained by a Pydantic model via Instructor. No free-form JSON parsing.
2. **Stateless by design** — The container holds no per-document state; the `id` is the only correlation handle and persistence belongs to the caller. This is what makes horizontal scaling trivial.
3. **Modularity** — Each processing block is an independent Python module, independently testable. The blocks are composed by a single orchestrator, not wired through a task framework.
4. **Fail-safe over fail-fast** — Within a request, a missing field comes back `null` rather than failing the whole extraction; low confidence never raises. Unrecoverable errors are reported with the `id`, not swallowed.
5. **Push concurrency to the platform** — The app does one thing per request; replicas, queues, and load balancing are infrastructure concerns.
6. **Quality gates** — `ruff`, `mypy --strict`, and `pytest` run as pre-commit hooks. Every module has dedicated quality tests.
