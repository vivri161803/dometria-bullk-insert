# Hardening & Observability (Phase 9)

> Status: complete · Extends Phase 7 (FastAPI service) + Phase 8 (container).
> Goal: make the service production-ready for the internal rollout — structured
> logs, graceful shutdown, rate-limit backoff, inbound auth + payload guards, and
> operator docs. No pipeline logic changes; this is a thin ring around the
> existing stateless service.

## 1. Motivation

Blocks 1–3, the orchestrator, the FastAPI service, and the container are done.
What's missing before an internal rollout is the operational ring: machine-parseable
logs to trace a request, backoff so a rate-limit spike doesn't fail requests
outright, an auth gate + size guard on the public endpoint, clean draining on
redeploy, and a runbook. Each piece is independently testable and toggled by its
own setting.

## 2. Decisions

- **Structured logging (`logging_config.py`).** One `configure_logging(cfg)` call
  from the app factory installs a single stdout handler. `LOG_JSON=true` →
  `python-json-logger` JSON (one object/line); `false` → human formatter for dev.
  `id` and `block` ride on **context variables** (a `logging.Filter` stamps them
  onto every record) so no call signature changes; `duration_ms` is passed via
  `extra=` on lifecycle lines. Uvicorn's loggers are routed through the same
  handler; its access logger is silenced in favour of our richer middleware line.
- **Correlation id + access log + size guard (`api/middleware.py`).** One
  `BaseHTTPMiddleware`: reads/generates `X-Request-ID`, binds it as the log `id`
  **before** `call_next` (so the copied downstream context inherits it), rejects
  bodies over `MAX_REQUEST_BYTES` with `413` (from `Content-Length`, before
  parsing), echoes `X-Request-ID` on the response, and logs one structured access
  line with method/path/status/`duration_ms`. The `/process` route overrides the
  log `id` with the document id so pipeline lines correlate to the caller's id.
- **Rate-limit backoff — SDK-level, not app-level.** The cleanest lever: pass
  `max_retries`/`timeout` into `instructor.from_provider` (all three block
  builders), which configures the underlying OpenAI client to retry `429`/transient
  upstream errors with exponential backoff **honouring OpenRouter's `Retry-After`**.
  No wrapper around the ~8 call sites; every LLM call inherits it. Exhausted
  backoff still surfaces as the existing `retryable`/`502`.
- **Auth (`api/security.py`).** A `require_api_key` dependency on `/process` and
  `/config/*` (not the probes). When `API_KEY` is set, a constant-time compare
  against the `API_KEY_HEADER` value; missing/wrong ⇒ `401`. Empty key ⇒ auth
  disabled (dev default). Header name is configurable, so a plain dependency (not
  `APIKeyHeader`) reads it off `app.state.settings`.
- **Payload shape.** `ProcessRequest.text` gains a static `max_length`
  (`MAX_TEXT_CHARS`, shape → `422`); `id` gains `min_length=1`. The dynamic byte
  guard (`MAX_REQUEST_BYTES` → `413`) is the DoS lever; the char cap bounds the
  parsed field.
- **Graceful shutdown.** The lifespan withdraws readiness after `yield`
  (`app.state.ready = False` ⇒ `/ready` → `503`, LB stops routing) and logs the
  drain; uvicorn's `--timeout-graceful-shutdown` (`UVICORN_GRACEFUL_TIMEOUT`, wired
  in the `Dockerfile` CMD) finishes in-flight requests. No persistent resources to
  release (stateless).
- **OpenAPI + docs.** App gains `version`, `description`, and `openapi_tags`
  (`extraction`/`config`/`probes`); routes are tagged. `docs/RUNBOOK.md` documents
  status-code → cause → action, common scenarios, and the env reference; the
  README gains a Hardening section.

## 3. Config additions (`config.py`)

`LOG_LEVEL="INFO"`, `LOG_JSON=True`; `LLM_MAX_RETRIES=5`, `LLM_TIMEOUT=60.0`;
`API_KEY=""`, `API_KEY_HEADER="X-API-Key"`, `MAX_REQUEST_BYTES=2_000_000`.
`MAX_TEXT_CHARS` lives as a static constant in `api/schemas.py` (it's a
class-level field constraint, not runtime-tunable).

## 4. Files

- New: `logging_config.py`, `api/middleware.py`, `api/security.py`,
  `docs/RUNBOOK.md`, this spec.
- Changed: `config.py` (+7 knobs), `api/app.py` (logging + middleware + OpenAPI +
  shutdown), `api/routes.py` (auth deps, tags, doc-id log binding),
  `api/schemas.py` (text cap, id min_length), `pipeline.py` (block/duration log
  context), the three Instructor builders (`preprocessing.py`, `contract.py`,
  `extraction/client.py`), `pyproject.toml` (`python-json-logger`), `Dockerfile`
  (graceful-timeout flag + Phase 9 env defaults), `README.md`.

## 5. Tests

- `test_logging.py` — JSON carries `id`/`block`/`duration_ms`; defaults to `-`;
  plain format; idempotent config; level respected.
- `test_llm_backoff.py` — all three builders forward `max_retries`/`timeout` to
  the underlying client.
- `test_api.py` additions — auth (`401` no/wrong key, `200` with key, `/config/*`
  guarded, probes open, open when unset), `413` oversized body, `X-Request-ID`
  echo, `text` over-cap `422`. Existing 19 route tests stay green.

## 6. Non-goals

- Distributed/shared rate limiting or a token-bucket across replicas (per-replica
  SDK backoff is enough for the internal rollout).
- OAuth / rotating keys / per-caller quotas (single shared `API_KEY`).
- Log shipping/aggregation infra (stdout JSON is the contract; the platform scrapes it).

## 7. Exit criteria

Structured JSON logs on stdout tagged with `id`/`block`/`duration_ms`; `SIGTERM`
drains in-flight requests; `429`s are absorbed by backoff and only surface as
`502` when exhausted; `/process` + `/config/*` gated by `API_KEY` with oversized
bodies rejected `413`; OpenAPI reviewed/tagged and a runbook shipped. Gates green
(ruff, mypy, pytest).
