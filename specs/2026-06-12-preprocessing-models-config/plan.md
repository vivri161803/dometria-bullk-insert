# Plan — Preprocessing & Data Models

> Implementation order. Each task group is a logical unit that can be code-reviewed independently.

---

## Task Group 1: Application Configuration

**Files:** `src/bulk_insert/config.py`, `.env.example`

1. Replace the placeholder `config.py` with a `pydantic-settings` `BaseSettings` class.
2. Define settings fields:
   - `OPENROUTER_API_KEY: str`
   - `OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"`
   - `MODEL_LIGHTWEIGHT: str = "openai/gpt-4o-mini"`
   - `MODEL_HEAVY: str = "openai/gpt-4o"`
   - `REDIS_URL: str = "redis://localhost:6379/0"`
   - `DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/dometria_bulk"`
   - `CHUNK_SIZE: int = 6` (sentences per chunk)
   - `CHUNK_OVERLAP: int = 2` (overlapping sentences)
3. Configure `model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")`.
4. Export a singleton `settings = Settings()`.
5. Create `.env.example` with all keys and placeholder/default values (no secrets).

**Tests:** `tests/unit/test_config.py`
- Verify settings load from environment variables.
- Verify default values are applied when env vars are absent.
- Verify `.env` file loading works.

---

## Task Group 2: Core Pydantic Models

**Files:** `src/bulk_insert/models/__init__.py`, `src/bulk_insert/models/chunk.py`, `src/bulk_insert/models/contract.py`, `src/bulk_insert/models/asset.py`, `src/bulk_insert/models/job.py`

1. Create the `models/` package with `__init__.py` re-exporting all models.
2. **`chunk.py`**:
   - Define `Chunk(BaseModel)` with fields: `index`, `text`, `start_sentence`, `end_sentence`, `overlap_previous`, `is_coref_resolved` (starts `False`, set `True` after the LLM call), `relevance_score` (optional int 1–10).
   - Define `ChunkScore(BaseModel)` with field: `score` (int, `Field(ge=1, le=10)`) — retained for isolated unit tests.
   - Define `ChunkAnalysis(BaseModel)` with fields: `score` (int, `Field(ge=1, le=10)`), `resolved_text` (str) — used as `response_model` for the combined scoring + coreference LLM call.
3. **`contract.py`**:
   - Define `Contract(BaseModel)` with fields: `lv1`, `lv2`, `lv3`, `schema` (dict, via alias `schema_def`).
4. **`asset.py`**:
   - Define `Asset(BaseModel)` with fields: `contract_id`, `data` (dict), `confidence` (float, 0–1).
5. **`job.py`**:
   - Define `JobStatus(StrEnum)` with values: `PENDING`, `PREPROCESSING`, `CONTRACTING`, `EXTRACTING`, `DONE`, `FAILED`.
   - Define `Job(BaseModel)` with fields: `job_id` (UUID), `status` (JobStatus), `created_at`, `updated_at`, `block_results` (dict).

**Tests:** `tests/unit/test_models.py`
- JSON round-trip serialization for each model.
- Validation edge cases (e.g., `relevance_score` outside 1–10 raises `ValidationError`).
- `ChunkAnalysis` score + resolved_text round-trip.
- `JobStatus` enum transition logic.

---

## Task Group 3: spaCy NLP Pipeline Setup

**Files:** `src/bulk_insert/blocks/__init__.py`, `src/bulk_insert/blocks/preprocessing.py`

1. Create the `blocks/` package.
2. In `preprocessing.py`, implement a function to load the spaCy pipeline:
   - Use `spacy.blank("en")` and add the `sentencizer` component (no model download required).
   - The pipeline loading MUST be lazy (load once on first call, cache the `nlp` object via `@lru_cache`).
3. Implement `segment_sentences(text: str) -> list[str]`:
   - Use spaCy's sentence segmentation (via `doc.sents`).
   - Return a list of sentence strings.

> **Note:** Coreference resolution is NOT a separate NLP pipeline step. It is performed during the LLM scoring call (Task Group 4). Neither `coreferee` nor `spacy-experimental` support spaCy 3.8 / Python 3.13; the LLM-based approach is superior and requires no additional dependencies.

**Tests:** `tests/unit/test_preprocessing_nlp.py`
- Test sentence segmentation with multi-sentence text.
- Test empty input returns empty list.

