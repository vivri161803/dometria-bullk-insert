# Roadmap — Dometria Bulk Insert Pipeline

> Each phase is designed to be **independently deliverable and testable**.
> Phases are sequential — each builds on the previous one.

---

## Phase 0 — Project Scaffolding

**Goal:** Establish the project skeleton, tooling, and developer workflow before any application logic.

| Task | Detail |
|---|---|
| Initialize `pyproject.toml` with `uv` | Define project metadata, Python 3.13 requirement, initial dependencies. |
| Create source layout | `src/bulk_insert/` package with `__init__.py`, `config.py`. |
| Create test layout | `tests/unit/`, `tests/integration/` with a placeholder test. |
| Configure `ruff` | `pyproject.toml` — linting + formatting rules. |
| Configure `mypy` | `pyproject.toml` — strict mode. |
| Configure `pre-commit` | `.pre-commit-config.yaml` — runs `ruff check`, `ruff format`, `pytest tests/unit`. |
| Add `.gitignore` | Standard Python + uv + IDE ignores. |
| Verify | `uv sync`, `ruff check .`, `mypy src/`, `pytest` — all pass on empty project. |

**Exit criteria:** A developer can clone, `uv sync`, and run all quality gates with zero errors.

---

## Phase 1 — Pydantic Models & Config

**Goal:** Define all shared data structures and configuration before implementing any logic.

| Task | Detail |
|---|---|
| `src/bulk_insert/config.py` | `pydantic-settings` based config: OpenRouter API key, Redis URL, Postgres DSN, model names, chunk size, overlap size. |
| `src/bulk_insert/models/chunk.py` | `Chunk` model: text, index, overlap metadata, coreference-resolved flag, relevance score. |
| `src/bulk_insert/models/contract.py` | `Contract` model: dynamic JSON schema representation (lv1, lv2, lv3 fields). |
| `src/bulk_insert/models/asset.py` | `Asset` model: populated contract; maps to final extraction output. |
| `src/bulk_insert/models/job.py` | `Job` model: job ID, status enum (PENDING → PREPROCESSING → CONTRACTING → EXTRACTING → DONE → FAILED), timestamps, block results. |
| Unit tests | Serialization round-trips, validation edge cases, enum transitions. |

**Exit criteria:** All models serialize/deserialize correctly; config loads from `.env`.

---

## Phase 2 — Block 1: Preprocessing

**Goal:** Implement sentence segmentation, chunking, and combined LLM scoring + coreference resolution as a standalone module.

| Task | Detail |
|---|---|
| `src/bulk_insert/blocks/preprocessing.py` | spaCy pipeline: sentence segmentation → chunking with overlap. No separate coref step — see below. |
| Combined scoring + coreference resolution | Single Instructor call per chunk to GPT-4o-mini. Input: chunk text. Output: `ChunkAnalysis` Pydantic model (`score` int 1–10, `resolved_text` str). The LLM both rates relevance and rewrites the text with pronouns replaced by their explicit referents. `Chunk.text` is updated with the resolved text; `Chunk.is_coref_resolved` is set `True`. |
| Unit tests | Test chunking on sample texts; test overlap boundaries; test combined score + coref call with mocked LLM. |
| Integration test | End-to-end with real LLM call on a small sample (gated behind `pytest.mark.integration`). |

**Exit criteria:** Given raw text, produces a list of scored, coreference-resolved `Chunk` objects with correct overlap metadata.

---

## Phase 3 — Block 2: Contract Determination

**Goal:** Determine the target category path (`lv1/lv2/lv3`) from scored chunks using a two-stage hybrid approach — no knowledge graph.

> **Architecture note:** The original architecture (`architecture.md`) envisioned a "graph matching" approach for lv2/lv3 deduction. Since the category structure is a simple 3-level tree with only 3 roots (not a complex graph), we adopted a simpler and more effective approach: LLM for lv1 → cosine similarity + weighted majority voting for lv2+lv3.

| Task | Detail |
|---|---|
| `src/bulk_insert/blocks/contract.py` | Two-stage pipeline: **Stage 1** — Instructor-constrained LLM call (GPT-4o-mini) classifies document into one of 3 `Level1Literal` values. **Stage 2** — Sentence-transformers (`paraphrase-multilingual-MiniLM-L12-v2`) encodes chunk texts and candidate category labels; cosine similarity + relevance-weighted majority vote selects the winning `(lv2, lv3)` pair. |
| Chunk filtering | Filter chunks by `relevance_score >= CONTRACT_MIN_SCORE` (default 5), select top-k (default 5) by descending score. |
| Category helpers | `get_candidate_paths(lv1)` and `get_candidate_labels(lv1)` expand the `CATEGORY_TREE` into candidate `(lv2, lv3)` pairs with human-readable labels for embedding. |
| Unit tests | Test chunk filtering, mocked LLM lv1 extraction, real embedding-based voting (restaurant/villa texts), end-to-end with mocked LLM + real embeddings. |

**Output:** `Contract(lv1, lv2, lv3, schema_def={})` — the category path is determined; schema loading is deferred to Block 3.

**Exit criteria:** Given scored chunks, outputs the correct `Contract` with the correct category path. The `lv3` code maps 1:1 to a contract JSON file in `contracts/`.

---

## Phase 4 — Block 3: Asset Extraction

**Goal:** Load the contract schema and populate it using a high-capacity LLM.

