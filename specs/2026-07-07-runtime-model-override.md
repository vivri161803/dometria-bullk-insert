# Runtime Model Override (API config endpoints)

> Status: in progress · Extends Phase 7 (FastAPI service) + Phase 8 (container).
> Goal: let an operator swap the **heavy** and **lightweight** LLM slugs on a
> **running** container — without a redeploy — via HTTP, one request per model.

## 1. Motivation

`MODEL_HEAVY` / `MODEL_LIGHTWEIGHT` are already overridable at container start
(env → `Settings`). This adds the ability to change them **after** deployment,
so an operator can A/B a model or react to an upstream outage without rebuilding
or restarting the image. The caller sends the bare OpenRouter slug (e.g.
`anthropic/claude-haiku-4.5`) for one role per request.

## 2. Feasibility & the statelessness caveat (important)

Feasible, with a caveat that must be stated because it cuts against the project's
core design (`specs/mission.md`: "Stateless by design"):

- The route reads `request.app.state.settings` on **every** call, and the extraction
  blocks build their Instructor client fresh per call from `cfg.MODEL_*` (no cache is
  keyed on the model slug — the `lru_cache`s cover only spaCy / embedding / tree /
  metadata). So atomically replacing `app.state.settings` with a `model_copy(update=…)`
  changes the model used by all **subsequent** `/process` calls on that process.
- **The override is per-replica and ephemeral.** It affects only the one container that
  received the POST and is lost on restart. It is **not** shared across replicas. With
  N replicas behind a load balancer, a cluster-wide change means POSTing every replica,
  or running a single replica, or (the durable path) setting the env var and
  redeploying. This is called out in the response semantics and the docs; the feature
  is a per-container operator lever, not shared cluster state.
- In-flight requests are unaffected: each `/process` call already captured its `cfg`
  reference before the swap (atomic pointer reassignment under the GIL).
- The container runs a **single uvicorn worker**, so one process (one `app.state`)
  serves the whole container — the override is container-wide, not per-worker. (With
  multiple workers each process would hold its own `app.state` and the POST would hit
  only one; we deliberately run `--workers 1`.)

## 3. Decisions

- **One endpoint, one role per request** (matches the request: "two different requests,
  one per model"): `POST /config/model` with `{ role, model }`, plus a read-only
  `GET /config/models` for verification.
- **Only the two LLM slugs are mutable.** The embedding model is **excluded** — it is a
  sentence-transformer baked into the image offline, would need a reload + re-cache, and
  is not an OpenRouter slug.
- **Gated behind a flag** — `RUNTIME_MODEL_OVERRIDE_ENABLED` (default `True`). When
  `False`, `POST /config/model` returns `403` (`GET /config/models` still works). This
  lets a hardened deployment lock the models to the baked/env values.
- **No auth yet** — consistent with the other endpoints; API-key auth is Phase 9. The
  docs flag this as an admin endpoint to protect then.
- **Validation** — `role` is a `Literal["lightweight","heavy"]` (invalid → `422`);
  `model` is whitespace-stripped with `min_length=1` (empty/blank → `422`). No live
  OpenRouter reachability check (a bad slug surfaces as a normal `retryable`/`server`
  error on the next `/process`).

## 4. Implementation

- **`config.py`** — add `RUNTIME_MODEL_OVERRIDE_ENABLED: bool = True`.
- **`api/schemas.py`** —
  - `SetModelRequest{ role: Literal["lightweight","heavy"], model: Annotated[str, strip+min_length=1] }`.
  - `ModelConfigResponse{ lightweight: str, heavy: str, embedding: str }` (returned by both
    endpoints; reflects the post-swap state).
- **`api/routes.py`** —
  - `GET /config/models` → current `ModelConfigResponse` from `app.state.settings`.
  - `POST /config/model` → if disabled, `403`; else `app.state.settings =
    cfg.model_copy(update={"MODEL_HEAVY"|"MODEL_LIGHTWEIGHT": model})`; return the new map.
- **`Dockerfile`** — surface `RUNTIME_MODEL_OVERRIDE_ENABLED` in the runtime `ENV` block
  (documented default) so it is discoverable; runtime overrides via `-e` as usual.

## 5. Tests (`tests/unit/test_api.py`)

- `GET /config/models` returns the current three slugs.
- `POST /config/model {role:"heavy"}` swaps heavy, echoes new map, leaves lightweight/embedding.
- `POST /config/model {role:"lightweight"}` swaps lightweight.
- After a swap, `POST /process` reports the new slug under `_models` (patched
  `process_document`, asserting `request.app.state.settings` was updated / reading the
  response `_models`).
- Invalid `role` → `422`; empty/whitespace `model` → `422`.
- `RUNTIME_MODEL_OVERRIDE_ENABLED=False` → `POST /config/model` → `403`, `GET` still `200`.

## 6. Exit criteria

On a running container, `POST /config/model` with `{role, model}` (called once per role)
changes the heavy/lightweight slug used by subsequent `/process` calls, verified via
`GET /config/models` and the `_models` block; the change is per-replica/ephemeral (documented);
the endpoint is flag-gated; `ruff`/`mypy`/`pytest` stay green.
