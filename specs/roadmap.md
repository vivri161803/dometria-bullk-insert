# Roadmap — Dometria Bulk Insert Pipeline

> Each phase is designed to be **independently deliverable and testable**.
> Phases are sequential — each builds on the previous one.
>
> **Legend:** ✅ = completata · ⬜ = da fare

---

## ✅ Phase 0 — Project Scaffolding

**Goal:** Establish the project skeleton, tooling, and developer workflow before any application logic.

- [x] Inizializzare `pyproject.toml` con `uv` — metadata, Python 3.13, dipendenze iniziali.
- [x] Creare layout sorgenti — package `src/bulk_insert/` con `__init__.py`, `config.py`.
- [x] Creare layout test — `tests/unit/`, `tests/integration/` con test segnaposto.
- [x] Configurare `ruff` — regole linting + formatting in `pyproject.toml`.
- [x] Configurare `mypy` — strict mode in `pyproject.toml`.
- [x] Configurare `pre-commit` — `.pre-commit-config.yaml` con `ruff check`, `ruff format`, `pytest tests/unit`.
- [x] Aggiungere `.gitignore` — ignore standard Python + uv + IDE.
- [x] Verifica — `uv sync`, `ruff check .`, `mypy src/`, `pytest` passano tutti a zero errori.

**Exit criteria:** Un developer può clonare, `uv sync`, e lanciare tutte le quality gate con zero errori.

---

## ✅ Phase 1 — Pydantic Models & Config

**Goal:** Define all shared data structures and configuration before implementing any logic.

- [x] `src/bulk_insert/config.py` — config basata su `pydantic-settings`: OpenRouter API key, Redis URL, Postgres DSN, nomi modelli, chunk size, overlap, embedding model, contract min score, contract top-k.
- [x] `src/bulk_insert/models/chunk.py` — modello `Chunk` (text, index, overlap metadata, flag coreference-resolved, relevance score) + `ChunkAnalysis` e `ChunkScore`.
- [x] `src/bulk_insert/models/contract.py` — modello `Contract` (lv1, lv2, lv3, `schema_def` con default `{}`) + `Level1Prediction` (Instructor response model con `Level1Literal`).
- [x] `src/bulk_insert/models/asset.py` — modello `Asset` (contract_id, data dict, confidence 0–1).
- [x] `src/bulk_insert/models/job.py` — modello `Job` con enum `JobStatus` (PENDING → PREPROCESSING → CONTRACTING → EXTRACTING → DONE / FAILED), timestamps, block results.
- [x] `src/bulk_insert/models/categories.py` — `CATEGORY_TREE`, `CATEGORY_NAMES`, `CATEGORY_ANCESTRY`, `Level1Category`, `Level1Literal`, helper functions `get_candidate_paths()` e `get_candidate_labels()`.
- [x] Unit tests — serialization round-trip, validation edge cases, enum transitions, category helper correctness.

**Exit criteria:** Tutti i modelli serializzano/deserializzano correttamente; la config si carica da `.env`.

---

## ✅ Phase 2 — Block 1: Preprocessing

**Goal:** Implement sentence segmentation, chunking, and combined LLM scoring + coreference resolution as a standalone module.

- [x] `src/bulk_insert/blocks/preprocessing.py` — pipeline spaCy (sentencizer only, no model download) → sentence segmentation → chunking con overlap.
- [x] Scoring + coreference combinati — singola chiamata Instructor per chunk a GPT-4o-mini. Input: testo chunk. Output: `ChunkAnalysis` (score 1–10, resolved_text). L'LLM valuta la rilevanza e riscrive il testo risolvendo i coreference in un'unica chiamata.
- [x] Unit tests — test chunking su testi campione, test overlap boundaries, test scoring + coref con LLM mockato.
- [x] Integration test — end-to-end con chiamata LLM reale (gated da `pytest.mark.integration`).

**Exit criteria:** Dato un testo grezzo, produce una lista di `Chunk` scorati e coref-resolved con overlap metadata corretti.

---

## ✅ Phase 3 — Block 2: Contract Determination

**Goal:** Determine the target category path (`lv1/lv2/lv3`) from scored chunks using a two-stage hybrid approach — no knowledge graph.

