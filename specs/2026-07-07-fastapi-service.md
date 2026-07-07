# FastAPI Synchronous Service — Phase 7 Spec

> **Status:** proposed · **Date:** 2026-07-07 · **Roadmap slot:** Phase 7
> (`specs/roadmap.md`), after Phases 6–6.10 (tree-driven extraction, compile,
> clustering, normalization, grounded resolution).
>
> This is a **documentation-only** spec. No code is produced here — it defines
> the API contract, module layout, startup behaviour, status-code mapping,
> concurrency model, test plan, and dependency cleanup that a later
> implementation step will build.

---

## 1. Why this exists — the last-mile adapter

The pipeline is complete and fully tested from the CLI
(`cli.py:bulk-insert-cli`), but the production caller — Dometria Connect — needs
an HTTP endpoint, not a shell command. Phase 7 is a **thin adapter layer** over
`pipeline.process_document` (which already never raises and returns a structured
`PipelineResult`). The API maps `PipelineResult.error.category` to an HTTP
status code, echoes `id`, and adds health probes. No pipeline logic moves into
the API layer.

### 1.1 Design principles

- **No business logic in the API layer.** The route handler calls
  `process_document`, maps the result, and returns. Any extraction/validation
  logic stays in the pipeline or its blocks.
- **Stateless.** No database, no session, no file writes. The only I/O is
  reading bundled `contracts/` and `artifacts/` (already handled by Block 3).
- **Fail-fast on misconfiguration.** If the offline artifacts (field tree,
  compiled sidecar, topics) are missing or invalid at startup, the app **fails
  to start** with a clear error — traffic is never routed to a broken container.
- **Single-worker concurrency.** One uvicorn worker, one request at a time per
  container. Concurrency is pushed to the platform (more replicas behind a load
  balancer), consistent with `tech-stack.md` and the blocking-synchronous
  pipeline design.

---

## 2. Surface `completeness` on `Asset` (small Block 3 change)

The grounding stage already computes
`completeness = mandatory_filled / mandatory_total` (`ground.py`, line ~209) but
folds it into the `confidence` scalar (as `grounding × completeness`) and
discards the standalone value. Phase 7 carries it out as a first-class field.

### 2.1 Changes

1. **`GroundResult`** — add `completeness: float = 0.0` (parallel to
   `confidence`). Assign
   `out.completeness = mandatory_filled / mandatory_total if mandatory_total else 1.0`
   (the same expression currently used only as a factor).

2. **`Asset`** — add `completeness: float | None = None` with
   `Field(ge=0.0, le=1.0, default=None)`. Semantics: the fraction of mandatory
   fields filled after extraction. `None` when the product has no mandatory
   fields *or* the extractor predates this field (backward-compatible default).

3. **`orchestrator.py`** — wire `grounded.completeness` into
   `Asset(…, completeness=grounded.completeness)`.

4. **`PipelineResult.success`** — the factory already lifts `confidence` from
   the asset; do **not** lift `completeness` here (lifting is done in the API
   response schema, §3.2, to keep the pipeline model unchanged).

5. **Unit test** — add one test in the Block 3 suite: an extraction with 3
   mandatory fields (2 filled, 1 null) should produce
   `asset.completeness ≈ 0.667`. Existing tests stay green (the new field is
   optional/defaulted).

### 2.2 Why a separate scalar

`review_queue` lists *which* mandatory fields are missing;
`completeness` is the *proportion* — a single number a caller can threshold on
(e.g. "reject below 0.8") without counting list items. Together they give both
"how bad" and "what's missing".

---

## 3. API schemas (`src/bulk_insert/api/schemas.py`)

### 3.1 Request

```python
class ProcessRequest(BaseModel):
    """Inbound extraction request from Dometria Connect."""
    id: str
    text: str
```

Validation: `text` must be non-empty (Pydantic `min_length=1` or a
`@field_validator`). An empty/whitespace `text` is also caught downstream by
`pipeline.process_document`, but validating at the schema level gives a cleaner
`422` from FastAPI before the pipeline runs.

### 3.2 Success response

```python
class ProcessResponse(BaseModel):
    """Successful extraction — the full Block 3 v2 Asset + lifted scalars."""
    id: str
    asset: Asset
    confidence: float
    completeness: float | None
    _models: dict[str, str]
```

- **`asset`** — the full Block 3 v2 `Asset`: `contract_id`, `data`,
  `confidence`, `evidence`, `flags`, `review_queue`, `completeness` all
  serialize **nested inside `asset` as-is**. The API adds no top-level review
  envelope — callers read `asset.review_queue`.
