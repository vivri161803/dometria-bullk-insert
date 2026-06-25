# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Dometria Bulk Insert is an asynchronous, resilient extraction pipeline that turns raw documents (received from *Dometria Connect*) into schema-compliant JSON assets. Internal-only — there is no end-user UI. The work is split into independent processing **blocks**, each a pure Python module designed to later be wrapped in a Celery task and chained Block 1 → Block 2 → Block 3.

The repo is built **spec-driven**: every feature lives under `specs/<date>-<name>/` as `requirements.md`, `plan.md`, `validation.md`. `specs/roadmap.md` is the source of truth for what's done (✅) and what's pending (⬜). Read the relevant spec before changing a block.

## Commands

```bash
uv sync                                   # install deps + create venv
uv run pytest tests/unit                  # unit tests (what pre-commit runs)
uv run pytest tests/unit/test_extraction.py::test_name   # single test
uv run pytest tests/integration -m integration           # live LLM tests (needs OPENROUTER_API_KEY)
uv run ruff check . && uv run ruff format # lint + format
uv run mypy src                           # strict type check
uv run pre-commit run --all-files         # all gates at once
```

There is no `python` on PATH — always invoke through `uv run`. Pre-commit runs ruff (`--fix`), ruff-format, `mypy src`, and `pytest tests/unit`; all must pass at zero errors before committing.

## Architecture

The pipeline is three blocks under `src/bulk_insert/blocks/`, each exposing one orchestrator function. Data flows as Pydantic models defined in `src/bulk_insert/models/`.

**Block 1 — `preprocessing.py` → `preprocess(text) -> list[Chunk]`**
spaCy (blank pipeline + sentencizer only, no model download) segments sentences → sliding-window chunking with overlap → one lightweight-LLM call per chunk (`MODEL_LIGHTWEIGHT`) that *both* scores relevance (1–10) and rewrites the text with coreferences resolved, returning a `ChunkAnalysis`. Scoring and coref resolution are deliberately fused into a single call.

**Block 2 — `contract.py` → `determine_contract(chunks) -> Contract`**
Two-stage category determination, **no knowledge graph**. (1) Filter chunks by `CONTRACT_MIN_SCORE`, take top-`CONTRACT_TOP_K` by score. (2) A constrained LLM call classifies into one of 3 `Level1Literal` macro-categories. (3) `sentence-transformers` embeds chunk texts and candidate `(lv2, lv3)` labels; cosine similarity + a **majority vote weighted by each chunk's relevance score** picks the winning leaf. The category hierarchy is a plain nested dict (`CATEGORY_TREE`) in `models/categories.py` — 3 roots, 12 lv2 nodes, 97 lv3 leaves — traversed by dict lookup via `get_candidate_labels(lv1)`. Returns `Contract(lv1, lv2, lv3, schema_def={})`; schema loading is deferred to Block 3.

**Block 3 — `extraction.py` → `extract_asset(chunks, contract) -> Asset`**
`contract_loader.load_contract_schema(lv3)` globs `contracts/*_{lv3}.json` for the matching contract. `build_dynamic_model()` turns the contract's `data.fields.native` into an **all-optional** Pydantic model via `create_model()` (every field `Optional` with `None` default, so a missing field comes back null instead of failing). A single heavy-model (`MODEL_HEAVY`) Instructor call populates it, wrapped with a self-reported `confidence`. A retry loop re-extracts while `confidence < EXTRACTION_MIN_CONFIDENCE` up to `EXTRACTION_MAX_ATTEMPTS`, then returns the best attempt — **low confidence never raises** (fail-safe).

### Cross-cutting conventions