> **Nota architetturale:** L'architettura originale (`architecture.md`) prevedeva un approccio "graph matching" per dedurre lv2/lv3. Poiché la struttura delle categorie è un semplice albero a 3 livelli con sole 3 radici (non un grafo complesso), abbiamo adottato un approccio più semplice ed efficace: LLM per lv1 → cosine similarity + majority voting pesato per lv2+lv3.

- [x] `src/bulk_insert/blocks/contract.py` — pipeline a due stadi:
  - **Stage 1:** Chiamata LLM vincolata con Instructor (GPT-4o-mini) classifica il documento in uno dei 3 valori `Level1Literal`.
  - **Stage 2:** Sentence-transformers (`paraphrase-multilingual-MiniLM-L12-v2`) codifica testi chunk e label candidate; cosine similarity + majority vote pesato per `relevance_score` seleziona la coppia `(lv2, lv3)` vincente.
- [x] Chunk filtering — filtra chunk con `relevance_score >= CONTRACT_MIN_SCORE` (default 5), seleziona top-k (default 5) per score decrescente.
- [x] Category helpers — `get_candidate_paths(lv1)` e `get_candidate_labels(lv1)` espandono il `CATEGORY_TREE` in coppie candidate `(lv2, lv3)` con label leggibili per l'embedding.
- [x] Custom exception — `ContractDeterminationError` per errori di filtering (nessun chunk qualificato).
- [x] Embedding model — caricato lazily con `@lru_cache`, configurabile via `Settings.EMBEDDING_MODEL`.
- [x] Logging strutturato — statistiche filtering, predizione lv1, top-3 candidati con score, vincitore finale.
- [x] Unit tests — test filtering, mocked LLM lv1, voting con embedding reali (testi ristorante/villa), e2e con LLM mockato + embedding reali.
- [x] Quality gates — `ruff check`, `ruff format`, `mypy --strict`, `pytest` tutti passano a zero errori.

**Output:** `Contract(lv1, lv2, lv3, schema_def={})` — il percorso categoria è determinato; il caricamento dello schema è demandato al Block 3.

**Exit criteria:** Dati chunk scorati, restituisce il `Contract` corretto. Il codice `lv3` mappa 1:1 a un file JSON contratto in `contracts/`.

---

## ✅ Phase 4 — Block 3: Asset Extraction

**Goal:** Load the contract schema and populate it using a high-capacity LLM.

Il Block 3 riceve il `Contract(lv1, lv2, lv3, schema_def={})` dal Block 2. È responsabile di:

1. **Caricare lo schema del contratto** — usando il codice `lv3` per localizzare il file JSON corrispondente in `contracts/` (convenzione: `{id}_{lv3_code}.json`). Ogni file contiene le definizioni complete dei campi (`data.fields.native`) con key, tipi, label e vincoli.
2. **Costruire un modello Pydantic dinamico** — convertendo le definizioni dei campi in un modello Pydantic (via `pydantic.create_model()`) da usare come `response_model` per la chiamata Instructor.
3. **Estrazione LLM** — passando chunk scorati + schema dinamico a GPT-4o via Instructor. L'output vincolato garantisce che i dati estratti siano conformi alla struttura del contratto.

- [x] `src/bulk_insert/models/contract_loader.py` — `load_contract_schema(lv3: str) -> dict`: localizza e legge il file JSON del contratto tramite codice lv3 (glob `*_{lv3}.json`). Ritorna la sezione `data` con le definizioni dei campi. `contract_id_for(lv3)` ritorna lo stem del file. Eccezione `ContractNotFoundError`.
- [x] `src/bulk_insert/blocks/extraction.py` — `build_dynamic_model(schema: dict) -> type[BaseModel]`: converte le definizioni campi in modello Pydantic dinamico (tutti i campi `Optional`, default `None`; `boolean`/`select` → `Literal`). `extract_asset(chunks, contract, cfg) -> Asset`: orchestra caricamento schema, costruzione modello e estrazione LLM.
- [x] Estrazione single-batch — singola chiamata GPT-4o (`MODEL_HEAVY`) via Instructor sui chunk concatenati, con response model wrapper `{asset, confidence}`.
- [x] Loop di re-estrazione — re-estrae finché `confidence < EXTRACTION_MIN_CONFIDENCE` (default 0.6) fino a `EXTRACTION_MAX_ATTEMPTS` (default 3); mantiene il miglior tentativo (fail-safe, nessun raise su bassa confidence).
- [x] Field exclusion dichiarativa — esclude `type ∈ {foreign_id, decimal_pair}` o campi `native_location` non-text (geo id/coordinate).
- [x] Unit tests — mock LLM; contract loading per lv3; costruzione modello dinamico (tipi, Literal, opzionalità, esclusioni, metadata); orchestratore (happy path, retry, exhaust-best, short-circuit, errori).