- **`confidence`** — lifted from `asset.confidence` (mirrors how
  `PipelineResult.success` lifts it, and how the CLI does it).
- **`completeness`** — lifted from `asset.completeness` so a caller can read the
  mandatory-completion percentage without descending into `asset`.
- **`_models`** — `{lightweight, heavy, embedding}` from
  `Settings.MODEL_LIGHTWEIGHT` / `MODEL_HEAVY` / `EMBEDDING_MODEL` (cf.
  `cli.py:_models_used`). Observability/debugging parity with the CLI's
  `--json` output.

> **Design note:** `_models` is prefixed with `_` to signal it is metadata, not
> extraction output. It is a plain `dict[str, str]` in the response body, **not**
> a Pydantic field alias — FastAPI serializes it literally as `_models`.

### 3.3 Error response

```python
class ErrorResponse(BaseModel):
    """Structured error — echoes id + the PipelineError detail."""
    id: str
    error: PipelineError
```

The `PipelineError` model (`models/pipeline.py`) already carries `stage`,
`category`, and `message` — reused directly, no wrapper needed.

### 3.4 Non-empty `review_queue` is a 200

A successful extraction with a non-empty `review_queue` (some mandatory fields
missing) is **not** an error — it is a successful extraction with incomplete
mandatory coverage. The caller reads `asset.review_queue` to decide what to do.
The HTTP response is **200**, not 4xx/5xx. This is consistent with the fail-safe
philosophy: low confidence and missing optional/mandatory fields never raise.

---

## 4. Routes (`src/bulk_insert/api/routes.py`)

### 4.1 `POST /process`

- Synchronous `def` (not `async def`) — runs in the default threadpool (a single
  uvicorn worker means one thread, one request at a time).
- Calls `process_document(request.id, request.text)`.
- Maps the `PipelineResult` to either `ProcessResponse` (200) or
  `ErrorResponse` (4xx/5xx) based on `result.error.category`.

### 4.2 Status-code mapping

Driven by `PipelineResult.error.category`, **not** by re-inspecting exceptions
(the pipeline never raises):

| `error.category` | HTTP | Meaning |
|---|---|---|
| *(success — `ok=True`)* | **200** | Extraction completed (incl. low confidence and non-empty `review_queue`) |
| `bad_input` | **400** | Empty/blank text |
| `unprocessable` | **422** | `ContractDeterminationError` — no qualifying chunk; document cannot be categorized |
| `retryable` | **502** | Transient upstream LLM/network failure |
| `server` | **500** | Missing/malformed schema, catch-all |

Every body — success or error — echoes the `id`.

Implementation: a simple `dict[ErrorCategory, int]` lookup:

```python
_STATUS_MAP: dict[str, int] = {
    "bad_input": 400,
    "unprocessable": 422,
    "retryable": 502,
    "server": 500,
}
```

The route handler returns a `JSONResponse` with the mapped status code for
errors, or a plain `ProcessResponse` for success.

### 4.3 `GET /health` — liveness probe

Returns `200 {"status": "ok"}`. No dependencies checked. Serves as the
Kubernetes/orchestrator liveness probe: "the process is running and accepting
TCP connections."

### 4.4 `GET /ready` — readiness probe

Returns `200 {"status": "ready"}` **only** once:

1. The spaCy pipeline is loaded (`preprocessing._get_nlp()`).
2. The sentence-transformers embedding model is loaded
   (`contract._get_embedding_model(settings.EMBEDDING_MODEL)`).
3. The offline artifacts are loaded and valid:
   - `tree.load_field_tree(settings)` — field tree.
   - `metadata.load_field_metadata(settings)` — compiled sidecar.
   - Topics resolved (via the same metadata load path).

All of these are warmed at startup (§5); the readiness probe only checks a
boolean flag set by the lifespan. **If artifacts are missing/invalid, the app
fails to start** (see §5.2) — so `/ready` returning 503 is not the expected
path; it is a defensive fallback for a race between startup and the first probe.

---

## 5. App factory (`src/bulk_insert/api/app.py`)

### 5.1 FastAPI factory

```python
def create_app(cfg: Settings | None = None) -> FastAPI:
    """Build the FastAPI application with lifespan warm-up."""
```

Uses `@asynccontextmanager` for the FastAPI `lifespan` parameter.

### 5.2 Lifespan warm-up

The lifespan `startup` phase warms all heavy resources **before** the app
accepts traffic:

1. **spaCy pipeline** — `preprocessing._get_nlp()` (already cached via
   `@lru_cache`).
2. **Embedding model** —
   `contract._get_embedding_model(settings.EMBEDDING_MODEL)` (already cached
   via `@lru_cache`).
