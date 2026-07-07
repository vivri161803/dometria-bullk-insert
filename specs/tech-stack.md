# Tech Stack — Dometria Bulk Insert Pipeline

## Runtime & Language

| Component | Choice | Rationale |
|---|---|---|
| Language | **Python 3.13** | Latest stable release; best ecosystem for NLP + LLM tooling. |
| Package manager | **uv** | Fast dependency resolution, lockfile support, built-in venv management. |
| Virtual environment | Managed by `uv` (`uv venv`, `uv sync`) | Single tool for deps + env. |

---

## Application Framework

| Component | Choice | Rationale |
|---|---|---|
| HTTP API | **FastAPI** | Pydantic-integrated, auto-generated OpenAPI docs. Internal-only, but typed API contracts simplify integration with Dometria Connect. Exposes a single synchronous `POST /process` endpoint plus health probes. |
| ASGI server | **uvicorn** | Standard production server for FastAPI. |

> **Endpoint is synchronous (`def`, not `async def`).** The pipeline blocks are blocking calls (Instructor sync client, spaCy, sentence-transformers). FastAPI runs a `def` path operation in a worker thread, keeping the event loop free for health checks. An `async def` endpoint would block the loop on every LLM call. Per-container concurrency is kept low (a handful of in-flight requests) so one replica does not attempt many heavy extractions at once; throughput scales by adding replicas, not threads.

---

## NLP & Text Processing

| Component | Choice | Rationale |
|---|---|---|
| Sentence segmentation | **spaCy** (blank pipeline + sentencizer) | Lightweight, no model download required. Only used for sentence boundary detection; all semantic processing is delegated to LLMs. |
| Coreference resolution | **LLM-based** (combined with scoring) | Performed during the Block 1 scoring call — the LLM both rates chunk relevance and rewrites the text with pronouns resolved. Eliminates the need for a separate NLP coreference library (e.g. `coreferee`, which lacks Python 3.13 / spaCy 3.8 support). |
| Embeddings (contract determination) | **sentence-transformers** (`paraphrase-multilingual-MiniLM-L12-v2`) | Multilingual 384d model for cosine similarity between chunk texts and Italian category labels in Block 2. Loaded lazily via `@lru_cache`, fast enough for real-time inference on ~20 candidates. **Baked into the container image** at build time (~470 MB) so cold-start has no network dependency. |

> **Removed dependency:** The original architecture proposed a knowledge graph (KG) engine for "graph matching" of category levels 2 and 3. This was replaced by direct dictionary traversal of the `CATEGORY_TREE` (a 3-level nested dict with 3 roots, 12 level-2 nodes, and 97 level-3 leaves). The tree structure is too simple to justify a graph engine — a simple `dict[lv1][lv2] → list[lv3]` lookup achieves the same result with zero external dependencies.

---

## LLM Integration

| Component | Choice | Rationale |
|---|---|---|
| LLM provider gateway | **OpenRouter** | Unified API for multiple LLM providers; OpenAI-compatible. |
| Client SDK | **openai** (Python SDK) | OpenRouter exposes an OpenAI-compatible endpoint; no need for a custom client. |
| Structured output | **instructor** + **Pydantic v2** | Constrains every LLM call to a typed schema. Eliminates free-form JSON parsing and guarantees contract adherence. |
| Lightweight model (scoring) | **GPT-4o-mini** (via OpenRouter) | Fast, cheap — ideal for chunk relevance scoring (Block 1) and lv1 classification (Block 2). |
| Heavy model (extraction) | **GPT-4o** (via OpenRouter) | High-capacity reasoning — required for accurate asset extraction (Block 3). |

> Provider and model names are configured in `config.py` (`pydantic-settings`) and overridable via `.env` / environment variables, so providers or models can be swapped with no code changes.

---

## Asset Extraction (Block 3) — tree-driven, evidence-grounded

