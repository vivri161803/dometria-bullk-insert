# Requirements — Preprocessing & Data Models

## Scope

This feature covers **Phase 1 (Pydantic Models & Config)** and **Phase 2 (Block 1: Preprocessing)** from the [roadmap](file:///Users/vivri161803/Documents/Code/Dometria/bulk_insert/specs/roadmap.md) as a single deliverable. Phase 1 defines the shared data structures and application configuration that Phase 2 depends on.

---

## Context

The Dometria Bulk Insert Pipeline receives raw document text from Dometria Connect and must extract structured data into schema-compliant JSON assets. **Preprocessing** is the first computational block in the pipeline: it transforms raw text into scored, semantically meaningful chunks that downstream blocks (Contract Determination, Asset Extraction) consume.

Without high-quality preprocessing, downstream extraction accuracy degrades. Coreference resolution and relevance scoring are critical to ensure that:
1. Pronouns and anaphoric references are resolved before chunking, so chunks are self-contained.
2. Low-relevance chunks are deprioritized, reducing noise and LLM token costs in later stages.

---

## Functional Requirements

### FR-1: Application Configuration (`config.py`)

| ID | Requirement |
|---|---|
| FR-1.1 | Configuration MUST be defined using `pydantic-settings` `BaseSettings`, loading values from environment variables and a `.env` file. |
| FR-1.2 | The following settings MUST be configurable: OpenRouter API key, OpenRouter base URL, lightweight LLM model name, heavy LLM model name, Redis URL, PostgreSQL DSN, chunk size (in sentences), chunk overlap (in sentences). |
| FR-1.3 | All LLM provider and model settings MUST be configurable without code changes (swap providers/models by editing `.env`). |
| FR-1.4 | A singleton `settings` instance MUST be importable from `bulk_insert.config`. |

### FR-2: Pydantic Data Models

| ID | Requirement |
|---|---|
| FR-2.1 | A `Chunk` model MUST represent a text chunk with fields: `index` (int), `text` (str — holds the coreference-resolved text after the LLM step), `start_sentence` (int), `end_sentence` (int), `overlap_previous` (int — number of overlapping sentences with previous chunk), `is_coref_resolved` (bool — `False` at creation, `True` after the combined LLM call), `relevance_score` (int, 1–10, optional — populated after scoring). |
| FR-2.2 | A `ChunkScore` model MUST be retained for isolated scoring unit tests. Fields: `score` (int, 1–10, constrained via `Field(ge=1, le=10)`). |
| FR-2.3 | A `ChunkAnalysis` model MUST be used as the Instructor response schema for the combined scoring + coreference resolution LLM call. Fields: `score` (int, 1–10, constrained via `Field(ge=1, le=10)`), `resolved_text` (str — the chunk text rewritten with coreferences resolved). |
| FR-2.4 | A `Contract` model MUST represent the target JSON schema with hierarchical fields: `lv1` (str), `lv2` (str), `lv3` (str), plus a `schema` field (dict) holding the actual JSON schema to populate. |
| FR-2.5 | An `Asset` model MUST represent the final extracted output: `contract_id` (str), `data` (dict — the populated JSON), `confidence` (float, 0–1). |
| FR-2.6 | A `Job` model MUST track pipeline state with fields: `job_id` (UUID), `status` (enum: `PENDING`, `PREPROCESSING`, `CONTRACTING`, `EXTRACTING`, `DONE`, `FAILED`), `created_at` (datetime), `updated_at` (datetime), `block_results` (dict — keyed by block name, storing intermediate outputs). |
| FR-2.7 | A `JobStatus` enum MUST enforce valid state transitions: `PENDING → PREPROCESSING → CONTRACTING → EXTRACTING → DONE`, with `FAILED` reachable from any state. |

### FR-3: Text Preprocessing (Block 1)

| ID | Requirement |
|---|---|
| FR-3.1 | The preprocessing module MUST accept raw text (str) as input and return a list of scored, coreference-resolved `Chunk` objects. |
| FR-3.2 | **Sentence segmentation**: Raw text MUST be segmented into sentences using spaCy's `Sentencizer` component on a blank `en` pipeline (no model download required). |
| FR-3.3 | **Chunking with overlap**: Sentences MUST be grouped into chunks using a sliding window approach. Default: 6 sentences per chunk, 2 sentences overlap with the previous chunk. Both values MUST be configurable via `Settings`. Freshly created chunks have `is_coref_resolved=False`. |
| FR-3.4 | **Combined scoring + coreference resolution**: Each chunk MUST be processed by a single LLM call (lightweight model, default GPT-4o-mini) via Instructor's `from_provider()` API, using the `ChunkAnalysis` Pydantic model as `response_model`. The call returns both: (a) `score` — an integer 1–10 representing how useful the chunk is for structured data extraction; (b) `resolved_text` — the chunk text rewritten with all pronouns/anaphora replaced by their explicit referents. After this call, `chunk.text` is replaced by `resolved_text` and `chunk.is_coref_resolved` is set to `True`. |
| FR-3.5 | The preprocessing module MUST be a pure function (no side effects beyond LLM API calls), independently importable and testable. |

---

## Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-1 | All models MUST pass `mypy --strict` type checking. |
| NFR-2 | All models MUST serialize to JSON and deserialize back without data loss (round-trip fidelity). |
| NFR-3 | Preprocessing MUST handle empty text input gracefully (return empty list, no crash). |
| NFR-4 | LLM scoring calls MUST include retry logic (via Instructor's built-in `max_retries`) for transient API failures. |
| NFR-5 | Logging MUST be present at each preprocessing step (sentence count, chunk count, scoring + coref results) using Python `logging` with structured context. |

---

## Design Decisions

| Decision | Rationale |
|---|---|
| **Sentence-based chunking** (not token-based) | Sentences are the natural semantic unit for NLP. Token-based chunking risks splitting mid-sentence, breaking context. Sentence-based chunking with overlap ensures each chunk is semantically coherent and overlaps provide continuity. |
| **Sliding window with overlap** | Overlap (default 2 sentences) ensures that information at chunk boundaries is not lost. Downstream blocks see redundant context, which improves extraction recall. |
| **Combined scoring + coreference resolution in one LLM call** | Coreference resolution is performed during the LLM scoring call rather than as a separate step. This approach: (1) requires zero additional LLM calls compared to score-only; (2) is unaffected by library compatibility issues (no coreferee/spacy-experimental for spaCy 3.8/Python 3.13); (3) resolves pronouns in the semantic context of each chunk, which is sufficient for downstream extraction. The `ChunkAnalysis` response model captures both outputs in a single structured call. |
| **Score as integer 1–10** (not float 0–1) | User preference. Discrete integer scale is simpler for human inspection and LLM instruction. The `ChunkAnalysis` Pydantic model constrains the range via `Field(ge=1, le=10)`. |
| **`instructor.from_provider()` API** | Current recommended API in Instructor docs. Cleaner than the legacy `patch()` approach. Configured to point at OpenRouter's OpenAI-compatible endpoint. |
| **`pydantic-settings` with `.env`** | All configuration (API keys, model names, database URLs) stored in `.env`, loaded automatically by `BaseSettings`. Allows switching LLM providers/models without code changes, per user requirement. |
| **`is_coref_resolved=False` at chunk creation** | Signals clearly that coreference resolution has not yet occurred. Set to `True` atomically with `relevance_score` by `score_and_resolve_chunk()`, ensuring the field accurately reflects the state of the `text` field. |

---

## Out of Scope

- Block 2 (Contract Determination) and Block 3 (Asset Extraction) logic.
- Celery task wiring and async orchestration.
- Database persistence (SQLAlchemy, Alembic).
- FastAPI endpoints.
- Docker / deployment configuration.
