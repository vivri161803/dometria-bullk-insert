# Validation — Preprocessing & Data Models

> How to know the implementation succeeded and is ready to merge.

---

## Automated Checks (all MUST pass)

### 1. Static Analysis

| Check | Command | Expected |
|---|---|---|
| Linting | `uv run ruff check .` | 0 errors |
| Formatting | `uv run ruff format --check .` | 0 files would be reformatted |
| Type checking | `uv run mypy src` | Success: no issues found |

### 2. Unit Tests

| Command | Expected |
|---|---|
| `uv run pytest tests/unit -v` | All tests pass |

**Required test coverage (by module):**

| Test file | What it validates |
|---|---|
| `test_config.py` | Settings load from env vars; defaults are correct; `.env` file is read |
| `test_models.py` | All Pydantic models serialize/deserialize correctly; validation rejects invalid data (e.g., score=15 raises `ValidationError`); `JobStatus` enum has all expected values |
| `test_preprocessing_nlp.py` | Sentence segmentation returns correct sentence list; coreference resolution replaces pronouns with referents |
| `test_chunking.py` | Correct number of chunks produced for known inputs; overlap boundaries are exact; edge cases (empty input, single sentence, short text) handled |
| `test_scoring.py` | Mock Instructor client is called with `ChunkScore` response_model; score is written to `chunk.relevance_score`; Pydantic rejects out-of-range scores |
| `test_preprocess_pipeline.py` | End-to-end pipeline (with mocked LLM): raw text → list of scored Chunks with all fields populated |

### 3. Integration Test (optional, requires API key)

| Command | Expected |
|---|---|
| `uv run pytest tests/integration -v -m integration` | Passes with a valid `OPENROUTER_API_KEY` in `.env` |

This test calls the real LLM API on a small sample text and verifies:
- The returned `ChunkScore.score` is an integer between 1 and 10.
- The overall pipeline produces a non-empty list of `Chunk` objects.

> This test is gated behind `@pytest.mark.integration` and will NOT run in the standard `pytest` invocation or pre-commit hooks.

---

## Manual Verification Checklist

- [ ] **Config loads correctly**: Create a `.env` with custom values, run `python -c "from bulk_insert.config import settings; print(settings.MODEL_LIGHTWEIGHT)"` and verify the custom value is printed.
- [ ] **Model inspection**: Run `python -c "from bulk_insert.models import Chunk; print(Chunk.model_json_schema())"` and verify the JSON schema looks correct.
- [ ] **Preprocessing dry run**: Run a quick script that calls `preprocess("Some multi-sentence text...")` with a mocked or real LLM and inspect the output chunks in the console.

---

## Merge Criteria

All of the following MUST be true before merging into `main`:

1. All automated checks pass (ruff, mypy, pytest unit tests).
2. No `# type: ignore` annotations added without an accompanying comment explaining why.
3. `.env.example` contains all required keys with placeholder values.
4. No secrets committed (`.env` is in `.gitignore`).
5. All new files follow the project layout defined in [tech-stack.md](file:///Users/vivri161803/Documents/Code/Dometria/bulk_insert/specs/tech-stack.md).
