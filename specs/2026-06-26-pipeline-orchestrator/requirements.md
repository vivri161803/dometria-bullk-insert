# Requirements — Phase 5: Pipeline Orchestrator

> Spec date: 2026-06-26 · Branch: `2026-06-26-pipeline-orchestrator`
> Roadmap phase: **Phase 5 — Pipeline Orchestrator** (`specs/roadmap.md`)

## 1. Scope

Compose the three existing pure-function blocks into a **single synchronous entry
point** — the glue currently missing between the blocks and the (Phase 6) FastAPI
service.

In scope:

- `src/bulk_insert/pipeline.py` exposing
  `process_document(doc_id: str, text: str, cfg: Settings | None = None) -> PipelineResult`.
- A `PipelineResult` Pydantic model plus a nested `PipelineError` model.
- Mapping every known block exception (and any unexpected exception) to a
  structured, **non-raising** result that always carries the `doc_id`.
- Unit tests covering the happy path and every error branch, all blocks mocked.

Out of scope (later phases):

- FastAPI app, routes, HTTP status-code mapping — **Phase 6**. The orchestrator only
  produces the structured outcome that Phase 6 will translate into status codes.
- Containerization, logging format, auth, rate limiting — Phases 7–8.
- Any change to Block 1/2/3 internals. The blocks are treated as a stable contract.

## 2. Block contracts consumed (existing, unchanged)

| Block | Entry point | Signature | Raises |
|---|---|---|---|
| 1 — preprocessing | `preprocess` | `(text: str, cfg) -> list[Chunk]` | (transient LLM errors propagate) |
| 2 — contract | `determine_contract` | `(chunks, cfg) -> Contract` | `ContractDeterminationError` (no qualifying chunk) |
| 3 — extraction | `extract_asset` | `(chunks, contract, cfg) -> Asset` | `ContractNotFoundError`, `AssetExtractionError` |

`Asset` (`models/asset.py`) = `{ contract_id: str, data: dict, confidence: float[0..1] }`.
Block 3 never raises on low confidence (fail-safe); it returns the best attempt.

Exception sources:
- `ContractDeterminationError` → `blocks/contract.py`
- `ContractNotFoundError` → `models/contract_loader.py`
- `AssetExtractionError` → `blocks/extraction.py`

## 3. Decisions (from the spec interview)

### D1 — `PipelineResult`: single model with optional fields
One model with an explicit `ok` discriminator; `asset`/`confidence` populated on
success, `error` populated on failure. Chosen for simplicity of consumption by the
Phase 6 route layer.

```python
class PipelineResult(BaseModel):
    id: str
    ok: bool
    asset: Asset | None = None
    confidence: float | None = None
    error: PipelineError | None = None
```

### D2 — `PipelineError`: stage + category + message
Carries enough structured data for Phase 6 to choose an HTTP status code without
re-inspecting exception types.

```python
class PipelineError(BaseModel):
    stage: Literal["input", "preprocess", "contract", "extract"]
    category: Literal["bad_input", "unprocessable", "server", "retryable"]
    message: str
```

### D3 — Catch-all → `server` error; the orchestrator never raises
The whole pipeline is wrapped so that any unanticipated exception becomes a
structured `server` error carrying the `id`. This matches the mission's
"fail-safe over fail-fast" principle: a request always returns a `PipelineResult`.

### D4 — LLM/transport failures → `retryable`
Transient upstream failures are recognized by matching SDK exception types and
mapped to `category="retryable"` (Phase 6 → `502`). Matched types:
`openai.APIError`, `openai.APIConnectionError`, `openai.RateLimitError`, and
`httpx.TransportError`. These can surface from **any** block that makes an LLM call
(Block 1 scoring, Block 2 lv1 classification, Block 3 extraction), so the `stage`
recorded is whichever block was executing when the error was raised.

> Ordering note: `RateLimitError`/`APIConnectionError` subclass `APIError`, so a
> single `except (APIError, APIConnectionError, RateLimitError, httpx.TransportError)`
> clause suffices; it must be matched **before** the generic catch-all.

### D5 — Blank-input guard: whitespace-only
The first guard, before any block runs: reject when `text` is `None`, empty, or
whitespace-only (`not text or not text.strip()`) → `stage="input"`,
`category="bad_input"`. No minimum-length tunable is introduced.

## 4. Error mapping (authoritative)

| Trigger | `stage` | `category` | Phase 6 status (informational) |
|---|---|---|---|
| `text` empty / blank | `input` | `bad_input` | `400` |
| `ContractDeterminationError` | `contract` | `unprocessable` | `422` |
| `ContractNotFoundError` | `extract` | `server` | `500` |
| `AssetExtractionError` | `extract` | `server` | `500` |
| `openai`/`httpx` transient error (any block) | current block | `retryable` | `502` |
| any other unexpected `Exception` | current block | `server` | `500` |

The Phase 6 column is documented here only to keep the category set aligned with the
downstream mapping; the orchestrator itself emits **no** HTTP concepts.

## 5. Stateless guarantee

- No I/O beyond reading the bundled `contracts/` directory (done inside Block 3).
- Nothing is persisted; the orchestrator holds no module-level mutable state.
- `doc_id` is **opaque** — never parsed, validated, or used for control flow; it is
  only echoed onto the result (success and error alike).
- `cfg` override is threaded to every block for testability; default is the
  process-level `settings` singleton.

## 6. Context references

- `specs/mission.md` — §"Synchronous Orchestration", principle 4 (fail-safe).
- `specs/tech-stack.md` — §"Orchestration & Concurrency".
- `CLAUDE.md` — "Status & not-yet-built" item 1.