---

## Task Group 4: Chunking Logic

**Files:** `src/bulk_insert/blocks/preprocessing.py` (continued)

1. Implement `create_chunks(sentences: list[str], chunk_size: int, overlap: int) -> list[Chunk]`:
   - Sliding window over the sentence list.
   - Each chunk joins `chunk_size` consecutive sentences.
   - Successive chunks overlap by `overlap` sentences.
   - Populate `Chunk` fields: `index`, `text` (joined sentences), `start_sentence`, `end_sentence`, `overlap_previous`, `is_coref_resolved=False`.
   - `relevance_score` left as `None` (populated in Task Group 5).
2. Handle edge cases:
   - Input shorter than `chunk_size` → single chunk containing all sentences.
   - Last chunk may be shorter than `chunk_size`.

**Tests:** `tests/unit/test_chunking.py`
- 10 sentences with chunk_size=3, overlap=1 → verify correct number of chunks, correct sentence ranges, correct overlap values.
- Single sentence → single chunk.
- Empty input → empty list.
- Overlap=0 → non-overlapping chunks.
- Verify `is_coref_resolved=False` on all freshly created chunks.

---

## Task Group 5: Combined LLM Scoring + Coreference Resolution

**Files:** `src/bulk_insert/blocks/preprocessing.py` (continued)

1. Implement `score_and_resolve_chunk(chunk: Chunk, client: instructor.Instructor) -> Chunk`:
   - Call the LLM via `client.chat.completions.create(response_model=ChunkAnalysis, ...)` with a system prompt instructing the model to:
     (a) rate the chunk's relevance for structured data extraction (1–10);
     (b) rewrite the text replacing all pronouns/anaphora with their explicit referents.
   - Return a new chunk (via `model_copy`) with `text=analysis.resolved_text`, `relevance_score=analysis.score`, `is_coref_resolved=True`.
2. Implement `score_and_resolve_chunks(chunks: list[Chunk], cfg: Settings) -> list[Chunk]`:
   - Create the Instructor client: `instructor.from_provider("openai/<model>", base_url=..., api_key=...)`.
   - Iterate over chunks, calling `score_and_resolve_chunk` for each.
   - Include retry logic (`max_retries=3`) for transient API errors.
   - Log each chunk's score.
3. Consider batching or async scoring for performance (design note — implement sequentially first, optimize later).

**Tests:** `tests/unit/test_scoring.py`
- Mock the Instructor client; verify `ChunkAnalysis` schema is used as `response_model`.
- Verify `score` is written to `chunk.relevance_score`.
- Verify `resolved_text` replaces `chunk.text`.
- Verify `is_coref_resolved=True` after the call.
- Verify the original chunk is not mutated (immutability via `model_copy`).
- Verify invalid scores (outside 1–10) are rejected by Pydantic.

---

## Task Group 6: Top-Level Preprocessing Orchestrator

**Files:** `src/bulk_insert/blocks/preprocessing.py` (continued)

1. Implement `preprocess(text: str, cfg: Settings) -> list[Chunk]`:
   - Three-step pipeline: `segment_sentences(text)` → `create_chunks(sentences, cfg.CHUNK_SIZE, cfg.CHUNK_OVERLAP)` → `score_and_resolve_chunks(chunks, cfg)`.
   - Add structured logging at each step (sentence count, chunk count, scoring + coref summary).
   - Handle empty text gracefully (return `[]`).
2. This is the public entry point that Celery tasks (Phase 6) will call.

**Tests:** `tests/unit/test_preprocess_pipeline.py`
- End-to-end unit test with mocked LLM: raw text → list of scored, coref-resolved Chunks.
- Verify all fields populated correctly (`relevance_score`, `is_coref_resolved=True`, `text` replaced).
- Verify logging output (using `caplog` fixture).

**Integration test:** `tests/integration/test_preprocess_live.py`
- Real LLM call on a small sample text (gated behind `pytest.mark.integration`).

---

## Task Group 7: Quality Gates & Cleanup

1. Run `uv run ruff check . --fix && uv run ruff format .`
2. Run `uv run mypy src` — zero errors.
3. Run `uv run pytest tests/unit` — all pass.
4. Update `tests/unit/__init__.py` and `tests/integration/__init__.py` if needed.
5. Delete `tests/unit/test_placeholder.py` (replaced by real tests).