**Exit criteria:** Dati chunk + `Contract(lv1, lv2, lv3)`, carica lo schema corretto, produce un `Asset` valido che passa la validazione Pydantic.

---

> **🔀 Architectural shift (from Phase 5 onward).** The original plan (Phases 5–9 below)
> built an in-app async system: PostgreSQL for job state, Celery workers on a Redis broker,
> and a `202 Accepted` + polling API. This was **replaced by a stateless, synchronous,
> horizontally-scalable container**. The pipeline blocks are pure functions, so concurrency,
> queueing, and retries are pushed down to the deployment platform (more replicas behind a
> load balancer) instead of being reimplemented in the application. Celery, Redis, and
> PostgreSQL — and the `db/` and `workers/` packages — are **no longer part of the plan.**
> See `mission.md` and `tech-stack.md` for the full rationale.

---

## ✅ Phase 5 — Pipeline Orchestrator

**Goal:** Compose the three blocks into a single synchronous entry point — the glue currently missing.

> Spec: `specs/2026-06-26-pipeline-orchestrator/`.

- [x] `src/bulk_insert/pipeline.py` — `process_document(doc_id: str, text: str, cfg: Settings | None) -> PipelineResult`: runs `preprocess` → `determine_contract` → `extract_asset` in sequence and tags the output with `doc_id`.
- [x] `PipelineResult` model (`models/pipeline.py`) — single model `{ id, ok, asset?, confidence?, error? }`; the error variant carries `PipelineError{ stage, category, message }`.
- [x] Error translation — map block exceptions to outcomes: empty/blank text → `bad_input`; `ContractDeterminationError` (no qualifying chunk) → `unprocessable`; `ContractNotFoundError` / `AssetExtractionError` → `server`; transient OpenAI/`httpx` failure → `retryable`; any other exception → `server` (catch-all, never raises).
- [x] Stateless guarantee — no I/O beyond reading bundled `contracts/`; nothing persisted; `doc_id` is opaque and only echoed.
- [x] Unit tests — happy path with all blocks mocked; each error branch; `id` is always echoed (16 tests).
- [x] `bulk-insert-cli` (`cli.py`) — developer harness to run a document through the orchestrator from the terminal (`--text`/`--file`/stdin, `--json`).

**Exit criteria:** Given `(id, text)`, returns a compiled-asset result or a structured error, both carrying the `id`. ✅

---

## ✅ Phase 6 — Tree-Driven Extraction (Block 3 v2 · variance reduction)

**Goal:** Rework Block 3 into a **tree-driven, evidence-grounded extractor** to cut
**field-level extraction variance** (the value drift `scripts/measure_variance.py`
isolates within a stable contract). Blocks 1 & 2 are unchanged; Block 2's
`Contract(lv1,lv2,lv3)` is the classifier input. The public entrypoint
`extract_asset(chunks, contract, cfg) -> Asset` stays stable, so `pipeline.py` and
`PipelineResult` are untouched.

> Full spec: `specs/2026-07-03-tree-driven-extraction.md`. This **supersedes the
> Phase 4 Block 3 implementation** (single all-optional dynamic model → single heavy
> call). Sequenced **before** the FastAPI service: get extraction reproducible before
> exposing it over HTTP.

- [x] **6.0 — Offline ingest → static artifacts.** Parse the 97 contracts (N-agnostic) →
  `field_registry.json`, `field_tree.json`, `product_manifests.json`,
  `field_metadata.yaml`. Round-trip: every contract exactly reconstructable from its
  manifest paths. One `topic` per field; `derivation.source_fields` all resolve.
- [x] **6.1 — Select + schema generation.** Traverse the tree over the `Contract` path →
  cached per-product Pydantic model with paired `_quote` fields, `NOT_PRESENT` in every
  compiled enum, quote `@model_validator`; derived + excluded-type fields dropped.
