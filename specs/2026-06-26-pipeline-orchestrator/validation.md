# Validation — Phase 5: Pipeline Orchestrator

How to know the implementation is correct and mergeable.

## Exit criteria (from roadmap)

> Given `(id, text)`, returns a compiled-asset result or a structured error, both
> carrying the `id`.

## Functional checks

| # | Scenario | Expected `PipelineResult` |
|---|---|---|
| V1 | Valid text, all blocks succeed | `ok=True`, `asset` set, `confidence` = asset confidence, `error=None`, `id` echoed |
| V2 | `text=""` / `"   "` | `ok=False`, `error.stage="input"`, `error.category="bad_input"`; blocks never invoked |
| V3 | Block 2 raises `ContractDeterminationError` | `error.stage="contract"`, `category="unprocessable"`; Block 3 never invoked |
| V4 | Block 3 raises `ContractNotFoundError` | `error.stage="extract"`, `category="server"` |
| V5 | Block 3 raises `AssetExtractionError` | `error.stage="extract"`, `category="server"` |
| V6 | Any block raises `openai.APIConnectionError` / `RateLimitError` / `APIError` / `httpx.TransportError` | `category="retryable"`, `stage` = the failing block |
| V7 | Any block raises an unmodelled exception (e.g. `ValueError`) | `category="server"`, `stage` = the failing block |
| V8 | Every scenario above | `result.id == input id`; `process_document` itself never raises |
| V9 | Low extraction confidence (Block 3 returns a low-confidence `Asset`) | `ok=True` — low confidence is **not** an error (fail-safe); `confidence` reflects the value |

## Quality gates (must all be zero-error)

```bash
uv run ruff check . && uv run ruff format --check
uv run mypy src
uv run pytest tests/unit
```

- `mypy --strict` passes — `Literal` unions and the optional `Asset`/`confidence`
  fields type-check; `process_document`'s return type is exactly `PipelineResult`.
- New test file `tests/unit/test_pipeline.py` covers V1–V9 with all blocks mocked
  (no live LLM calls; no `-m integration` dependency).

## Statelessness / invariants

- Grep confirms `pipeline.py` performs no file/network I/O of its own (all I/O is
  inside Block 3's contract loading); no module-level mutable state.
- `doc_id` is never parsed or branched on — only echoed.
- `cfg` is forwarded to all three block calls (verifiable in tests via a sentinel
  `Settings` and asserting it is passed through).

## Documentation sync (part of the same PR)

- `CLAUDE.md`: Phase 5 removed from "Not yet implemented"; `pipeline.py` +
  `PipelineResult`/`PipelineError` described in Architecture.
- `specs/roadmap.md`: Phase 5 checkboxes ticked and heading marked ✅.

## Definition of done

All of V1–V9 pass, all three quality gates are green at zero errors, and the docs
above are updated in the same change set. The orchestrator is then ready to be
wired into the FastAPI service in Phase 6.