3. **Offline artifacts** — `tree.load_field_tree(settings)` and
   `metadata.load_field_metadata(settings)` (both cached). If either raises
   (file not found, malformed YAML, etc.), the startup **fails with a clear
   error** — the process exits, preventing traffic from reaching a
   misconfigured container.

On failure, the lifespan logs the exception at `CRITICAL` level and re-raises,
causing uvicorn to exit with a non-zero status.

A module-level `_ready: bool = False` flag is set to `True` after all warm-up
steps complete. `/ready` checks this flag.

### 5.3 Settings injection

The factory accepts an optional `Settings` override (for tests). When not
provided, it uses the process-level singleton `config.settings`. The `Settings`
instance is stored in `app.state.settings` so routes can access it without
importing the module-level singleton (testable, overridable).

---

## 6. Concurrency model

### 6.1 Single worker

One uvicorn worker (`--workers 1`). Since `process_document` is synchronous and
CPU+network-bound (LLM calls), a single worker processes one request at a time.
Concurrency is achieved by running more container replicas behind a load
balancer, per the stateless-synchronous-container philosophy.

### 6.2 No explicit threadpool cap

With a single uvicorn worker and a synchronous `def` route, FastAPI runs the
handler in the default threadpool. Since each request involves heavy LLM
calls, limiting to one concurrent request per container is the right default.
No explicit `max_workers` cap is added — the single-worker deployment already
enforces it. If future profiling shows a need, it can be added via
`anyio.CapacityLimiter` or a uvicorn worker count.

---

## 7. Dependency cleanup

### 7.1 `pyproject.toml` — remove dead dependencies

The following dependencies are dead code from the pre-Phase-5 architecture and
should be removed:

- `celery>=5.4.0`
- `redis>=5.0.0`
- `sqlalchemy[asyncio]>=2.0.0`
- `asyncpg>=0.29.0`
- `alembic>=1.13.0`

This was originally slated for Phase 8 (containerization), but since these are
unused and confusing, Phase 7 removes them proactively.

After removal, run `uv lock` / `uv sync` to regenerate the lock file.

### 7.2 `config.py` — remove dead settings

Remove from `Settings`:

- `REDIS_URL`
- `DATABASE_URL`

These are vestiges of the original Celery/Postgres architecture and are not
referenced anywhere in the codebase.

---

## 8. Module layout

```
src/bulk_insert/
  api/
    __init__.py       # empty; marks the package
    app.py            # FastAPI factory + lifespan warm-up (§5)
    routes.py         # POST /process, GET /health, GET /ready (§4)
    schemas.py        # ProcessRequest, ProcessResponse, ErrorResponse (§3)
  models/
    asset.py          # +completeness field (§2.1)
    pipeline.py       # unchanged
  blocks/
    extraction/
      ground.py       # +completeness on GroundResult (§2.1)
      orchestrator.py # wire completeness (§2.1)
  config.py           # -REDIS_URL, -DATABASE_URL (§7.2)
  pipeline.py         # unchanged

tests/unit/
  test_api.py         # route tests with TestClient (§9)
  test_extraction.py  # +completeness test (§2.1, appended to existing)
```

---

## 9. Test plan

All tests use `fastapi.testclient.TestClient` (backed by `httpx`) with
`process_document` mocked — no live LLM calls.

### 9.1 Success path

- **T1 — happy path.** Mock `process_document` to return a successful
  `PipelineResult` with a full `Asset` (including `evidence`, `flags`,
  `review_queue`, `completeness`). Assert:
  - Status 200.
  - Response body has `id`, `asset`, `confidence`, `completeness`, `_models`.
  - `asset.review_queue` is present and serialized nested under `asset`.
  - `completeness` is present both nested (`asset.completeness`) and lifted to
    the top level.
  - `_models` is present and has `lightweight`, `heavy`, `embedding` keys.
  - `confidence` matches `asset.confidence`.

- **T2 — non-empty `review_queue` is still 200.** Mock a result with
  `review_queue=["campo_1", "campo_2"]`. Assert status 200, not 4xx.

- **T3 — completeness present and lifted.** Mock a result with
  `asset.completeness=0.667`. Assert `response.json()["completeness"] == 0.667`
  and `response.json()["asset"]["completeness"] == 0.667`.

### 9.2 Error paths (category → status mapping)

For each `ErrorCategory`:

- **T4 — `bad_input` → 400.** Mock a failure with `category="bad_input"`. Assert
  status 400, body has `id` and `error.category == "bad_input"`.
