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
| HTTP API | **FastAPI** | Async-native, Pydantic-integrated, auto-generated OpenAPI docs. Internal-only, but typed API contracts simplify integration with Dometria Connect. |
| ASGI server | **uvicorn** | Standard production server for FastAPI. |

---

## NLP & Text Processing

| Component | Choice | Rationale |
|---|---|---|
| Chunking & coreference | **spaCy** (with `coreferee` or `spacy-experimental` coreference) | Production-grade NLP; handles tokenization, sentence segmentation, chunking, and coreference resolution in a single pipeline. |
| Embeddings (cosine similarity) | **sentence-transformers** | Fast local embeddings for chunk-to-schema cosine similarity in Block 2. |

---

## LLM Integration

| Component | Choice | Rationale |
|---|---|---|
| LLM provider gateway | **OpenRouter** | Unified API for multiple LLM providers; OpenAI-compatible. |
| Client SDK | **openai** (Python SDK) | OpenRouter exposes an OpenAI-compatible endpoint; no need for a custom client. |
| Structured output | **instructor** + **Pydantic v2** | Constrains every LLM call to a typed schema. Eliminates free-form JSON parsing and guarantees contract adherence. |
| Lightweight model (scoring) | **GPT-4o-mini** (via OpenRouter) | Fast, cheap — ideal for chunk relevance scoring (Block 1). |
| Heavy model (extraction) | **GPT-4o** (via OpenRouter) | High-capacity reasoning — required for accurate asset extraction (Block 3). |

> Note: make this configurable using a config.py file for all the LLM providers and models, so that we can switch providers or models with minimal code changes. Make this as well as the other configration stored in a .env file.

---

## Task Orchestration

| Component | Choice | Rationale |
|---|---|---|
| Task queue / broker | **Redis** | Lightweight, fast, well-supported by Celery. Also serves as result backend. |
| Task worker | **Celery** | Industry-standard for Python async task processing; built-in retry, chaining, error callbacks. |
| Task chaining | **Celery chains** (`chain()`, `chord()`) | Blocks are linked automatically: Block 1 → Block 2 → Block 3. |

---

## Persistence & State

| Component | Choice | Rationale |
|---|---|---|
| Pipeline state & intermediate results | **PostgreSQL** | Relational integrity for tracking job state, intermediate outputs, and final assets. Supports JSONB for storing extracted schemas and assets without schema migration overhead. Robust, battle-tested, easy to query for manual intervention. |
| Redis | **Result backend + broker only** | Ephemeral task metadata; not for long-term persistence. |

> **Why PostgreSQL over MongoDB?** The data has a well-defined relational structure (jobs → blocks → results). PostgreSQL's JSONB columns provide document flexibility where needed (contract schemas, extracted assets) without sacrificing referential integrity. It also integrates cleanly with SQLAlchemy/Alembic for migrations.

---

## Testing & Code Quality

| Component | Choice | Rationale |
|---|---|---|
| Test framework | **pytest** | Standard; supports fixtures, parametrization, async tests. |
| Linter / formatter | **ruff** | Extremely fast; replaces flake8, isort, black in a single tool. |
| Pre-commit hooks | **pre-commit** | Runs `ruff check`, `ruff format`, and `pytest` (quality tests) before every commit. |
| Type checking | **mypy** (strict mode) | Catches type errors early; complements Pydantic's runtime validation. |

---

## Observability (Phase 1)

| Component | Choice | Rationale |
|---|---|---|
| Logging | **Python `logging`** (stdlib) | Simple, no external dependencies. Structured log messages with job/block context. |
| Log format | **JSON lines** (via `python-json-logger`) | Machine-parseable; ready for future log aggregation (ELK, Loki). |

> Future phases may introduce OpenTelemetry or Sentry. The logging foundation is designed to be forwards-compatible.

---

## Containerization (when deployment target is decided)

| Component | Choice | Rationale |
|---|---|---|
| Container runtime | **Docker** | Standard; Dockerfile per service (API, worker). |
| Local orchestration | **Docker Compose** | Run API + Redis + PostgreSQL + Worker locally in one command. |

> Production deployment target (Kubernetes, ECS, Cloud Run, etc.) is deferred. Docker Compose is sufficient for development and initial staging.

---

## Project Structure (target layout)

```
bulk_insert/
├── specs/                  # ← You are here
│   ├── mission.md
│   ├── tech-stack.md
│   └── roadmap.md
├── src/
│   └── bulk_insert/
│       ├── __init__.py
│       ├── api/            # FastAPI app, routes, schemas
│       ├── blocks/         # Block 1, 2, 3 modules
│       ├── models/         # Pydantic models (contracts, chunks, assets)
│       ├── workers/        # Celery task definitions
│       ├── db/             # SQLAlchemy models, migrations (Alembic)
│       └── config.py       # Settings via pydantic-settings
├── tests/
│   ├── unit/
│   └── integration/
├── pyproject.toml
├── .pre-commit-config.yaml
├── Dockerfile
├── docker-compose.yml
└── README.md
```
