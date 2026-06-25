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

## ⬜ Phase 5 — Pipeline Orchestrator

**Goal:** Compose the three blocks into a single synchronous entry point — the glue currently missing.

- [ ] `src/bulk_insert/pipeline.py` — `process_document(doc_id: str, text: str, cfg: Settings | None) -> PipelineResult`: runs `preprocess` → `determine_contract` → `extract_asset` in sequence and tags the output with `doc_id`.
- [ ] `PipelineResult` model — `{ id, asset, confidence }` for success; a structured error variant `{ id, error }` carrying the failing stage.
- [ ] Error translation — map block exceptions to outcomes: empty/blank text → bad input; `ContractDeterminationError` (no qualifying chunk) → unprocessable input; `ContractNotFoundError` / `AssetExtractionError` → server/schema error; upstream LLM failure → retryable.
- [ ] Stateless guarantee — no I/O beyond reading bundled `contracts/`; nothing persisted; `doc_id` is opaque and only echoed.
- [ ] Unit tests — happy path with all blocks mocked; each error branch; `id` is always echoed.

**Exit criteria:** Given `(id, text)`, returns a compiled-asset result or a structured error, both carrying the `id`.

---

## ⬜ Phase 6 — FastAPI Synchronous Service

**Goal:** Expose the single HTTP endpoint Dometria Connect calls, plus health probes.

- [ ] `src/bulk_insert/api/schemas.py` — `ProcessRequest{ id, text }`, `ProcessResponse{ id, asset, confidence }`, `ErrorResponse{ id, error }`.
- [ ] `src/bulk_insert/api/routes.py` — `POST /process` (synchronous `def`, runs in the threadpool) → calls `process_document`; `GET /health` (liveness); `GET /ready` (readiness — green once models are warmed).
- [ ] `src/bulk_insert/api/app.py` — FastAPI factory; lifespan warms the spaCy pipeline and embedding model so the first real request isn't cold.
- [ ] Status-code mapping — `200` success (incl. low confidence); `400` empty text; `422` `ContractDeterminationError`; `502` upstream LLM failure; `500` otherwise. Every body echoes the `id`.
- [ ] Concurrency limit — cap per-container in-flight requests (constrained threadpool / single uvicorn worker) so one replica doesn't run many heavy extractions at once.
- [ ] Unit tests — route tests with `fastapi.testclient` / `httpx`, `process_document` mocked; assert status-code mapping and `id` echo.

**Exit criteria:** `POST /process {id,text}` returns `{id, asset, confidence}` on the same request; health/readiness probes behave correctly.

---

## ⬜ Phase 7 — Containerization

**Goal:** Ship the service as one self-contained, stateless image.

- [ ] `Dockerfile` — multi-stage (`uv` deps → app); single image, single process (uvicorn). No separate API/worker targets.
- [ ] Bake models — download the sentence-transformers embedding model at build time (`HF_HOME`) so cold-start has no network dependency; bundle `contracts/`.
- [ ] Runtime config — `OPENROUTER_API_KEY` and model/URL overrides injected via env/secrets, never baked into the image.
- [ ] Trim `pyproject.toml` — remove `celery`, `redis`, `sqlalchemy`, `asyncpg`, `alembic`; refresh `uv.lock`.
- [ ] `README.md` — build/run instructions, the API contract, and the deployment handoff notes (stateless, scale by replicas, long-request timeouts).
- [ ] Smoke test — `docker run` the image and hit `POST /process` with a sample document.

**Exit criteria:** `docker run` starts the service; a real document submitted via `curl` returns a valid compiled asset.

---

## ⬜ Phase 8 — Hardening & Observability (post-MVP)

**Goal:** Production readiness for internal rollout.

- [ ] Structured logging — add `python-json-logger`; enrich every log line with `id`, `block`, and `duration`; emit to stdout/stderr.
- [ ] Graceful shutdown — FastAPI lifespan cleanup; let in-flight requests drain before exit (so the orchestrator can do rolling deploys safely).
- [ ] Rate limiting — respect OpenRouter rate limits; add per-model backoff/throttling to absorb `429`s.
- [ ] Security — validate inbound payload size/shape; add an API-key check for Dometria Connect.
- [ ] Documentation — review the auto-generated OpenAPI spec; write an internal runbook for failure scenarios.

**Exit criteria:** The service is production-ready for the initial internal rollout.
