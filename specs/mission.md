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
| `POST /process` | Run the full pipeline on one document. | `{ "id": "<doc-id>", "text": "<raw text>" }` | `200` → `{ "id", "asset": {...}, "confidence": <0–1>, "completeness": <0–1>, "_models": {...} }` |
| `GET /health` | Liveness — process is up. | — | `200` |
| `GET /ready` | Readiness — spaCy + embedding model are warmed and the container can serve. | — | `200` once warm |
| `GET /config/models` | Report the LLM/embedding slugs in effect on this container. | — | `200` → `{ "lightweight", "heavy", "embedding" }` |
| `POST /config/model` | Swap one LLM slug on the running container (one role per request). | `{ "role": "heavy"\|"lightweight", "model": "<slug>" }` | `200` → `{ "lightweight", "heavy", "embedding" }` |

> **Runtime model override.** `POST /config/model` lets an operator change `MODEL_HEAVY` /
> `MODEL_LIGHTWEIGHT` on a running container without a redeploy (send one request per model
> with the bare OpenRouter slug). The override is **per-replica and ephemeral** — it affects
> only the container that received it and resets on restart; it is not shared across replicas
> (POST every replica, run a single replica, or set the env var + redeploy for a durable,
> cluster-wide change). The embedding model is baked into the image and excluded. Gated by
> `RUNTIME_MODEL_OVERRIDE_ENABLED` (default on); protect with the Phase 9 API-key auth.

The `id` is opaque to the service and is **echoed back on every response**, success or error, so the caller can correlate results without the service holding any state. **The service persists nothing** — storing the compiled JSON is the caller's responsibility.

## API Guide (Phase 7 — testing)

The service is a thin FastAPI adapter (`src/bulk_insert/api/`) over `pipeline.process_document`. It adds **no pipeline logic**: it maps `PipelineResult.error.category` to an HTTP status code and echoes `id`. This section is a hands-on guide for exercising it locally.

### Running the service

`api/app.py` exposes an application **factory** `create_app(cfg)` (no module-level `app`), so launch uvicorn with `--factory`:

```bash
# 1. Install deps + create the venv (also exposes bulk-insert-cli)
uv sync

# 2. Provide the OpenRouter key the pipeline needs for live LLM calls
echo "OPENROUTER_API_KEY=sk-or-..." >> .env    # or export it in the shell

# 3. Serve on :8000 — the factory's lifespan warms spaCy + the embedding
#    model and loads/validates the offline artifacts before serving.
uv run uvicorn "bulk_insert.api.app:create_app" --factory --port 8000

# interactive OpenAPI docs, once up:  http://localhost:8000/docs
```

Warm-up runs **once at startup**: a missing/invalid artifact (uncompiled sidecar, absent field tree) makes startup fail and `/ready` never turns green — it does **not** fail the first `/process` request. Because the pipeline calls OpenRouter, a real `POST /process` needs a valid `OPENROUTER_API_KEY`; the health/readiness probes do not.

### Endpoints

| Method & Path | Purpose | Auth | Body |
|---|---|---|---|
| `POST /process` | Run the full pipeline on one document (synchronous, seconds→minutes). | none (Phase 9) | `ProcessRequest` |
| `GET /health` | Liveness — the process is up. Always `200`. | none | — |
| `GET /ready` | Readiness — `200` once models **and** offline artifacts are warmed/valid, else `503`. | none | — |
| `GET /config/models` | The LLM/embedding slugs currently in effect on this container. | none (Phase 9) | — |
| `POST /config/model` | Swap one LLM slug at runtime (per-replica, ephemeral; one role per request). | none (Phase 9) | `SetModelRequest` |

`POST /process` runs as a synchronous `def` handler, so FastAPI executes it in the threadpool; per-container in-flight concurrency is capped so one replica doesn't run many heavy extractions at once.

### Request schema — `ProcessRequest`

```jsonc
{
  "id": "doc-123",     // opaque correlation handle, echoed back verbatim
  "text": "..."        // raw document text; must be non-empty (min_length=1)
}
```