Block 3 receives the `Contract(lv1, lv2, lv3, schema_def={})` from Block 2. It is responsible for:

1. **Loading the contract schema** — using the `lv3` code to locate the matching JSON file in `contracts/` (naming convention: `{id}_{lv3_code}.json`). Each file contains the full field definitions (`data.fields.native`) with field keys, types, labels, and constraints.
2. **Building a dynamic Pydantic model** — converting the contract field definitions into a Pydantic model (via `pydantic.create_model()`) to serve as the `response_model` for the Instructor call.
3. **LLM extraction** — passing scored chunks + the dynamic schema to GPT-4o via Instructor. The constrained output guarantees the extracted data conforms to the contract structure.

| Task | Detail |
|---|---|
| `src/bulk_insert/models/contract_loader.py` | `load_contract_schema(lv3: str) -> dict` — locates and reads the contract JSON file by lv3 code. Returns the parsed `data` section with field definitions. |
| `src/bulk_insert/blocks/extraction.py` | `build_dynamic_model(schema: dict) -> type[BaseModel]` — converts contract field definitions into a dynamic Pydantic model. `extract_asset(chunks, contract, cfg) -> Asset` — orchestrates schema loading, model building, and LLM extraction. |
| Per-chunk extraction | Process chunks sequentially (or batched), merging partial results into the final asset. |
| Unit tests | Mock LLM; verify output conforms to contract schema; test contract loading by lv3; test dynamic model building; test merge logic. |

**Exit criteria:** Given chunks + `Contract(lv1, lv2, lv3)`, loads the correct schema, produces a valid `Asset` that passes Pydantic validation.

---

## Phase 5 — Database & State Persistence

**Goal:** Persist pipeline state and results in PostgreSQL.

| Task | Detail |
|---|---|
| `src/bulk_insert/db/models.py` | SQLAlchemy ORM models: `Job`, `BlockResult` (JSONB columns for intermediate data). |
| `src/bulk_insert/db/session.py` | Async session factory (`asyncpg` + SQLAlchemy async). |
| Alembic setup | Initial migration: create `jobs` and `block_results` tables. |
| Repository layer | `src/bulk_insert/db/repository.py` — CRUD for jobs and block results. |
| Unit tests | Test repository with in-memory SQLite or test-scoped Postgres. |

**Exit criteria:** Jobs and block results can be created, updated, and queried. Migrations run cleanly.

---

## Phase 6 — Celery Workers & Task Chaining

**Goal:** Wire blocks into async Celery tasks with automatic chaining and retry.

| Task | Detail |
|---|---|
| `src/bulk_insert/workers/tasks.py` | Define Celery tasks: `preprocess_task`, `determine_contract_task`, `extract_asset_task`. |
| Task chaining | Use `celery.chain()` to link: preprocess → contract → extraction. |
| Retry config | Per-task retry policy: max retries, exponential backoff, exception whitelist. |
| Failure handling | On max retries exhausted: update job status to `FAILED`, persist last-known state, log alert. |
| Celery config | `src/bulk_insert/workers/celery_app.py` — broker URL (Redis), result backend, serialization (JSON). |
| Integration test | Spin up Redis + worker; submit a job; verify chain executes end-to-end. |

**Exit criteria:** A task submitted to the broker triggers the full chain; failures are retried and eventually flagged.

---

## Phase 7 — FastAPI Orchestrator

**Goal:** Expose the HTTP API that Dometria Connect calls.

| Task | Detail |
|---|---|
| `src/bulk_insert/api/app.py` | FastAPI application factory; lifespan for DB connection pool. |
| `src/bulk_insert/api/routes.py` | `POST /jobs` — accept document payload, create job, dispatch chain, return `202 Accepted` + job ID. `GET /jobs/{id}` — return job status and results (if done). |
| Request/response schemas | Pydantic models for API input/output. |
| Unit tests | Test routes with `httpx.AsyncClient` + mocked Celery `.delay()`. |

**Exit criteria:** API accepts a document, responds with `202`, and the background pipeline triggers.

---

## Phase 8 — Docker Compose & Local E2E

**Goal:** Run the entire stack locally with one command.

| Task | Detail |
|---|---|
| `Dockerfile` | Multi-stage build: base image → deps → app. Separate targets for API and worker. |
| `docker-compose.yml` | Services: `api`, `worker`, `redis`, `postgres`. Health checks for all. |
| E2E test script | Submit a real document via `curl` → poll job status → verify extracted asset. |
| `README.md` | Setup instructions, architecture overview, quick-start guide. |

**Exit criteria:** `docker compose up` starts the full stack; E2E test passes.

---

## Phase 9 — Hardening & Observability (post-MVP)

**Goal:** Production readiness.

| Task | Detail |
|---|---|
| Structured logging | Add `python-json-logger`; enrich all log lines with `job_id`, `block`, `duration`. |
| Health endpoints | `GET /health` (API liveness), `GET /ready` (DB + Redis connectivity). |
| Graceful shutdown | Celery warm shutdown; FastAPI lifespan cleanup. |
| Rate limiting | Respect OpenRouter rate limits; add token-bucket or leaky-bucket per model. |
| Security | Validate incoming payloads; add API key auth for Dometria Connect. |
| Documentation | OpenAPI spec review; internal runbook for failure scenarios. |

**Exit criteria:** Service is production-ready for initial internal rollout.