- **Every LLM call is constrained by a Pydantic model via Instructor** — no free-form JSON parsing, ever. Each block has a private `_build_instructor_client(cfg)` using `instructor.from_provider(f"openai/{model}", base_url=..., api_key=...)`. The provider is **OpenRouter** (OpenAI-compatible); switch models/providers via `.env`, not code.
- **Config** lives in `config.py` as a `pydantic-settings` `Settings`; the module-level `settings` singleton is the default, but every public function takes an optional `cfg: Settings | None` override for testability. Read tunables from `Settings`, don't hard-code.
- **Expensive resources are lazily loaded and `@lru_cache`d** — the spaCy pipeline and the sentence-transformers embedding model load once per process.
- **Fail-safe over fail-fast**: intermediate work is never discarded on a downstream failure. Custom exceptions (`ContractDeterminationError`, `ContractNotFoundError`, `AssetExtractionError`) signal structural problems only.
- Each block module declares an explicit `__all__` and keeps helper functions importable for unit testing.

### Contract files

`contracts/` holds 97 JSON files named `{id}_{lv3}.json` (e.g. `162_restaurant.json`). Each `lv3` code maps 1:1 to exactly one file (the loader raises if zero or multiple match). The relevant content is under `data.fields.native[]` — each field has `key`, `label`, `type` (`text`, `integer`, `decimal`/`currency`, `boolean`/`select`, `foreign_id`, `decimal_pair`, …), `is_required`, and optional `constraints.options`. Fields with type in `EXTRACTION_EXCLUDED_TYPES` (`foreign_id`, `decimal_pair`) and non-text `native_location` fields (geo ids/coordinates) are excluded from the dynamic model — the LLM can't derive them from free text.

## Status & not-yet-built

Blocks 1–3 are complete pure functions. **Architecture pivoted** (2026-06-25) away from the in-app async stack (Celery/Redis/Postgres + `202 Accepted`) to a **stateless, synchronous, horizontally-scalable container**: one container processes one document per request, and concurrency/queueing/retries are pushed down to the deployment platform (more replicas). See `specs/mission.md` + `specs/tech-stack.md` for rationale.

**Not yet implemented** (roadmap Phases 5–8, in order):
1. `src/bulk_insert/pipeline.py` — `process_document(id, text, cfg)` composing Block 1 → 2 → 3 (the missing glue).
2. `src/bulk_insert/api/` — FastAPI app: synchronous `POST /process {id,text} → {id,asset,confidence}`, `GET /health`, `GET /ready` (warms models).
3. `Dockerfile` — single image, embedding model baked in; trim `celery`/`redis`/`sqlalchemy`/`asyncpg`/`alembic` from `pyproject.toml`.
4. Hardening — JSON logging, graceful shutdown, rate limiting, API-key auth.

There is **no** `db/` or `workers/` package and there won't be. `config.py` still carries `REDIS_URL`/`DATABASE_URL` fields — vestigial, removed during the Phase 7 trim.

## Maintaining CLAUDE.md

CLAUDE.md is the single source of truth for how to work in this repository and should stay synchronized with the actual implementation status. **Update CLAUDE.md as part of the spec-driven workflow:**

- **When a phase/block is complete**, update the "Status & not-yet-built" section to mark it done and move the next item to the roadmap.
- **When architecture changes**, update the relevant **Architecture** subsection immediately.
- **When adding new commands/conventions**, add them to the corresponding section so future developers have one authoritative guide.
- **Include CLAUDE.md updates in the same commit/PR as the feature**, not as a separate commit.

If CLAUDE.md and `specs/roadmap.md` disagree, **roadmap is temporarily authoritative** (it updates first), but file an issue to sync CLAUDE.md before merging.

## Notes

- `.env` holds secrets (`OPENROUTER_API_KEY`) and any model/URL overrides.
- Tests use mocked Instructor clients for unit tests; `tests/integration/` is gated behind `-m integration` and makes real LLM calls.
- Italian appears throughout: roadmap/specs prose, category display names (`CATEGORY_NAMES`), contract labels, and default boolean options (`si`/`no`/`non dichiarato`). LLM prompts are in English but target Italian real-estate/business documents.