- [x] **6.2 — Partition + sequential standard extraction.** Partition by `topic` (assert
  one topic per field); extract groups **sequentially**; Instructor + evidence spans +
  retry on missing quote.
- [x] **6.3 — Self-consistency tier.** Vote (`SELF_CONSISTENCY_N`) only on groups with a
  `mandatory AND priority>=HIGH_PRIORITY` field; disagreement → null + flag.
- [x] **6.4 — Computed derivation + reconcile.** Fill `computed` fields in code from
  `source_fields`; flag stated-vs-derived disagreements.
- [x] **6.5 — Judged derivation (if any).** 2nd LLM run over **primitives + quotes only,
  never the raw document**; entailment-grounded; votes if high-value.
- [x] **6.6 — Grounding gate + completeness.** LLM-as-judge entailment gate
  (`JUDGE_THRESHOLD` ~0.7); null mandatory fields → `review_queue`; map
  `{record, evidence, flags, review_queue}` into the extended `Asset`.
- [x] **6.7 — Eval / variance harness.** Per-field metrics (schema-pass, accuracy,
  hallucination rate, abstention correctness); demonstrate improved field-level
  agreement vs the Phase-4 Block 3.
- [x] **Config knobs** — `HIGH_PRIORITY`, `SELF_CONSISTENCY_N`, `JUDGE_THRESHOLD`,
  `FIELD_METADATA_PATH` added to `Settings`.
- [x] **Enum/description compilation is deferred** — the sidecar ships with `pending`
  stubs and `# TODO: llm-compile` markers; 6.1+ tests use **mock enums/descriptions**.
  A separate offline LLM tool fills them later (**Phase 6.8**); the runtime does not
  change when it does (enum/description parts). After compile, `topic` becomes an id
  into `artifacts/topics.yaml`.

**Exit criteria:** Given `(chunks, Contract)`, Block 3 returns an evidence-grounded
`Asset` with measurably higher run-to-run field agreement than the Phase-4 extractor,
mandatory-completeness enforced, and low-confidence/ungrounded fields flagged.

---

## ✅ Phase 6.8 — Offline Enum/Description Compile (metadata sidecar)

**Goal:** Fill the deferred `field_metadata.yaml` stubs — `description`, enum
`values`, and a baseline `topic` assignment — via LLM API calls, so closed-vocab
fields extract as constrained enums, fields carry compiled prompt context, and fields
are grouped by a model-proposed topic dictionary the user can edit. **Offline,
one-off**; the enum/description parts require no runtime change (the runtime already
consumes `compiled` enums / descriptions), and the topic-id scheme adds one small
resolution lookup in `metadata.py`. Gated behind an API key like the integration
tests. This is the "Enum-compile handoff" of
`specs/2026-07-03-tree-driven-extraction.md` §8, extended to topics.

- [x] `Settings.MODEL_COMPILE` — new slug; empty → falls back to `MODEL_HEAVY`.
      Optional `COMPILE_BATCH_SIZE` (fields per LLM call); `TOPICS_PATH`
      (default `artifacts/topics.yaml`).
- [x] Topics dictionary — the model proposes a compact `topic → id` set written to
      `artifacts/topics.yaml` (user-editable config); each field's `topic` in
      `field_metadata.yaml` is rewritten to the integer id. Runtime resolves the id
      back to the topic name in `metadata.py` (fallback preserved for unmapped/legacy
      string topics). Partitioning in `extract.py` is unaffected (it groups by the
      resolved topic).
- [x] `src/bulk_insert/blocks/extraction/compile.py` — pure, testable core:
      given the loaded registry + sidecar, call Instructor (batched) to produce, per
      pending field, an **Italian** `description`, a proposed `topic` name, and — for
      enum candidates — a refined **Italian** value list, an `applicable` flag, and an
      `unsure` flag. Proposed topic names are collected/deduped into the topics
      dictionary (`topic → id`), and each field's `topic` is stored as that id. Reuses
      `client.build_instructor_client(cfg)` and `llm.sampling_params(cfg)`. Input
      context per field from `field_registry.json`: `label`, `field_type`, seeded
      `options`, `products`.
- [x] Italian-only output — the system prompt requires all descriptions and enum
      values strictly in Italian; a validation check rejects/flags obviously
      non-Italian output (at minimum: non-empty, and `unsure=true` when the model is
      not confident).
