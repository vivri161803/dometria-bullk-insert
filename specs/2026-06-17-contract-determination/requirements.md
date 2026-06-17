# Requirements — Block 2: Contract Determination

## Scope

This feature covers **Phase 3 (Block 2: Contract Determination)** from the [roadmap](file:///Users/vivri161803/Documents/Code/Dometria/bulk_insert/specs/roadmap.md). It implements the logic to determine the correct category path (`lv1` / `lv2` / `lv3`) from scored chunks produced by Block 1 (Preprocessing).

> This block does **NOT** load or populate the JSON schema (`schema_def`). Schema loading will be handled by Block 3 (Asset Extraction).

---

## Context

The Dometria Bulk Insert Pipeline must match each incoming document to one of 97 predefined JSON contracts (see [`contracts/`](file:///Users/vivri161803/Documents/Code/Dometria/bulk_insert/contracts)). Each contract belongs to a three-level category hierarchy defined in [`categories.py`](file:///Users/vivri161803/Documents/Code/Dometria/bulk_insert/src/bulk_insert/models/categories.py):

| Level | Example | Cardinality |
|---|---|---|
| **lv1** | `commercial-activities`, `businesses`, `real-estate` | 3 |
| **lv2** | `food-beverage`, `specialized-retail`, `company-macro-sector`, … | 12 |
| **lv3** | `restaurant`, `pharmacy`, `villa`, `hotel`, … | 97 |

The original architecture ([`architecture.md`](file:///Users/vivri161803/Documents/Code/Dometria/bulk_insert/architecture.md)) suggests a "graph matching" approach, but since the category structure is a simple tree with only 3 roots (not a complex graph), we adopt a **simpler two-stage approach**: LLM for lv1 → cosine similarity voting for lv2+lv3.

### Upstream dependency

Block 2 consumes the output of Block 1 ([`preprocessing.py`](file:///Users/vivri161803/Documents/Code/Dometria/bulk_insert/src/bulk_insert/blocks/preprocessing.py)): a `list[Chunk]` where each chunk has:
- `text`: coreference-resolved text
- `relevance_score`: integer 1–10
- `is_coref_resolved`: `True`

---

## Functional Requirements

### FR-1: Chunk Filtering & Selection

| ID | Requirement |
|---|---|
| FR-1.1 | The block MUST filter input chunks to include only those with `relevance_score ≥ CONTRACT_MIN_SCORE` (default: 5). |
| FR-1.2 | From the filtered set, the block MUST select the top-k chunks ordered by `relevance_score` descending (default: `CONTRACT_TOP_K = 5`). |
| FR-1.3 | Both `CONTRACT_MIN_SCORE` and `CONTRACT_TOP_K` MUST be configurable via `Settings`. |
| FR-1.4 | If no chunks pass the filter, the block MUST raise a descriptive error (`ContractDeterminationError`). |

### FR-2: Stage 1 — LLM Extraction of lv1

| ID | Requirement |
|---|---|
| FR-2.1 | The block MUST call the lightweight LLM (default GPT-4o-mini) via Instructor's `from_provider()` API to classify the concatenated top-k chunk texts into one of the three `Level1Literal` values: `"commercial-activities"`, `"businesses"`, `"real-estate"`. |
| FR-2.2 | The LLM response MUST be constrained to a Pydantic model `Level1Prediction` with a single field `lv1: Level1Literal`. The `Literal` type enforces that only valid values are returned. |
| FR-2.3 | The Instructor call MUST use `max_retries=3` for transient API failures. |
| FR-2.4 | The system prompt MUST instruct the LLM to determine the macro-category of the described asset based on the document content. |

### FR-3: Stage 2 — Cosine Similarity & Weighted Majority Vote

| ID | Requirement |
|---|---|
| FR-3.1 | Given the lv1 code from Stage 1, the block MUST expand `CATEGORY_TREE[lv1]` to generate all valid `(lv2, lv3)` candidate pairs. |
| FR-3.2 | For each candidate pair, the block MUST construct a human-readable label string using `CATEGORY_NAMES`, formatted as `"<lv2_name> > <lv3_name>"` (e.g., `"Food & Beverage > Ristorante"`). |
| FR-3.3 | The block MUST encode the top-k chunk texts and all candidate labels using a sentence-transformers model (`paraphrase-multilingual-MiniLM-L12-v2`). |
| FR-3.4 | The block MUST compute cosine similarity between each chunk embedding and each candidate label embedding using `model.similarity()`. |
| FR-3.5 | Each chunk MUST "vote" for the candidate with the highest cosine similarity. The vote weight is the chunk's `relevance_score` (integer 1–10). |
| FR-3.6 | The candidate `(lv2, lv3)` pair with the highest cumulative weighted score wins. |
| FR-3.7 | In case of a tie, the candidate with the highest single-chunk similarity score MUST be selected as tiebreaker. |

### FR-4: Contract Assembly & Output

| ID | Requirement |
|---|---|
| FR-4.1 | The block MUST return a `Contract` object with `lv1`, `lv2`, `lv3` populated from the winning path, and `schema_def` set to an empty dict `{}`. |
| FR-4.2 | The `Contract` model's `schema_def` field MUST be updated to accept `dict[str, object] | None` or default to `{}`, since Block 2 only determines the path — not the schema. |

### FR-5: Public API

| ID | Requirement |
|---|---|
| FR-5.1 | The block MUST expose a single public function `determine_contract(chunks: list[Chunk], cfg: Settings | None = None) -> Contract` as the entry point for Celery tasks. |
| FR-5.2 | All internal functions (chunk filtering, lv1 extraction, candidate generation, embedding, voting) MUST be independently importable and testable. |

---

## Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-1 | All new code MUST pass `mypy --strict` type checking. |
| NFR-2 | All new code MUST pass `ruff check` and `ruff format` without errors. |
| NFR-3 | The sentence-transformers model MUST be loaded lazily (first call) and cached for the lifetime of the process via `@lru_cache`. |
| NFR-4 | Logging MUST be present at each stage: chunk filtering stats, lv1 prediction result, candidate count, top-3 voted candidates with scores, final contract selection. |
| NFR-5 | The embedding model name MUST be configurable via `Settings.EMBEDDING_MODEL`. |
| NFR-6 | The function MUST be deterministic given the same input chunks and model weights (no temperature randomness in cosine similarity stage; LLM stage is non-deterministic but constrained to 3 values). |

---

## Design Decisions

| Decision | Rationale |
|---|---|
| **No graph matching engine** | The category hierarchy is a 3-level tree with 3 roots stored as a nested `dict`. A simple `CATEGORY_TREE[lv1].items()` iteration replaces any graph engine. Zero additional dependencies, zero synchronization overhead. |
| **LLM for lv1 only** | lv1 has only 3 possible values — an LLM can classify this reliably with a single constrained call. Using LLM for all 3 levels would be wasteful: lv2 has 12 values and lv3 has 97, making hallucination risk high. |
| **Cosine similarity + weighted vote for lv2+lv3** | After constraining the tree to a single lv1 branch, the number of lv2+lv3 candidates is small (max ~20). Cosine similarity between chunk embeddings and category labels is cheap, deterministic (no LLM call), and leverages the `relevance_score` from Block 1 as natural vote weights. |
| **`paraphrase-multilingual-MiniLM-L12-v2`** | Category labels are in Italian (`CATEGORY_NAMES`). A multilingual model handles the cross-lingual matching between document text (Italian) and labels (Italian) without translation overhead. 384-dimensional, fast enough for real-time. |
| **Top-k = 5** | Reduces LLM token cost while ensuring enough signal. 5 high-scoring chunks provide sufficient context for macro-category classification. |
| **Score threshold ≥ 5** | Filters out noisy chunks (boilerplate, headers, page numbers) that received low scores in Block 1. Keeps only chunks that the scoring LLM rated as moderately-to-highly relevant. |
| **`schema_def` not populated** | Block 2 is responsible only for determining the category path. Schema loading (reading the JSON contract file) is deferred to Block 3, maintaining clean separation of concerns. |
| **`Literal` type for LLM constraint** | Instructor + Pydantic `Literal` guarantees the LLM output is one of exactly 3 values. Eliminates post-hoc validation or string matching. See [Instructor docs](https://python.useinstructor.com/examples/single_classification). |

---

## Out of Scope

- Loading or populating the JSON schema (`schema_def`) — handled by Block 3.
- Block 3 (Asset Extraction) logic.
- Celery task wiring and async orchestration.
- Database persistence.
- FastAPI endpoints.
- Docker / deployment configuration.