> Full spec: `specs/2026-07-03-tree-driven-extraction.md`. Reworks Block 3 to attack **field-level extraction variance**.

| Component | Choice | Rationale |
|---|---|---|
| Field selection | **Static master field tree** (built offline from all contracts) | The 97 contracts are unioned once into a `field_tree.json` + per-product `product_manifests.json`. At runtime, Block 2's `(lv1/lv2/lv3)` path selects the product's fields by traversal — no per-request contract globbing. Cached by field-set signature. |
| Per-product schema | **`pydantic.create_model` at runtime** | Each extracted field is paired with a `{name}_quote` evidence field; every closed enum gets a `NOT_PRESENT` abstention value; a `@model_validator` rejects non-null fields missing their quote (drives Instructor retry). Derived fields are excluded. |
| Grouping & execution | **`topic` tag → sequential groups** | Fields partition cleanly by a one-per-field topic tag; groups extract **sequentially** within the request (no `asyncio` fan-out — consistent with the synchronous-container model below). `priority` orders the queue and gates voting; it is not an execution dependency. |
| Faithfulness | **Evidence quotes + self-consistency voting + LLM-as-judge gate** | Mandatory/high-priority groups vote (`SELF_CONSISTENCY_N`, disagreement → null + flag); a final entailment gate (`JUDGE_THRESHOLD` ~0.7) drops/flags ungrounded values; null mandatory fields route to a `review_queue`. |
| Derivation | **Code (`computed`) + isolated 2nd LLM run (`judged`)** | Arithmetic/temporal/cross-field values are computed in Python, never asked of the extractor. The rare reasoning fields get a second LLM pass over **primitives + quotes only, never the raw document**. |
| Field metadata | **`field_metadata.yaml` sidecar (runtime-injected, fully editable)** | Enum vocabularies, descriptions, topics, priorities, and derivations live in YAML keyed by field — not baked into code. **Enum vocabularies and descriptions are compiled offline in a later step via an LLM API call**; until then the runtime treats `pending` enums as free-text and tests inject mocks. The file stays modifiable after compilation. |

> **No DAG.** Field selection replaces conditional node-skipping (irrelevant fields never enter the pipeline); a topic partition replaces ordered execution. There is no topological sort, dependency edge, or workflow engine — deliberately.

---

## Orchestration & Concurrency

| Component | Choice | Rationale |
|---|---|---|
| In-process composition | **`process_document(id, text, cfg)` orchestrator** | Chains Block 1 → 2 → 3 synchronously within a single HTTP request. No message broker, no background worker, no task framework. |
| Concurrency model | **Horizontal — more container replicas** | The pipeline blocks are pure, stateless functions, so parallelism is achieved by running more containers behind a load balancer/autoscaler rather than an in-app worker pool. |
| Retries | **Instructor `max_retries` (per LLM call) + Block 3 confidence loop; platform-level resubmission** | Transient LLM errors are retried inside each call. Whole-document retries are the caller's / load balancer's responsibility (resubmit the `id`). |

> **Why this replaces Celery + Redis.** The original plan used Celery workers, a Redis broker, and a `202 Accepted` + polling API to avoid blocking a single long-lived API process under load. With a stateless container that does one document per request, that machinery is redundant: the deployment platform provides queueing, parallelism, and retries. Celery, Redis, and the broker/result-backend were **removed from the stack**.

---

## Persistence & State

| Component | Choice | Rationale |
|---|---|---|
| Application state | **None — the container is stateless** | The service holds no per-document state. The `id` is echoed on every response as the only correlation handle. |
| Result persistence | **Caller's responsibility** | The compiled contract JSON is returned in the HTTP response; storing it (DB, object store, queue) is owned by Dometria Connect / the surrounding infrastructure. |

