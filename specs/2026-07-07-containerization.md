# Phase 8 — Containerization

> Status: in progress · Roadmap: `specs/roadmap.md` Phase 8 · Depends on: Phase 7 (FastAPI service).
> Goal: ship the service as **one self-contained, stateless image** — `docker run` starts it,
> a real document POSTed via `curl` returns a valid compiled asset.

## 1. Scope

Roadmap Phase 8 checklist and how each item is addressed here:

| Roadmap item | Status in this phase |
|---|---|
| `Dockerfile` — multi-stage (`uv` deps → app), single image, single process (uvicorn) | **This phase** |
| Bake models + artifacts (embedding model at build via `HF_HOME`; bundle `contracts/` + `artifacts/`) | **This phase** |
| Runtime config — `OPENROUTER_API_KEY` + overrides via env/secrets, never baked | **This phase** |
| Trim `pyproject.toml` (remove `celery`/`redis`/`sqlalchemy`/`asyncpg`/`alembic`) + refresh `uv.lock` | **Already landed** — `pyproject.toml` and `config.py` carry none of these deps/fields today. This phase only re-verifies `uv.lock` is in sync. |
| `README.md` — build/run, API contract, deployment handoff | **This phase** |
| Smoke test — `docker run` + `POST /process` | **This phase** |

### Pre-existing state (verified before writing code)

- `pyproject.toml` dependencies are already limited to `fastapi`, `uvicorn`, `pydantic`,
  `pydantic-settings`, `openai`, `instructor`, `spacy`, `sentence-transformers`, `pyyaml`,
  `types-pyyaml`. **No** `celery`/`redis`/`sqlalchemy`/`asyncpg`/`alembic`.
- `config.py` has **no** `REDIS_URL`/`DATABASE_URL` fields (the CLAUDE.md note that they are
  "still vestigial in config.py" is stale — they were already removed). The only stragglers are
  two lines in the `.env` example file, cleaned up as housekeeping here.
- `uv.lock` has no top-level entries for the removed packages.

## 2. Decisions (questions asked)

1. **Base image — `python:3.13-slim` + `uv` (chosen).** Multi-stage: `uv` builds a `.venv`
   in a `python:3.13-slim` builder; runtime is a fresh `python:3.13-slim` with only the venv,
   app source, baked model, and static assets copied in. Rationale: `torch` /
   `sentence-transformers` ship glibc wheels — Alpine (musl) forces slow source builds and
   distroless complicates model-baking, so slim/Debian is the safe, small-enough default.
   `uv` is pulled from the official `ghcr.io/astral-sh/uv` image (pinned) into the builder.

2. **Smoke test — run in-session (chosen).** The Docker daemon is available, so the build and
   `POST /process` smoke test are executed as part of delivery (not just documented). A live
   `POST /process` needs a real `OPENROUTER_API_KEY` (passed via `-e`); `/health` and `/ready`
   do not.

