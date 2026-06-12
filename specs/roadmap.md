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

**Goal:** Determine the target JSON schema from scored chunks using hybrid RB-RAG.

| Task | Detail |
|---|---|
| `src/bulk_insert/blocks/contract.py` | LLM extracts lv1 field (Instructor-constrained to a predefined enum). Graph matching deduces lv2/lv3. |
| Cosine similarity | Embed chunks + candidate schema paths via sentence-transformers; compute cosine similarity; weighted majority vote. |
| Unit tests | Test with known contracts; test voting logic; test graph matching. |

**Exit criteria:** Given scored chunks, outputs the correct `Contract` with deterministic schema selection.

---

## Phase 4 — Block 3: Asset Extraction

**Goal:** Populate the contract using a high-capacity LLM.

| Task | Detail |
|---|---|
| `src/bulk_insert/blocks/extraction.py` | Instructor + openai SDK call to GPT-4o. Input: scored chunks + empty contract schema. Output: populated `Asset` Pydantic model. |
| Per-chunk extraction | Process chunks sequentially (or batched), merging partial results into the final asset. |
| Unit tests | Mock LLM; verify output conforms to contract schema; test merge logic. |

**Exit criteria:** Given chunks + contract, produces a valid `Asset` that passes Pydantic validation.

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
