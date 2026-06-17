# Plan — Block 2: Contract Determination

> Implementation order. Each task group is a logical unit that can be code-reviewed independently.

---

## Task Group 1: Configuration Updates

**Files:** `src/bulk_insert/config.py`

1. Add `EMBEDDING_MODEL: str = "paraphrase-multilingual-MiniLM-L12-v2"` — sentence-transformers model for cosine similarity (multilingual, 384d).
2. Add `CONTRACT_MIN_SCORE: int = 5` — minimum `relevance_score` to include a chunk in contract determination voting.
3. Add `CONTRACT_TOP_K: int = 5` — maximum number of top-scoring chunks to use.

**Tests:** `tests/unit/test_config.py` (extend existing)
- Verify new default values load correctly.

---

## Task Group 2: Model Updates

**Files:** `src/bulk_insert/models/contract.py`, `src/bulk_insert/models/categories.py`, `src/bulk_insert/models/__init__.py`

1. **`contract.py`** — Update the `Contract` model:
   - Change `schema_def: dict[str, object] = Field(alias="schema")` to `schema_def: dict[str, object] = Field(default_factory=dict, alias="schema")` so that Block 2 can return a `Contract` without populating the schema.

2. **`contract.py`** — Add Instructor response model:
   ```python
   class Level1Prediction(BaseModel):
       """Instructor response model for lv1 classification.

       The Literal type constrains the LLM output to exactly
       the three valid level-1 category codes.
       """
       lv1: Level1Literal
   ```

3. **`categories.py`** — Add two helper functions:
   ```python
   def get_candidate_paths(lv1: str) -> list[tuple[str, str]]:
       """Return all valid (lv2, lv3) pairs under the given lv1."""
       ...

   def get_candidate_labels(lv1: str) -> list[tuple[tuple[str, str], str]]:
       """Return ((lv2, lv3), "Lv2Name > Lv3Name") for all candidates under lv1."""
       ...
   ```

4. **`__init__.py`** — Re-export `Level1Prediction`, `get_candidate_paths`, `get_candidate_labels`.

**Tests:** `tests/unit/test_models.py` (extend existing) or new `tests/unit/test_categories.py`
- `test_get_candidate_paths_commercial_activities`: verify all (lv2, lv3) pairs for `"commercial-activities"`.
- `test_get_candidate_paths_businesses`: verify the single lv2 (`"company-macro-sector"`) with 12 lv3.
- `test_get_candidate_paths_real_estate`: verify 3 lv2 groups with correct lv3 counts.
- `test_get_candidate_paths_invalid_lv1`: verify `KeyError` for unknown lv1.
- `test_get_candidate_labels_format`: verify label format `"Food & Beverage > Ristorante"`.
- `test_contract_default_schema`: verify `Contract` can be created with `lv1`, `lv2`, `lv3` only (empty schema_def).

---

## Task Group 3: Block 2 Core — LLM lv1 Extraction (Stage 1)

**Files:** `src/bulk_insert/blocks/contract.py`

1. Create the `blocks/contract.py` module with docstring explaining the two-stage approach.

2. Implement chunk filtering and selection:
   ```python
   def select_top_chunks(
       chunks: list[Chunk],
       min_score: int = 5,
       top_k: int = 5,
   ) -> list[Chunk]:
       """Filter chunks by min score and select top-k by relevance."""
       ...
   ```
   - Filter `relevance_score >= min_score` (exclude `None` scores).
   - Sort descending by `relevance_score`.
   - Return at most `top_k`.
   - If empty after filtering, raise `ContractDeterminationError`.

3. Define the custom exception:
   ```python
   class ContractDeterminationError(Exception):
       """Raised when contract determination fails."""
   ```

4. Implement the LLM extraction:
   ```python
   def extract_lv1(
       chunks: list[Chunk],
       client: instructor.Instructor,
   ) -> str:
       """Call LLM to classify chunk texts into a Level1Literal."""
       ...
   ```
   - Concatenate chunk texts (separated by `\n---\n`).
   - System prompt instructs classification into one of the 3 macro-categories.
   - Uses `response_model=Level1Prediction` with `max_retries=3`.
   - Returns the `lv1` string.

5. Add `_build_instructor_client(cfg)` (reuse pattern from `preprocessing.py`).