3. **Housekeeping cleanups — all three (chosen).**
   - Remove the vestigial `REDIS_URL` / `DATABASE_URL` lines from the `.env` example.
   - Re-run `uv lock` to confirm `uv.lock` matches the already-trimmed `pyproject.toml`.
   - Update `specs/roadmap.md` (mark Phase 8) and `CLAUDE.md` (drop the stale "config.py still
     carries REDIS_URL/DATABASE_URL" note; document the Dockerfile).

### Additional Dockerfile decisions (sensible defaults, not blocking)

- **Single process, single uvicorn worker.** Matches the mission's synchronous-container model;
  concurrency scales by replicas, not workers.
- **In-flight concurrency cap via `--limit-concurrency`.** Satisfies the Phase 7
  "cap per-container in-flight requests" intent at the process boundary; tunable via the
  `UVICORN_LIMIT_CONCURRENCY` env var (default `8`). One replica therefore never runs an
  unbounded number of heavy extractions at once.
- **Non-root runtime user** (`app`) for least privilege.
- **Port `8000`**, overridable via `PORT`. `EXPOSE 8000`.
- **`HEALTHCHECK`** hits `GET /health` (liveness) so the runtime can restart a wedged process.
- **Offline model at runtime** — `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` so the baked
  embedding model is never re-fetched at cold-start (no network dependency except OpenRouter).

## 3. Implementation plan

### 3.1 `Dockerfile` (multi-stage)

```
Stage `builder`  (python:3.13-slim)
  - Bring in the pinned `uv` binary from ghcr.io/astral-sh/uv.
  - UV_COMPILE_BYTECODE=1, UV_LINK_MODE=copy, UV_PYTHON_DOWNLOADS=0 (use the base python).
  - WORKDIR /app.
  - COPY pyproject.toml uv.lock → `uv sync --frozen --no-dev --no-install-project`
    (deps only; cached across source changes).
  - COPY src, artifacts, contracts → `uv sync --frozen --no-dev` (installs the project).
  - HF_HOME=/opt/hf; bake the embedding model:
    `.venv/bin/python -c "from sentence_transformers import SentenceTransformer;
     SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')"`.

Stage `runtime`  (python:3.13-slim)
  - Create non-root user `app`.
  - WORKDIR /app.
  - COPY --from=builder (with --chown) the venv (/app/.venv), src, artifacts, contracts,
    and the baked HF cache (/opt/hf).
  - ENV: PATH=/app/.venv/bin:$PATH, HF_HOME=/opt/hf, HF_HUB_OFFLINE=1,
    TRANSFORMERS_OFFLINE=1, PYTHONUNBUFFERED=1, PYTHONDONTWRITEBYTECODE=1,
    PORT=8000, UVICORN_LIMIT_CONCURRENCY=8.
  - USER app; EXPOSE 8000.
  - HEALTHCHECK → GET /health.
  - CMD: uvicorn "bulk_insert.api.app:create_app" --factory --host 0.0.0.0
    --port $PORT --workers 1 --limit-concurrency $UVICORN_LIMIT_CONCURRENCY.
```

**Why `contracts/` + `artifacts/` land at `/app` next to `/app/src`:** `config.py` derives
`CONTRACTS_DIR` / `ARTIFACTS_DIR` from `Path(__file__).resolve().parents[2]`, i.e. the repo root
two levels above `src/bulk_insert/`. With an editable install at `/app/src/bulk_insert/…`, that
root resolves to `/app`, so the static assets must sit at `/app/contracts` and `/app/artifacts`.

### 3.2 `.dockerignore`

Exclude everything not needed in the build context: `.venv`, `.git`, `tests/`, caches
(`.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `__pycache__`), `*.log`, `.env`, `specs/`,
`scripts/`, `sample.txt`, IDE/OS files. The builder only `COPY`s specific paths, so this is a
lean-context safety net.

### 3.3 `README.md`

- What the service is (one-paragraph mission summary; link to `specs/mission.md`).
- Local dev quickstart (`uv sync`, run tests, run uvicorn with `--factory`).
- Docker: build, run (with `OPENROUTER_API_KEY`), curl smoke test, health/ready.
- The API contract table (`POST /process`, `GET /health`, `GET /ready`) + request/response
  shapes and the status-code mapping (sourced from `specs/mission.md`).
- Deployment handoff: stateless, scale by replicas, long-request timeouts, gate traffic on
  `/ready`, load balancer performs whole-document retry on `502`.

### 3.4 Housekeeping

- `.env`: drop `REDIS_URL` / `DATABASE_URL` lines.
- `uv lock` (verify in sync; commit if it changes).
- `specs/roadmap.md`: mark Phase 8 ✅ once the smoke test passes.
- `CLAUDE.md`: remove the stale "config.py still carries REDIS_URL/DATABASE_URL" sentence;
  add the `Dockerfile` / build+run commands and mark Phase 8 done in "Status & not-yet-built".

## 4. Testing / gates

- **Smoke test (in-session):** `docker build -t bulk-insert:local .`; `docker run` with the
  `OPENROUTER_API_KEY` env; poll `/ready` until `200`; `POST /process` with `sample.txt`;
  assert `200` + an `asset` with a `contract_id` and a `data` record; also confirm the
  `bad_input` (`400`) and request-validation (`422`) paths.
- **Quality gates:** `ruff check .`, `ruff format --check`, `mypy src`, `pytest tests/unit`
  all green. No application code changes are expected in this phase, so the existing unit
  suite is the regression guard; the Dockerfile itself is exercised by the smoke test.

## 5. Exit criteria

`docker run` starts the service; `/health` is `200` immediately, `/ready` turns `200` after
warm-up (spaCy + baked embedding model + artifacts), and a real document submitted via `curl`
to `POST /process` returns a valid compiled asset. `pyproject.toml`/`uv.lock` carry none of the
async-stack dependencies. `README.md` documents build, run, the API contract, and the
deployment handoff.