> **Removed dependency:** PostgreSQL, SQLAlchemy, asyncpg, and Alembic were dropped. There is no job/block-result table because there are no long-lived jobs to track — a request either returns the asset or returns a structured error carrying the `id`. Persisting state inside the service would have re-coupled the unit that horizontal scaling is meant to keep independent.

---

## Testing & Code Quality

| Component | Choice | Rationale |
|---|---|---|
| Test framework | **pytest** | Standard; supports fixtures, parametrization. Unit tests mock the Instructor client; live LLM tests are gated behind `-m integration`. |
| Linter / formatter | **ruff** | Extremely fast; replaces flake8, isort, black in a single tool. |
| Pre-commit hooks | **pre-commit** | Runs `ruff check`, `ruff format`, `mypy src`, and `pytest tests/unit` before every commit. |
| Type checking | **mypy** (strict mode) | Catches type errors early; complements Pydantic's runtime validation. |

---

## Observability

| Component | Choice | Rationale |
|---|---|---|
| Logging | **Python `logging`** (stdlib) | Simple, no external dependencies. Structured log messages carry the document `id` and block context. |
| Log format | **JSON lines** (via `python-json-logger`) | Machine-parseable; ready for future log aggregation (ELK, Loki). Logs go to stdout/stderr for the container runtime to collect. |

> Future phases may introduce OpenTelemetry or Sentry. The logging foundation is designed to be forwards-compatible.

---

## Containerization & Deployment

The service ships as **one container image** running the FastAPI app under uvicorn.

| Component | Choice | Rationale |
|---|---|---|
| Container runtime | **Docker** | Single multi-stage `Dockerfile` (deps → app). One image, one process — no separate API/worker targets. |
| Embedded assets | **Embedding model + `contracts/` baked into the image** | No network dependency at cold-start; the image is fully self-contained except for the LLM API key. |
| Injected config | **`OPENROUTER_API_KEY` + model/URL overrides via env/secrets** | Secrets are never baked into the image; everything else has sane defaults in `config.py`. |
| Scaling | **N stateless replicas behind a load balancer / autoscaler** | The system engineer owns the topology (Kubernetes Deployment + HPA, ECS service, Cloud Run, etc.). The app makes no assumption about it. |

> **Deployment contract (handoff to the system engineer):**
> - The container exposes `POST /process`, `GET /health` (liveness), `GET /ready` (readiness — green once models are warmed).
> - It is **stateless and horizontally scalable** — run as many replicas as needed; no shared state, no sticky sessions, no DB.
> - **Requests are long-running** (tens of seconds to a couple of minutes per document). Client, ingress, and load-balancer **timeouts must be sized accordingly**, and the load balancer should perform the whole-document retry on a `502`.
> - The only required runtime input is the `OPENROUTER_API_KEY` secret and outbound network access to OpenRouter.

---

## Project Structure (target layout)

```
bulk_insert/
├── contracts/              # 97 JSON contract files ({id}_{lv3_code}.json) — source for the field tree
├── artifacts/              # offline build output: field_registry.json, field_tree.json,
│                           #   product_manifests.json, field_metadata.yaml (baked into image)
├── specs/
│   ├── mission.md
│   ├── tech-stack.md
│   ├── roadmap.md
│   └── 2026-07-03-tree-driven-extraction.md   # Block 3 v2 (variance reduction)
├── src/
│   └── bulk_insert/
│       ├── __init__.py
│       ├── api/            # FastAPI app, routes, request/response schemas
│       ├── blocks/         # Block 1, 2, 3 modules (Block 3 = tree-driven extraction:
│       │                   #   ingest, tree, metadata, schema, extract, derive, ground)
│       ├── models/         # Pydantic models (contracts, chunks, assets)
│       ├── pipeline.py     # process_document() — composes Block 1 → 2 → 3
│       └── config.py       # Settings via pydantic-settings
├── tests/
│   ├── unit/
│   └── integration/
├── pyproject.toml
├── .pre-commit-config.yaml
├── Dockerfile
└── README.md
```