- [x] Faithfulness + validation — bless/refine seeded `options` (never invent wildly),
      strip/dedupe, never emit `NOT_PRESENT` (added at runtime); `applicable=False`
      ⇒ `enum.status: not_applicable`.
- [x] Approved-by-default flip — compiling a field writes `description`,
      `enum.values`, sets `enum.status: compiled` (or `not_applicable`) immediately,
      and records `unsure: true|false`. No separate approval step; the runtime picks
      up `compiled` enums on next load.
- [x] `unsure` spot-check field — every compiled field gets a boolean `unsure`
      (additive, runtime-ignored). Reviewers filter `unsure: true` (e.g.
      `grep 'unsure: true'`) to inspect only the low-confidence subset.
- [x] `scripts/compile_metadata.py` — CLI (mirrors `build_artifacts.py`):
      `--dry-run` (print diff, default), `--write`, `--field <name>`, `--force`
      (recompile already-compiled). Writes back preserving the sidecar shape (reuse the
      ingest YAML emitter; compiled fields drop their `# TODO: llm-compile` markers and
      gain an `unsure` line).
- [x] Idempotent — skips fields already `compiled`/`not_applicable` unless `--force`;
      global `meta.enum_compile_status` flips to `compiled` once no `pending` remain.
      Re-runs reuse existing `topics.yaml` ids, only adding new topics. *(Superseded by
      Phase 6.9: with clustering enabled, ids are re-derived per run, not stable.)*
- [x] `metadata.py` runtime resolution — load `topics.yaml`; `FieldMetadata.resolve`
      maps an integer `topic` id → topic name for `FieldNode.topic`; string topics and
      unmapped ids fall back to the existing source-based default. Small, covered
      change; `extract.partition_by_topic` is untouched.
- [x] Unit tests (mocked Instructor) — Italian description + values written; topic
      dictionary built and per-field `topic` stored as id; `status` auto-flips to
      `compiled`; `unsure` recorded; `not_applicable` path; dedupe / `NOT_PRESENT`
      stripped; `--force`; dry-run makes no writes; metadata resolves topic id → name.
      One `-m integration` live smoke test (real LLM, one field, asserts Italian).

**Exit criteria:** After a compile run, `artifacts/topics.yaml` holds the baseline
`topic → id` dictionary, `field_metadata.yaml` references it by id, and
`metadata.load_field_metadata` returns `compiled` enums (Italian values), non-null
Italian descriptions, and topics resolved from the dictionary;
`schema.build_extraction_model` produces `Literal` enums (incl. `NOT_PRESENT`) for
those fields. Fields flagged `unsure: true` are the only ones needing a human look;
the user can freely edit `topics.yaml` and per-field ids/priorities afterward.

---

## ✅ Phase 6.9 — Topic Consolidation (embedding clustering)

**Goal:** The 6.8 compile proposes topics batch-by-batch with exact-string dedup, so a
full run emitted **236 topics for 1083 fields** (near-duplicates, singular/plural,
product-coupled names like `servizi bar` / `superfici wine bar`). Consolidate them into
a compact set of embedding clusters **automatically** at the end of the compile, so
`artifacts/topics.yaml` is a cluster dictionary that stays wired to `field_metadata.yaml`
by id. **Offline (no LLM)**, deterministic. Spec:
`specs/2026-07-07-topic-consolidation.md`.

- [x] `blocks/extraction/topics_cluster.py::consolidate_topics` — embeds topic names
      with the Block 2 model (`EMBEDDING_MODEL`), clusters via scikit-learn
      `AgglomerativeClustering` (`metric="cosine"`), picks the most-used member as each
      cluster's representative, assigns fresh ids, and remaps every field's `topic` id to
      its cluster id (non-integer/unmapped topics passed through).
- [x] Agglomerative (not greedy) — greedy nearest-representative created a generic hub
      cluster; agglomerative `average` linkage avoids it. `linkage` is configurable.
- [x] `Settings` knobs + CLI overrides — `TOPIC_CLUSTER_ENABLED` (`--cluster` /
      `--no-cluster`), `TOPIC_CLUSTER_THRESHOLD` (`--cluster-threshold`),
      `TOPIC_CLUSTER_LINKAGE` (`--cluster-linkage {average,complete,single}`). Applied
      via `settings.model_copy` in `scripts/compile_metadata.py`.