**Tests:** `tests/unit/test_contract.py`
- `test_select_top_chunks_filters_by_score`: chunks with scores [3, 5, 7, 9] → only [5, 7, 9].
- `test_select_top_chunks_limits_to_top_k`: 10 qualifying chunks, top_k=5 → returns 5.
- `test_select_top_chunks_empty_raises`: all chunks below threshold → `ContractDeterminationError`.
- `test_select_top_chunks_none_scores_excluded`: chunks with `relevance_score=None` excluded.
- `test_extract_lv1_mocked`: mock Instructor client, verify returned value is one of Level1Literal values.
- `test_extract_lv1_uses_level1prediction_model`: verify `response_model=Level1Prediction` is passed to the client call.

---

## Task Group 4: Block 2 Core — Cosine Similarity & Voting (Stage 2)

**Files:** `src/bulk_insert/blocks/contract.py` (continued)

1. Implement the embedding model loader:
   ```python
   @lru_cache(maxsize=1)
   def _get_embedding_model(model_name: str) -> SentenceTransformer:
       """Load and cache the sentence-transformers model."""
       ...
   ```

2. Implement the voting function:
   ```python
   def weighted_majority_vote(
       chunks: list[Chunk],
       lv1: str,
       model_name: str,
   ) -> tuple[str, str]:
       """Compute cosine similarity + weighted vote to determine (lv2, lv3).

       Returns:
           Tuple of (lv2_code, lv3_code).
       """
       ...
   ```
   - Call `get_candidate_labels(lv1)` → list of `((lv2, lv3), label_text)`.
   - Encode chunk texts and label texts via `model.encode()`.
   - Compute cosine similarity matrix via `model.similarity(chunk_embeddings, label_embeddings)`.
   - For each chunk: find the label with highest similarity → accumulate `relevance_score` weight to that candidate.
   - Return the `(lv2, lv3)` with the highest cumulative score.
   - Tiebreaker: highest single-chunk similarity.

3. Add detailed logging:
   - Log the top-3 candidates with their cumulative scores.
   - Log the winner.

**Tests:** `tests/unit/test_contract.py` (continued)
- `test_weighted_majority_vote_basic`: provide chunks whose text clearly maps to a known category (e.g., "ristorante con cucina tradizionale") → verify returns `("food-beverage", "restaurant")`.
- `test_weighted_majority_vote_weights_matter`: two chunks with different scores; higher-scored chunk should dominate.
- `test_weighted_majority_vote_all_candidates_generated`: verify all (lv2, lv3) pairs under the lv1 are considered.

> **Note:** These tests use the **real embedding model** (downloads on first run, ~100MB). They should run fast enough for unit tests since encoding is only ~10-20 texts.

---

## Task Group 5: Block 2 Orchestrator

**Files:** `src/bulk_insert/blocks/contract.py` (continued)

1. Implement the public entry point:
   ```python
   def determine_contract(
       chunks: list[Chunk],
       cfg: Settings | None = None,
   ) -> Contract:
       """Full Block 2 pipeline: chunks → Contract(lv1, lv2, lv3).

       Pipeline steps:
       1. Filter & select top-k chunks.
       2. Stage 1: LLM extracts lv1.
       3. Stage 2: Cosine similarity + weighted vote → (lv2, lv3).
       4. Assemble and return Contract.
       """
       ...
   ```
   - Step 1: `select_top_chunks(chunks, cfg.CONTRACT_MIN_SCORE, cfg.CONTRACT_TOP_K)`
   - Step 2: `extract_lv1(top_chunks, client)`
   - Step 3: `weighted_majority_vote(top_chunks, lv1, cfg.EMBEDDING_MODEL)`
   - Step 4: `Contract(lv1=lv1, lv2=lv2, lv3=lv3, schema_def={})`

2. Add structured logging at each step.

3. Export `__all__` with public functions.

**Tests:** `tests/unit/test_contract.py` (continued)
- `test_determine_contract_e2e_mocked`: mock LLM (returns known lv1), use real embeddings. Feed chunks with restaurant-related text → verify full `Contract(lv1="commercial-activities", lv2="food-beverage", lv3="restaurant")`.
- `test_determine_contract_empty_chunks_raises`: empty input → `ContractDeterminationError`.
- `test_determine_contract_all_low_scores_raises`: chunks all below threshold → `ContractDeterminationError`.

---

## Task Group 6: Blocks Package Update

**Files:** `src/bulk_insert/blocks/__init__.py`

1. Re-export `determine_contract` and `ContractDeterminationError` from `blocks/__init__.py`.

---

## Task Group 7: Quality Gates & Cleanup

1. Run `uv run ruff check . --fix && uv run ruff format .`
2. Run `uv run mypy src` — zero errors.
3. Run `uv run pytest tests/unit -v` — all tests pass (including new and existing).
4. Verify the sentence-transformers model downloads correctly on first run.
5. Review logging output with a manual dry run.
