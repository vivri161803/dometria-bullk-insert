# Mission — Dometria Bulk Insert Pipeline

## Purpose

Provide a **stateless, resilient extraction service** that transforms raw documents received from *Dometria Connect* into fully populated, schema-compliant JSON assets — ready for downstream consumption by the Dometria platform.

The service is packaged as a **single self-contained container**: it receives a document (an `id` + raw `text`), runs the full extraction pipeline synchronously, and returns the compiled contract JSON tagged with the same `id`. One container processes one document at a time per request; throughput scales **horizontally** by running more replicas.

## Problem Statement

Dometria Connect ingests documents (text-heavy PDFs, reports, contracts) at scale. Extracting structured data from these documents is:

1. **Computationally expensive** — NLP preprocessing, embedding-based schema matching, and high-capacity LLM calls introduce significant latency (a single document can take tens of seconds to a couple of minutes).
2. **Non-deterministic** — LLM outputs must be constrained to deterministic schemas to guarantee data quality.
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
| **Asset Extraction** (Block 3) | Load the JSON contract schema matching the category path determined by Block 2. Dynamically build a Pydantic model from the contract fields, then pass scored chunks + the dynamic schema to the **heavy LLM** (see [Models](#models)) via Instructor to populate the asset. |
| **Synchronous Orchestration** | A thin `process_document(id, text)` orchestrator chains Block 1 → 2 → 3 in-process and returns the result on the same HTTP request. No broker, no worker, no polling. |
| **Resilience** | Each LLM call retries internally (Instructor `max_retries`); Block 3 re-extracts while confidence is below threshold and keeps the best attempt. On unrecoverable failure the service returns a structured error carrying the `id` so the platform can retry the whole document. |

## Models

The pipeline uses **three** models across its blocks: two LLMs (lightweight + heavy) and one embedding model. All three are configured in `config.py` (`pydantic-settings`) and overridable via `.env` / environment — providers or models swap with **no code change**. The two LLMs are served through **OpenRouter** (OpenAI-compatible); the model string sent is the bare OpenRouter slug (e.g. `openai/gpt-4o-mini`, `mistralai/ministral-3b`).

| Role | Setting | Default | Used by | Purpose |
|---|---|---|---|---|
| **Lightweight LLM** | `MODEL_LIGHTWEIGHT` | `openai/gpt-4o-mini` | Block 1 — relevance scoring + coreference resolution (one call per chunk); Block 2 — `lv1` macro-category classification | High-volume, low-complexity calls where speed and cost dominate. |
| **Heavy LLM** | `MODEL_HEAVY` | `openai/gpt-4o` | Block 3 — asset extraction (populate the dynamic contract schema) | The quality-critical step: high-capacity reasoning over a possibly-large schema. **Output stability is dominated by this model's capability** — a small model varies run-to-run even at temperature 0. |
| **Embedding model** | `EMBEDDING_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | Block 2 — `(lv2, lv3)` leaf selection | Not an LLM: a multilingual sentence-transformer for cosine similarity between chunk texts and candidate category labels. Baked into the container image. |

> The `build_dynamic_model` step (contract JSON → Pydantic model, Block 3) and the spaCy sentence segmentation (Block 1) involve **no model** — they are deterministic Python / NLP and are intentionally excluded from the table above.

### Sampling

To keep extraction reproducible, the same two sampling parameters are applied to **every** LLM call (scoring, `lv1`, extraction):

| Setting | Default | Effect |
|---|---|---|
| `LLM_TEMPERATURE` | `0.0` | Deterministic sampling. At `0.0`, Block 3's confidence retry loop collapses to a single call (re-runs would be identical). |
| `LLM_SEED` | `None` | Optional best-effort reproducibility seed, forwarded only when the provider supports it. |

> Provider, model, and sampling rationale live in `tech-stack.md` (§ *LLM Integration*); this section is the at-a-glance map of **which model does what**.

## Guiding Principles

1. **Schema-first** — Every LLM interaction is constrained by a Pydantic model via Instructor. No free-form JSON parsing.
2. **Stateless by design** — The container holds no per-document state; the `id` is the only correlation handle and persistence belongs to the caller. This is what makes horizontal scaling trivial.
3. **Modularity** — Each processing block is an independent Python module, independently testable. The blocks are composed by a single orchestrator, not wired through a task framework.
4. **Fail-safe over fail-fast** — Within a request, a missing field comes back `null` rather than failing the whole extraction; low confidence never raises. Unrecoverable errors are reported with the `id`, not swallowed.
5. **Push concurrency to the platform** — The app does one thing per request; replicas, queues, and load balancing are infrastructure concerns.
6. **Quality gates** — `ruff`, `mypy --strict`, and `pytest` run as pre-commit hooks. Every module has dedicated quality tests.