- **T5 — `unprocessable` → 422.** Assert status 422.
- **T6 — `retryable` → 502.** Assert status 502.
- **T7 — `server` → 500.** Assert status 500.

All error responses assert `id` is echoed.

### 9.3 `id` echo

- **T8 — id is always echoed.** For both success and every error category, assert
  the `id` in the response body matches the `id` in the request.

### 9.4 Health probes

- **T9 — `GET /health`.** Assert 200, body `{"status": "ok"}`.
- **T10 — `GET /ready`.** With the lifespan warm-up completing successfully
  (mocked), assert 200, body `{"status": "ready"}`.

### 9.5 `_models` validation

- **T11 — `_models` matches settings.** Assert the `_models` dict in the success
  response matches the configured `MODEL_LIGHTWEIGHT`, `MODEL_HEAVY`, and
  `EMBEDDING_MODEL`.

### 9.6 Request validation

- **T12 — missing `text` → 422.** POST `{"id": "x"}` (no `text` field). Assert
  FastAPI's built-in 422 response.
- **T13 — empty `text` → 400.** POST `{"id": "x", "text": ""}`. The pipeline's
  own guard catches this (empty text → `bad_input` → 400).
- **T14 — missing `id` → 422.** POST `{"text": "something"}` (no `id` field).
  Assert FastAPI's built-in 422 response.

### 9.7 Completeness on Asset (Block 3 suite)

- **T15 — completeness wired from grounding.** Mock a `ground_and_assemble` with
  3 mandatory fields, 2 filled. Assert `asset.completeness ≈ 0.667`. Existing
  extraction tests stay green (new field is optional/defaulted).

---

## 10. Configuration (new `Settings` knobs)

No new `Settings` knobs are added for Phase 7. The API layer is configuration-
free — it reads `MODEL_LIGHTWEIGHT`, `MODEL_HEAVY`, and `EMBEDDING_MODEL` for
the `_models` metadata, and forwards `Settings` to `process_document`. The
single-worker deployment is controlled by uvicorn CLI flags, not application
settings.

---

## 11. Resolved decisions

| Decision | Resolution | Rationale |
|---|---|---|
| Concurrency model | Single uvicorn worker | Consistent with stateless-synchronous-container philosophy; scale by replicas |
| Artifact validation at startup | Fail to start | Prevents traffic to a misconfigured container; clearer than 503 on `/ready` |
| `review_queue` HTTP status | 200 (not 4xx) | Successful extraction with incomplete coverage; the queue is data, not an error |
| Dead dependencies | Remove in Phase 7 | Dead code is confusing; no reason to wait for Phase 8 |
| `completeness` lifting | Lifted to top-level response AND nested in `asset` | Parity with `confidence` lifting; callers don't need to descend into `asset` |
| Route handler type | Synchronous `def` (not `async def`) | Pipeline is blocking; sync `def` runs in threadpool automatically |
| `_models` field | Plain dict, underscore prefix | Signals metadata, not extraction output; matches CLI's `--json` output |

---

## 12. Open items

None. All design decisions for Phase 7 are resolved. The implementation is a
straightforward mapping exercise:

1. Add `completeness` to `GroundResult` → `Asset` → `orchestrator.py`.
2. Create `api/` package with `schemas.py`, `routes.py`, `app.py`.
3. Write `test_api.py` + completeness test.
4. Clean `pyproject.toml` and `config.py`.
5. Verify: `ruff check .`, `ruff format --check .`, `mypy --strict src/`,
   `pytest` — all pass at zero errors.

---

## 13. Acceptance criteria

- [ ] `POST /process {id, text}` returns
  `{id, asset, confidence, completeness, _models}` on the same request.
- [ ] `evidence`/`flags`/`review_queue`/`completeness` serialize nested inside
  `asset`.
- [ ] `confidence` and `completeness` are also lifted to the top level.
- [ ] A non-empty `review_queue` returns 200.
- [ ] `completeness` reports the share of mandatory fields filled (0.0–1.0).
- [ ] `_models` matches the configured model slugs.
- [ ] Error responses echo `id` and map `category` → status code
  (400/422/502/500).
- [ ] `/health` returns 200 (liveness only).
- [ ] `/ready` returns 200 only once models **and** offline artifacts are loaded
  and valid.
- [ ] Startup fails with a clear error if artifacts are missing/invalid.
- [ ] Dead dependencies removed from `pyproject.toml`; `REDIS_URL` /
  `DATABASE_URL` removed from `config.py`.
- [ ] `ruff check .`, `ruff format --check .`, `mypy --strict src/`,
  `pytest` — all pass at zero errors.