- [x] Integration — runs in `compile_metadata.main` after `plan_compile`, before the
      diff/write, on both dry-run and `--write`. `metadata.py` and
      `extract.partition_by_topic` are unchanged (still resolve/group by topic id).
- [x] `topics.yaml` header updated — ids are a derived build output (may change with
      topics/model/threshold), no longer "stable across re-runs".
- [x] Unit tests — `tests/unit/test_topics_cluster.py` (mocked encoder): merge + remap,
      representative = most-used, threshold controls granularity, singleton/non-integer
      passthrough. `test_compile.py` stays green (compile core unchanged).

**Exit criteria:** A compile run writes a consolidated `topics.yaml` (cluster
representative → id) and a `field_metadata.yaml` whose per-field `topic` ids all resolve
into that dictionary; cluster count and granularity are tunable from the CLI without code
changes; the runtime consumes the result with no modification.

---

## ⬜ Phase 7 — FastAPI Synchronous Service

**Goal:** Expose the single HTTP endpoint Dometria Connect calls, plus health probes.

- [ ] `src/bulk_insert/api/schemas.py` — `ProcessRequest{ id, text }`, `ProcessResponse{ id, asset, confidence }`, `ErrorResponse{ id, error }`.
- [ ] `src/bulk_insert/api/routes.py` — `POST /process` (synchronous `def`, runs in the threadpool) → calls `process_document`; `GET /health` (liveness); `GET /ready` (readiness — green once models are warmed).
- [ ] `src/bulk_insert/api/app.py` — FastAPI factory; lifespan warms the spaCy pipeline and embedding model so the first real request isn't cold.
- [ ] Status-code mapping — `200` success (incl. low confidence); `400` empty text; `422` `ContractDeterminationError`; `502` upstream LLM failure; `500` otherwise. Every body echoes the `id`.
- [ ] Concurrency limit — cap per-container in-flight requests (constrained threadpool / single uvicorn worker) so one replica doesn't run many heavy extractions at once.
- [ ] Unit tests — route tests with `fastapi.testclient` / `httpx`, `process_document` mocked; assert status-code mapping and `id` echo.

**Exit criteria:** `POST /process {id,text}` returns `{id, asset, confidence}` on the same request; health/readiness probes behave correctly.

---

## ⬜ Phase 8 — Containerization

**Goal:** Ship the service as one self-contained, stateless image.

- [ ] `Dockerfile` — multi-stage (`uv` deps → app); single image, single process (uvicorn). No separate API/worker targets.
- [ ] Bake models + artifacts — download the sentence-transformers embedding model at build time (`HF_HOME`) so cold-start has no network dependency; bundle `contracts/` and the offline-built `artifacts/` (field tree, manifests, `field_metadata.yaml`).
- [ ] Runtime config — `OPENROUTER_API_KEY` and model/URL overrides injected via env/secrets, never baked into the image.
- [ ] Trim `pyproject.toml` — remove `celery`, `redis`, `sqlalchemy`, `asyncpg`, `alembic`; refresh `uv.lock`.
- [ ] `README.md` — build/run instructions, the API contract, and the deployment handoff notes (stateless, scale by replicas, long-request timeouts).
- [ ] Smoke test — `docker run` the image and hit `POST /process` with a sample document.

**Exit criteria:** `docker run` starts the service; a real document submitted via `curl` returns a valid compiled asset.

---

## ⬜ Phase 9 — Hardening & Observability (post-MVP)

**Goal:** Production readiness for internal rollout.

- [ ] Structured logging — add `python-json-logger`; enrich every log line with `id`, `block`, and `duration`; emit to stdout/stderr.
- [ ] Graceful shutdown — FastAPI lifespan cleanup; let in-flight requests drain before exit (so the orchestrator can do rolling deploys safely).
- [ ] Rate limiting — respect OpenRouter rate limits; add per-model backoff/throttling to absorb `429`s.
- [ ] Security — validate inbound payload size/shape; add an API-key check for Dometria Connect.
- [ ] Documentation — review the auto-generated OpenAPI spec; write an internal runbook for failure scenarios.

**Exit criteria:** The service is production-ready for the initial internal rollout.
