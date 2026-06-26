# Plan — Phase 5: Pipeline Orchestrator

> Numbered task groups. Each group is independently reviewable; complete them in
> order. No code is written until this spec is approved.

## 1. Result models (`models/pipeline.py`)

1.1 Create `src/bulk_insert/models/pipeline.py`.
1.2 Define `PipelineError(BaseModel)` with `stage: Literal["input","preprocess","contract","extract"]`,
    `category: Literal["bad_input","unprocessable","server","retryable"]`, `message: str`.
1.3 Define `PipelineResult(BaseModel)` with `id: str`, `ok: bool`,
    `asset: Asset | None = None`, `confidence: float | None = None`,
    `error: PipelineError | None = None`.
1.4 Add two small constructors / factory classmethods to keep the orchestrator terse:
    `PipelineResult.success(id, asset)` (pulls `confidence` from the asset) and
    `PipelineResult.failure(id, stage, category, message)`.
1.5 Declare `__all__`.

## 2. Orchestrator (`pipeline.py`)

2.1 Create `src/bulk_insert/pipeline.py` with module logger and `__all__`.
2.2 Implement
    `process_document(doc_id: str, text: str, cfg: Settings | None = None) -> PipelineResult`.
2.3 **Input guard** (D5): if `not text or not text.strip()` →
    `PipelineResult.failure(doc_id, "input", "bad_input", "...")`.
2.4 **Stage tracking**: maintain a `current_stage` variable set to `"preprocess"`,
    then `"contract"`, then `"extract"` before each block call, so the single
    `except` block records the correct stage for transient/unexpected errors.
2.5 **Happy path**: `preprocess` → `determine_contract` → `extract_asset`; return
    `PipelineResult.success(doc_id, asset)`.
2.6 **Exception mapping** (single `try` wrapping the three calls), in this order:
    - `except ContractDeterminationError` → `(contract, unprocessable)`.
    - `except (ContractNotFoundError, AssetExtractionError)` → `(extract, server)`.
    - `except (openai.APIError, openai.APIConnectionError, openai.RateLimitError, httpx.TransportError)` → `(current_stage, retryable)` (D4).
    - `except Exception` (catch-all, D3) → `(current_stage, server)`.
2.7 Log each transition (`info`) and each failure (`warning`/`error`) with `doc_id`
    and `stage`, consistent with existing block logging style.

## 3. Unit tests (`tests/unit/test_pipeline.py`)

3.1 Happy path: all three blocks mocked (monkeypatch the names imported into
    `pipeline`); assert `ok is True`, `asset` echoed, `confidence` propagated,
    `id == doc_id`.
3.2 Blank input: `""`, `"   "`, and `None`-like → `bad_input` / stage `input`;
    assert blocks are **not** called.
3.3 `ContractDeterminationError` raised by `determine_contract` → `unprocessable` /
    `contract`; `extract_asset` not called.
3.4 `ContractNotFoundError` and `AssetExtractionError` from `extract_asset` →
    `server` / `extract`.
3.5 Transient error: patch a block to raise `openai.APIConnectionError` (and
    `httpx.TransportError`) → `retryable`, with `stage` matching the failing block
    (parametrize across preprocess/contract/extract).
3.6 Unexpected error: a block raises a bare `ValueError` → `server` / current stage.
3.7 Invariant test: across every branch, `result.id == doc_id` and the orchestrator
    never raises.

## 4. Quality gates & docs

4.1 `uv run ruff check . && uv run ruff format`.
4.2 `uv run mypy src` (strict, zero errors) — confirm the `Literal` types and the
    `Asset | None` optionals narrow cleanly.
4.3 `uv run pytest tests/unit` green.
4.4 Update `CLAUDE.md`: move Phase 5 out of "Not yet implemented", document
    `pipeline.py` + `PipelineResult` in the Architecture section (same commit).
4.5 Tick Phase 5 boxes + mark ✅ in `specs/roadmap.md`.
