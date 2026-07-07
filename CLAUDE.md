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
uv run bulk-insert-cli --file sample.txt  # run a doc through the full pipeline (live LLM)
uv run python scripts/build_artifacts.py  # rebuild artifacts/ (field tree, manifests, metadata sidecar)
uv run python scripts/compile_metadata.py --dry-run  # Phase 6.8 offline enum/description/topic compile (live LLM); --write to apply
uv run python scripts/eval_extraction.py --file sample.txt -n 10  # Block 3 v2 per-field variance/eval (live LLM)
```

There is no `python` on PATH — always invoke through `uv run`. Pre-commit runs ruff (`--fix`), ruff-format, `mypy src`, and `pytest tests/unit`; all must pass at zero errors before committing.

The project is **pip-installable** (hatchling build-system) so the `bulk-insert-cli` console script (`[project.scripts]` → `cli.py:main`) is exposed on `uv sync`. The CLI prints which models were used (lightweight/heavy/embedding) in both the human summary and, under `_models`, in `--json` output. `ruff` is pinned to the same version in `.pre-commit-config.yaml` and the dev deps, with `known-first-party = ["bulk_insert"]` so import grouping is version-stable.

## Architecture

The pipeline is three blocks under `src/bulk_insert/blocks/`, each exposing one orchestrator function. Data flows as Pydantic models defined in `src/bulk_insert/models/`.

**Block 1 — `preprocessing.py` → `preprocess(text) -> list[Chunk]`**
spaCy (blank pipeline + sentencizer only, no model download) segments sentences → sliding-window chunking with overlap → one lightweight-LLM call per chunk (`MODEL_LIGHTWEIGHT`) that *both* scores relevance (1–10) and rewrites the text with coreferences resolved, returning a `ChunkAnalysis`. Scoring and coref resolution are deliberately fused into a single call.

**Block 2 — `contract.py` → `determine_contract(chunks) -> Contract`**
Two-stage category determination, **no knowledge graph**. (1) Filter chunks by `CONTRACT_MIN_SCORE`, take top-`CONTRACT_TOP_K` by score. (2) A constrained LLM call classifies into one of 3 `Level1Literal` macro-categories. (3) `sentence-transformers` embeds chunk texts and candidate `(lv2, lv3)` labels; cosine similarity + a **majority vote weighted by each chunk's relevance score** picks the winning leaf. The category hierarchy is a plain nested dict (`CATEGORY_TREE`) in `models/categories.py` — 3 roots, 12 lv2 nodes, 97 lv3 leaves — traversed by dict lookup via `get_candidate_labels(lv1)`. Returns `Contract(lv1, lv2, lv3, schema_def={})`; schema loading is deferred to Block 3.

**Block 3 — `blocks/extraction/` package → `extract_asset(chunks, contract) -> Asset`** *(tree-driven v2, Phase 6)*
A **tree-driven, evidence-grounded** extractor built to cut field-level variance. Instead of one all-optional model + one heavy call (Phase-4 v1, now superseded), it constrains the LLM to **transcribe evidence-grounded primitives, then derives everything else in code or an isolated second pass**. Pipeline (module → stage):
- `tree.py` — **Stage 1 (Select):** traverse the offline-built field tree over the `Contract`'s `(lv1,lv2,lv3)` path → this product's structural `TreeField`s (cached by signature).
- `metadata.py` — overlay the editable `field_metadata.yaml` sidecar → enriched `FieldNode`s (topic, priority, mandatory, enum, description, derivation).
- `schema.py` — **Stage 2 (Generate):** per-product Pydantic model via `create_model` with a paired `{field}_quote` per evidence field, `Literal` enums that always include the `NOT_PRESENT` abstention sentinel (and, when `OOV_ENABLED`, the `OUT_OF_VOCAB` long-tail sentinel — §6.10 B.1), and a `@model_validator` that rejects any non-null field lacking its verbatim quote (drives Instructor retry; `OUT_OF_VOCAB` is *not* exempt, so it always carries a quote). Derived + excluded-type + non-text `native_location` fields are dropped. A `count`-aggregate node (§6.10 B.2) drops its scalar and instead synthesizes a grounded `{field}__items: list[{value, quote}]` companion. Cached by signature.
- `extract.py` — **Stages 3–4 (Partition + Extract):** partition fields by `topic` (one topic per field), extract each group **sequentially** (no asyncio). A group with any `mandatory AND priority>=HIGH_PRIORITY` field routes to **self-consistency voting** (`SELF_CONSISTENCY_N` runs, majority wins, disagreement → null + flag); otherwise a single standard call. `NOT_PRESENT` normalises to `None`.
- `normalize.py` — **Stage 4b (Normalize):** deterministic, pure-Python value canonicalization (Phase 6.10 Part A), run **after extract, before derive** so computed rules/reconcile see canonical numbers. A **type-driven** normalizer registry (`currency`/`decimal`→float, `percentage`→float-as-written, `integer`→int, `year`→4-digit int, `date`→ISO `YYYY-MM-DD`, `text`→whitespace-collapsed); enums/booleans and `None` are no-ops (already canonical). Because the extraction schema already types numerics, the numeric normalizers are mostly a **safety net** — the real work is `date`/`text`. Unparseable → **keep raw + flag** (never nulls, never raises); rewrites record provenance under `flags[name]["normalization"]`. Off-switch `NORMALIZE_ENABLED`. No LLM, no unit (mq/ha) resolution in this cut.
- `resolve.py` — **Stage 4c (Resolve OUT_OF_VOCAB):** for each compiled-enum field the extractor marked `OUT_OF_VOCAB` (present but unlisted, §6.10 B.1), a **scoped judge** infers the value from the field's **own quote + description only, never the raw document** — re-mapping to the closest listed value when one fits, else emitting faithful Italian free-text (voted for mandatory/high-priority). Inferred-from-quote ⇒ passes Stage 8 by construction; flagged `resolution.out_of_vocab` (a high OOV rate = a wrong compiled enum axis, surfaced in `eval_extraction.py`). Off-switch `OOV_ENABLED` (disabled ⇒ sentinel → `None`, today's behaviour).
- `derive.py` — **Stages 5–7 (Derive):** `computed` fields filled in Python from `source_fields` (rule registry: sum/difference/product/ratio/min/max/mean/percent/ha_to_mq/mq_to_ha/**count** — all pure, clock-free for reproducibility) with reconcile against any stated value; `judged` fields via a 2nd LLM run over **primitives + quotes only, never the raw document**. A `count`-aggregate scalar is derived as `count({field}__items)` over its transcribed companion list (§6.10 B.2) — deterministic, grounded per-item, mandatory-satisfiable; the companion collapses to the count in `Asset.data` with item quotes surfaced under `evidence`.
- `ground.py` — **Stage 8 (Ground + assemble):** single LLM-as-judge entailment gate returning a **pass/fail** verdict per field (failed fields nulled + flagged `ungrounded`; for **numeric** values a faithful transcription of a stated bound/approximation — `"oltre 5.000"` → `5000` — counts as entailed, while enum/boolean/text stay strictly literal), then **policy defaults** (a field still null after grounding falls back to its sidecar `default:` — a business rule, not text evidence, so it carries no quote and is flagged `defaulted`; applied *before* completeness so a defaulted mandatory field is satisfied rather than queued), mandatory-completeness check (null mandatory → `review_queue`), and `confidence` = grounding pass-rate × completeness.
- `orchestrator.py` — wires the stages; `Asset.data`=record, plus optional `evidence`/`flags`/`review_queue` (all default `None`, so existing serialization stays green). **Fail-safe preserved:** low confidence / missing optional fields never raise; only structural problems (`ContractNotFoundError`, or `AssetExtractionError` for a missing tree path) raise.

The runtime consumes **offline-built static artifacts** in `artifacts/` (`field_tree.json`, `field_registry.json`, `product_manifests.json`, `field_metadata.yaml`, and — once compiled — `topics.yaml`), produced by `blocks/extraction/ingest.py` (run via `scripts/build_artifacts.py`, N-agnostic, gated by a round-trip check). The **offline enum/description/topic compile** (`blocks/extraction/compile.py`, run via `scripts/compile_metadata.py`, Phase 6.8) fills the sidecar stubs via batched LLM calls: strictly-Italian `description` + enum `values` (blessing/refining seeded options, `NOT_PRESENT` stripped), a per-field `unsure` self-flag, and a proposed `topic → id` dictionary written to `artifacts/topics.yaml` — each field's `topic` becomes that id, which `metadata.py` resolves back to a name at runtime (string/unmapped topics fall back to the source default). Compiling is **approved-by-default** (writes `description`/`values`, flips `enum.status` to `compiled`/`not_applicable`, records `unsure`) and idempotent (skips already-compiled fields unless `--force`; global `enum_compile_status` flips to `compiled` once none remain pending). Until a field is compiled, the runtime treats `pending` enums as free-text; tests inject mock compiled enums. Spec: `specs/2026-07-03-tree-driven-extraction.md`.

**Orchestrator — `pipeline.py` → `process_document(doc_id, text, cfg) -> PipelineResult`**
The synchronous glue chaining Block 1 → 2 → 3. **Never raises** — every call returns a `PipelineResult` (`models/pipeline.py`: `{id, ok, asset?, confidence?, error?}`) echoing the opaque `doc_id`. A whitespace-only `text` short-circuits to a `bad_input` failure before any block runs. Block exceptions are mapped to a structured `PipelineError{stage, category, message}`: `ContractDeterminationError` → `unprocessable`; `ContractNotFoundError`/`AssetExtractionError` → `server`; transient OpenAI/`httpx` transport errors → `retryable` (tagged with the failing block's stage); any other exception → `server` (catch-all). Categories are chosen so Phase 7 can map them to HTTP `400`/`422`/`500`/`502`. Low extraction confidence is **success**, not an error. The `bulk-insert-cli` dev tool (`cli.py`) runs a document through this orchestrator from the terminal.

### Cross-cutting conventions

- **Every LLM call is constrained by a Pydantic model via Instructor** — no free-form JSON parsing, ever. Each block has a private `_build_instructor_client(cfg)` using `instructor.from_provider(f"openai/{model}", base_url=..., api_key=...)`. The provider is **OpenRouter** (OpenAI-compatible); switch models/providers via `.env`, not code. The `openai/` is only the Instructor *provider* prefix — the model sent to OpenRouter is the **bare slug** (`cfg.MODEL_*`, e.g. `openai/gpt-4o-mini` or `mistralai/ministral-3b`), never double-prefixed.
- **LLM sampling is deterministic by default** — the per-call sampling kwargs are built in **one place**, `llm.py:sampling_params(cfg)`, and spread into every `chat.completions.create` across all three blocks (scoring, lv1, extraction). It sets `temperature` (`LLM_TEMPERATURE`, default `0.0`), optional `seed` (`LLM_SEED`), and `extra_body={"provider": {...}}` for **OpenRouter provider pinning** — the dominant source of run-to-run variance, since the same slug can otherwise be dispatched to different upstream providers with different numerics. Knobs: `OPENROUTER_PROVIDER_ORDER` (pin preference, e.g. `("openai",)`), `OPENROUTER_ALLOW_FALLBACKS` (default `False`, stick to the pinned provider), `OPENROUTER_REQUIRE_PARAMETERS` (default `True`, only route to providers that honour `seed`). Empirically, output stability is still dominated by model capability: a small model (e.g. ministral-3b) varies a lot even at temperature 0, so a capable `MODEL_HEAVY` matters more than the sampling knobs. To quantify variance, `scripts/measure_variance.py --file <doc> -n <N>` runs the full pipeline N times and reports schema-level (does Block 2 flip contracts) vs field-level (Block 3 value drift) agreement.
- **Config** lives in `config.py` as a `pydantic-settings` `Settings`; the module-level `settings` singleton is the default, but every public function takes an optional `cfg: Settings | None` override for testability. Read tunables from `Settings`, don't hard-code.
- **Expensive resources are lazily loaded and `@lru_cache`d** — the spaCy pipeline and the sentence-transformers embedding model load once per process.
- **Fail-safe over fail-fast**: intermediate work is never discarded on a downstream failure. Custom exceptions (`ContractDeterminationError`, `ContractNotFoundError`, `AssetExtractionError`) signal structural problems only.
- Each block module declares an explicit `__all__` and keeps helper functions importable for unit testing.

### Contract files

`contracts/` holds 97 JSON files named `{id}_{lv3}.json` (e.g. `162_restaurant.json`). Each `lv3` code maps 1:1 to exactly one file (the loader raises if zero or multiple match). The relevant content is under `data.fields.native[]` — each field has `key`, `label`, `type` (`text`, `integer`, `decimal`/`currency`, `boolean`/`select`, `foreign_id`, `decimal_pair`, …), `is_required`, and optional `constraints.options`. Fields with type in `EXTRACTION_EXCLUDED_TYPES` (`foreign_id`, `decimal_pair`) and non-text `native_location` fields (geo ids/coordinates) are excluded from the dynamic model — the LLM can't derive them from free text.

## Status & not-yet-built

Blocks 1–3 (**Block 3 now the tree-driven v2**, Phase 6), the Phase 5 orchestrator (`pipeline.py`), and the offline artifact build are complete. **Architecture pivoted** (2026-06-25) away from the in-app async stack (Celery/Redis/Postgres + `202 Accepted`) to a **stateless, synchronous, horizontally-scalable container**: one container processes one document per request, and concurrency/queueing/retries are pushed down to the deployment platform (more replicas). See `specs/mission.md` + `specs/tech-stack.md` for rationale.

**Not yet implemented** (roadmap Phases 7–9, in order):
1. `src/bulk_insert/api/` — FastAPI app: synchronous `POST /process {id,text} → {id,asset,confidence}`, `GET /health`, `GET /ready` (warms models). Maps `PipelineResult.error.category` → status code (`bad_input`→400, `unprocessable`→422, `retryable`→502, `server`→500).
2. `Dockerfile` — single image, embedding model + `artifacts/` baked in; trim `celery`/`redis`/`sqlalchemy`/`asyncpg`/`alembic` from `pyproject.toml`.
3. Hardening — JSON logging, graceful shutdown, rate limiting, API-key auth.

There is **no** `db/` or `workers/` package and there won't be. `config.py` still carries `REDIS_URL`/`DATABASE_URL` fields — vestigial, removed during the containerization trim.

The **enum/description compile step is implemented** (roadmap **Phase 6.8**, `blocks/extraction/compile.py` + `scripts/compile_metadata.py`): the ingest still seeds `artifacts/field_metadata.yaml` with `pending`/`null` stubs, and the offline compile tool fills `description` + `enum.values` (strictly **Italian**), sets a per-field `unsure` self-flag, and flips `status: compiled`. It also proposes a baseline **topics dictionary** in `artifacts/topics.yaml` (user-editable) and rewrites each field's `topic` to an id into it — the one small runtime addition (`metadata.py` resolves the id → topic name; enum/description remain runtime-unchanged). The compile is gated behind `OPENROUTER_API_KEY`; until a field is compiled, closed-vocab (`select`/`boolean`) fields extract as free-text. `priority` stays a human-owned field. (Note: the committed `artifacts/` are not yet compiled — run `scripts/compile_metadata.py --write` to produce `topics.yaml` and populate the sidecar.)

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
