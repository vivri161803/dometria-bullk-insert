# Runbook — Dometria Bulk Insert service

Internal operations guide for the stateless extraction service. The service is a
single container running one uvicorn worker; it holds no state, so recovery is
almost always "restart / add replicas". Scale throughput by running more
replicas behind a load balancer.

- **Contract:** `POST /process { id, text }` → `{ id, asset, confidence, completeness, _models }`.
- **Probes:** `GET /health` (liveness, always `200`), `GET /ready` (readiness).
- **Config:** `GET /config/models`, `POST /config/model { role, model }`.
- Architecture: `CLAUDE.md`, `specs/mission.md`. Hardening spec: `specs/2026-07-07-hardening.md`.

## Observability

Logs are **JSON, one object per line, on stdout** (`LOG_JSON=true`). Every line
carries:

- `id` — the document id on pipeline lines, or the request-correlation id
  (`X-Request-ID` header, else generated) on the access line.
- `block` — `preprocess` / `contract` / `extract` while the pipeline runs.
- `duration_ms` — on the access line and the pipeline-complete line.

Every response echoes `X-Request-ID`. To trace one request, filter logs by its
`id`. Set `LOG_JSON=false` for human-readable local logs; raise/lower verbosity
with `LOG_LEVEL`.

## Status codes → cause → action

| Status | Meaning | Likely cause | Action |
|--------|---------|--------------|--------|
| `200` | Success | — (may include a non-empty `asset.review_queue` or low `confidence`; both are successful extractions) | None. If `completeness` is low or `review_queue` non-empty, the doc lacked mandatory fields — a data issue, not a service fault. |
| `400` | `bad_input` | Empty/blank `text` | Caller bug — resubmit with real text. |
| `401` | Unauthorized | Missing/wrong `X-API-Key` (only when `API_KEY` is set) | Check the caller's key matches the deployed `API_KEY`. |
| `413` | Payload too large | Body exceeds `MAX_REQUEST_BYTES` | Caller sent an oversized document. Raise `MAX_REQUEST_BYTES` if legitimately large, or have the caller trim. |
| `422` | `unprocessable` / validation | No chunk cleared `CONTRACT_MIN_SCORE`, or malformed request body / over-length `text` | Usually an off-topic or empty-ish document. Inspect the payload. |
| `502` | `retryable` | Transient OpenRouter/network failure (429/5xx/timeout) after backoff exhausted | Retry the request. If sustained, check OpenRouter status and the rate-limit section below. |
| `500` | `server` | Missing/malformed contract schema or artifact, or an unexpected bug | Check logs for the traceback and `block`. Verify `artifacts/` + `contracts/` are present. Page on sustained `500`s. |
| `503` | Not ready | Warm-up not finished, or shutting down | Transient at startup (~60–90 s). If persistent, warm-up failed — check startup logs (`Startup warm-up failed`); the artifacts/model likely didn't load. |

## Common scenarios

**Rate limits (429 → 502).** Each LLM call already retries with exponential
backoff honouring OpenRouter's `Retry-After` (`LLM_MAX_RETRIES`, default 5). A
`502` means backoff was exhausted. If it's widespread: reduce concurrency
(`UVICORN_LIMIT_CONCURRENCY`), run fewer replicas against the same key, raise
`LLM_MAX_RETRIES`, or switch to a less-throttled model via `POST /config/model`
(ephemeral, per replica) or the `MODEL_HEAVY`/`MODEL_LIGHTWEIGHT` env + redeploy.

**Slow / hung extractions.** Each LLM call is bounded by `LLM_TIMEOUT` (default
60 s). A whole document can still take a while (multiple calls). Ensure the
caller's HTTP timeout and any LB idle timeout exceed the worst-case document
time. `--limit-concurrency` caps concurrent in-flight requests per replica.

**Readiness flaps / never ready.** `/ready` is `503` until spaCy, the embedding
model, the field tree, and the compiled metadata sidecar all load. A persistent
`503` with a `Startup warm-up failed` critical log means a missing/uncompiled
artifact — rebuild the image (artifacts are baked) or run
`scripts/compile_metadata.py --write`.

**Rolling deploy / graceful shutdown.** On `SIGTERM`, readiness is withdrawn
(`/ready` → `503` so the LB stops routing) and uvicorn drains in-flight requests
up to `UVICORN_GRACEFUL_TIMEOUT` (default 30 s) before exiting. Set the
orchestrator's termination grace period ≥ that value.

## Configuration reference (env)

| Var | Default | Purpose |
|-----|---------|---------|
| `OPENROUTER_API_KEY` | — | Required. OpenRouter credential. |
| `API_KEY` | *(empty)* | When set, requires `X-API-Key` on `/process` + `/config/*`. |
| `API_KEY_HEADER` | `X-API-Key` | Header name for the key. |
| `MAX_REQUEST_BYTES` | `2000000` | Reject larger bodies with `413`. |
| `LOG_JSON` / `LOG_LEVEL` | `true` / `INFO` | Structured logging. |
| `LLM_MAX_RETRIES` / `LLM_TIMEOUT` | `5` / `60` | Per-call backoff + timeout. |
| `UVICORN_LIMIT_CONCURRENCY` | `8` | Max in-flight requests per replica. |
| `UVICORN_GRACEFUL_TIMEOUT` | `30` | Drain window on shutdown (seconds). |
| `RUNTIME_MODEL_OVERRIDE_ENABLED` | `true` | Allow `POST /config/model`. |
| `MODEL_HEAVY` / `MODEL_LIGHTWEIGHT` | see `config.py` | OpenRouter slugs. |

The interactive OpenAPI docs are served at `/docs` (Swagger) and `/redoc`; the
raw schema at `/openapi.json`.