A missing/blank `text` is rejected: an **empty string** fails Pydantic validation at the FastAPI boundary (`422` with FastAPI's validation envelope), while **whitespace-only** text passes validation but the pipeline short-circuits to a `bad_input` error → **`400`** (see the error table below).

### Success response — `ProcessResponse` (`200`)

The body carries the **full Block 3 v2 `Asset`** under `asset`, with `confidence` and `completeness` also **lifted to the top level** and a `_models` observability block:

```jsonc
{
  "id": "doc-123",
  "confidence": 0.82,          // lifted: grounding pass-rate × completeness
  "completeness": 0.75,        // lifted: share of mandatory fields filled (null if product has none)
  "asset": {
    "contract_id": "162_restaurant",
    "data": {                  // the schema-compliant record (field key → value)
      "nome_attivita": "Trattoria da Mario",
      "posti_a_sedere": 48,
      "anno_apertura": 2019
    },
    "confidence": 0.82,        // same value, nested
    "completeness": 0.75,      // same value, nested
    "evidence": {              // per-field verbatim source quote (may be null)
      "posti_a_sedere": "sala interna con 48 coperti"
    },
    "flags": {                 // per-field agreement/grounding/normalization notes (may be null)
      "anno_apertura": { "normalization": { "status": "ok" } }
    },
    "review_queue": [          // mandatory field keys left unfilled (may be null / empty)
      "codice_fiscale"
    ]
  },
  "_models": {
    "lightweight": "openai/gpt-4o-mini",
    "heavy": "openai/gpt-4o",
    "embedding": "paraphrase-multilingual-MiniLM-L12-v2"
  }
}
```

> **A `200` can still need review.** Low `confidence` **and** a non-empty `review_queue` are *successful* extractions — they surface in the body, never as a non-2xx. The API adds no top-level review envelope; callers read `asset.review_queue`. Field values are the extracted record, not guarantees — `evidence`/`flags`/`review_queue` are the audit trail. `data` keys and mandatory set vary per resolved contract (`contract_id`); the values above are illustrative.

### Error response — `ErrorResponse`

Every non-2xx (except FastAPI's own request-validation `422`) echoes `id` and a structured `PipelineError`:

```jsonc
{
  "id": "doc-123",
  "error": {
    "stage": "contract",        // input | preprocess | contract | extract
    "category": "unprocessable",// bad_input | unprocessable | retryable | server
    "message": "no qualifying chunk: document cannot be categorized"
  }
}
```

Status mapping (`routes._STATUS_MAP`, keyed on `error.category`):

| Category | Status | When |
|---|---|---|
| `bad_input` | `400` | Whitespace-only `text` — the pipeline short-circuits before any block. |
| `unprocessable` | `422` | `ContractDeterminationError` — no chunk clears `CONTRACT_MIN_SCORE`; document can't be categorized. |
| `retryable` | `502` | Transient upstream OpenAI/`httpx` failure — safe to resubmit the same `id`. |
| `server` | `500` | `ContractNotFoundError` / `AssetExtractionError` (missing/malformed schema) or any uncaught error. |

(FastAPI request-validation failures — e.g. empty `text`, missing `id`, malformed JSON — return `422` with FastAPI's default `{"detail": [...]}` envelope, produced *before* the handler runs, so they do **not** carry the `PipelineError` shape.)

### `curl` recipes

```bash
# Liveness — up immediately, even mid warm-up.
curl -s http://localhost:8000/health
# {"status":"ok"}

# Readiness — 503 until warm, then 200. Gate traffic on this.
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/ready

# Happy path from a file (jq pretty-prints; -w shows the status code).
curl -s -w '\nHTTP %{http_code}\n' -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' \
  --data "$(jq -Rs '{id:"doc-123", text:.}' < sample.txt)"

# Inline text.
curl -s -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' \
  -d '{"id":"doc-1","text":"Trattoria da Mario, 48 coperti, aperta nel 2019."}'

# bad_input → 400 (whitespace-only text passes Pydantic, pipeline rejects it).
curl -s -w '\nHTTP %{http_code}\n' -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' -d '{"id":"blank","text":"   "}'

# Request-validation 422 (empty text fails min_length=1 at the boundary).
curl -s -w '\nHTTP %{http_code}\n' -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' -d '{"id":"empty","text":""}'
```

### Testing without live LLM calls

For deterministic tests, drive the app with the FastAPI `TestClient` and mock `process_document` — no network, no key needed (mirrors the Phase 7 route tests):

```python
from fastapi.testclient import TestClient
from bulk_insert.api.app import create_app

client = TestClient(create_app())          # lifespan warm-up runs on enter
resp = client.post("/process", json={"id": "t1", "text": "hello world"})
assert resp.json()["id"] == "t1"
```

Patch `bulk_insert.api.routes.process_document` to return a crafted `PipelineResult` to assert the `category → status` mapping and the `id` echo without hitting the pipeline. For a live end-to-end check, the `bulk-insert-cli` harness (`uv run bulk-insert-cli --file sample.txt --json`) runs the same orchestrator the endpoint wraps.

## Core Capabilities

| Capability | Description |
|---|---|
| **Chunking & Scoring** (Block 1) | Split raw text into semantically meaningful, overlapping chunks via spaCy; resolve coreferences and score each chunk's relevance in a single **lightweight LLM** call (see [Models](#models)). |
| **Contract Determination** (Block 2) | Derive the target contract category path (`lv1/lv2/lv3`) using a two-stage hybrid approach: (1) a constrained **lightweight LLM** call classifies the document into one of 3 level-1 macro-categories, then (2) cosine similarity between **embedding-model** vectors of the chunks and the candidate category labels — weighted by each chunk's relevance score — selects the winning `(lv2, lv3)` pair via majority vote. No knowledge graph is used; the category hierarchy is a simple 3-level tree traversed via dictionary lookup. |
| **Asset Extraction** (Block 3) | **Tree-driven, evidence-grounded extraction** targeting field-level variance. Select this product's fields from a prebuilt **master field tree** (built offline from the union of all contracts) using Block 2's `(lv1/lv2/lv3)` path; generate a per-product Pydantic model — every extracted field paired with a **verbatim evidence quote**, every closed vocabulary carrying a `NOT_PRESENT` abstention option — and populate it with the **heavy LLM** via Instructor. Fields are partitioned by a **topic** tag and extracted **sequentially**; mandatory/high-priority groups use **self-consistency voting**. Arithmetic/cross-field values are **derived in code**, not asked of the model; the raw record is then **normalized** (deterministic per-request canonicalization) and two opt-in, quote-bounded escape hatches finish it (present-but-unlisted enums; enumerated-not-totaled counts) before a final LLM-as-judge entailment gate and mandatory-completeness check route failures to a review queue. Enum vocabularies, field descriptions, and topic tags live in a runtime-injected, fully-editable `field_metadata.yaml` sidecar (+ `topics.yaml`), **populated by an offline compile step** (see [Offline metadata compile](#offline-metadata-compile-phase-68)). See `specs/2026-07-03-tree-driven-extraction.md` and [Post-extraction record finishing](#post-extraction-record-finishing-phase-610). |
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

### Post-extraction record finishing (Phase 6.10)

After the per-topic extraction produces raw primitives + quotes, three **in-request** stages finish the record before the grounding gate — each independently toggleable and evidence-bounded. Full spec: `specs/2026-07-07-normalization.md`.

- **Stage 4b — Normalization** (`normalize.py`, deterministic, **no LLM**). A type-driven registry keyed by `field_type` canonicalizes values in place: `date` → ISO `YYYY-MM-DD`, `text` → whitespace-tidied, and Italian numeric strings → `float`/`int`. Because the generated schema already types numerics and enums, the numeric normalizers are a **defensive safety net** — the real work is dates and text. It **cleans** single fields, never fabricating from siblings, so it cannot lower accuracy; an unparseable value is **kept and flagged**, never nulled. Runs before derivation so any computed rule / reconcile sees canonical numbers. Off-switch `NORMALIZE_ENABLED`. *(mq/ha unit resolution is deferred — a type-driven table is enough given typed extraction.)*
- **Stage 4c — `OUT_OF_VOCAB` resolution** (`resolve.py`, opt-in LLM, quote-bounded). Every compiled enum carries a third sentinel (beside `NOT_PRESENT`) the extractor emits when the field is clearly present but no listed value fits. Unlike `NOT_PRESENT` it is **not** validator-exempt, so it always carries a verbatim quote; a scoped judge then infers the value from **the field description + its own quote only, never the raw document** — re-mapping to the closest listed value when one fits, else emitting faithful Italian free-text (voted for mandatory/high-priority). Inferred-from-quote ⇒ passes the entailment gate by construction, turning a silent grounding null into a resolved value plus an **OOV-rate** signal (surfaced in `scripts/eval_extraction.py`) that flags a wrong compiled enum axis. Off-switch `OOV_ENABLED` (disabled ⇒ today's behaviour: sentinel → `null`).
- **count / aggregate** (§10, opt-in per field). A `numero_X` field whose document *enumerates* items rather than stating a total has no single span for the number. A per-field `aggregate: {rule: count, item_hint}` marker makes `schema.build_extraction_model` synthesize a grounded companion `{field}__items: list[{value, quote}]` (plain transcription) and declares the scalar as a **`computed` `count`** over it — so it drops out of extraction and is counted in code: deterministic, grounded per-item, and mandatory-satisfiable (no longer routed to `review_queue`). The companion is internal — it collapses to the count in `Asset.data`, its item quotes surfacing under `Asset.evidence`. This is the project's transcribe-then-derive principle applied one level down.

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
