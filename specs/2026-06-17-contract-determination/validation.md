# Validation — Block 2: Contract Determination

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
| `uv run pytest tests/unit -v` | All tests pass (existing + new) |

**Required test coverage (by focus area):**

| Test file | What it validates |
|---|---|
| `test_contract.py` → `select_top_chunks` | Chunk filtering by min score; top-k limiting; empty-after-filter raises `ContractDeterminationError`; `None` scores excluded |
| `test_contract.py` → `extract_lv1` | Mocked Instructor client: verifies `Level1Prediction` response model is used; returned value is valid `Level1Literal` |
| `test_contract.py` → `weighted_majority_vote` | Real embeddings: restaurant text → `("food-beverage", "restaurant")`; weight dominance verified; all candidates under lv1 considered |
| `test_contract.py` → `determine_contract` | End-to-end with mocked LLM + real embeddings: correct `Contract` output; error cases (empty chunks, all-low-scores) |
| `test_categories.py` or `test_models.py` | `get_candidate_paths` correctness for all 3 lv1; `get_candidate_labels` format validation; `Contract` default schema |
| `test_config.py` (extended) | New settings fields (`EMBEDDING_MODEL`, `CONTRACT_MIN_SCORE`, `CONTRACT_TOP_K`) load with correct defaults |

### 3. Existing Tests — Regression

| Command | Expected |
|---|---|
| `uv run pytest tests/unit/test_chunking.py -v` | All pass (unchanged) |
| `uv run pytest tests/unit/test_scoring.py -v` | All pass (unchanged) |
| `uv run pytest tests/unit/test_preprocess_pipeline.py -v` | All pass (unchanged) |
| `uv run pytest tests/unit/test_models.py -v` | All pass (extended with new model tests) |

### 4. Integration Test (optional, requires API key)

| Command | Expected |
|---|---|
| `uv run pytest tests/integration -v -m integration` | Passes with a valid `OPENROUTER_API_KEY` in `.env` |

Integration test flow:
1. Feed a known Italian real-estate document text through `preprocess()` → chunks.
2. Feed chunks through `determine_contract()` with a real LLM call.
3. Verify the returned `Contract` has valid `lv1`, `lv2`, `lv3` values that exist in `CATEGORY_TREE`.

> This test is gated behind `@pytest.mark.integration` and will NOT run in the standard `pytest` invocation or pre-commit hooks.

---

## Manual Verification Checklist

- [ ] **Helper functions**: Run `python -c "from bulk_insert.models.categories import get_candidate_paths; print(len(get_candidate_paths('commercial-activities')))"` — should print a number ≥ 40 (total lv2×lv3 combinations under `commercial-activities`).

- [ ] **Label format**: Run `python -c "from bulk_insert.models.categories import get_candidate_labels; print(get_candidate_labels('real-estate')[:3])"` — verify output format is `(('specialist-destinations', 'buildable-area'), 'Destinazioni specialistiche > Area edificabile')`.

- [ ] **Contract creation without schema**: Run `python -c "from bulk_insert.models import Contract; c = Contract(lv1='real-estate', lv2='specialist-destinations', lv3='villa'); print(c.model_dump())"` — verify `schema_def` defaults to `{}`.

- [ ] **Embedding model loads**: Run `python -c "from sentence_transformers import SentenceTransformer; m = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2'); print(m.encode(['test']).shape)"` — should print `(1, 384)`.

- [ ] **Dry run with real embedding**: Create a small script that constructs fake chunks with Italian restaurant text, runs `weighted_majority_vote(chunks, 'commercial-activities', 'paraphrase-multilingual-MiniLM-L12-v2')`, and verify it returns `("food-beverage", "restaurant")`.

---

## Merge Criteria

All of the following MUST be true before merging into `main`:

1. All automated checks pass (ruff, mypy, pytest unit tests).
2. No `# type: ignore` annotations added without an accompanying comment explaining why.
3. Existing tests from Block 1 / Phase 1 still pass (no regressions).
4. `config.py` has all new settings with correct defaults.
5. `contract.py` block module has `__all__` exports defined.
6. Sentence-transformers model name is configurable via `Settings.EMBEDDING_MODEL`.
7. Logging is present at each pipeline step (chunk filtering, lv1 extraction, voting, final result).
