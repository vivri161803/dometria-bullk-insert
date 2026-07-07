# Dometria Bulk Insert

A **stateless, synchronous** extraction service that turns a raw document (received from
*Dometria Connect*) into a schema-compliant JSON asset. It receives `{ id, text }`, runs the
full three-block pipeline in-process, and returns the compiled contract JSON tagged with the
same `id`. One container handles one document per request; throughput scales **horizontally**
by running more replicas.

- **Internal only** — Dometria Connect calls it programmatically over HTTP; there is no UI.
- **Stateless** — nothing is persisted; the `id` is the only correlation handle, echoed on
  every response. Storing the result is the caller's responsibility.
- Full rationale and architecture: [`specs/mission.md`](specs/mission.md),
  [`specs/tech-stack.md`](specs/tech-stack.md), and `CLAUDE.md`.

## Pipeline at a glance

`process_document(id, text)` chains three blocks (see `src/bulk_insert/`):

1. **Block 1 — Preprocessing** (`blocks/preprocessing.py`): spaCy sentence segmentation →
   sliding-window chunking → one lightweight-LLM call per chunk (relevance score + coreference
   resolution).
2. **Block 2 — Contract Determination** (`blocks/contract.py`): lightweight-LLM `lv1`
   classification → embedding cosine-similarity + weighted majority vote picks the `(lv2, lv3)`
   category leaf.
3. **Block 3 — Asset Extraction** (`blocks/extraction/`): tree-driven, evidence-grounded
   extraction with a per-product Pydantic schema, self-consistency voting on high-value groups,
   deterministic normalization, and an LLM-as-judge grounding gate.

Three models, all configured in `config.py` and overridable via env: a **lightweight LLM**
(`MODEL_LIGHTWEIGHT`, default `openai/gpt-4o-mini`) and **heavy LLM** (`MODEL_HEAVY`, default
`openai/gpt-4o`) served through **OpenRouter**, plus a **sentence-transformers embedding model**
(`EMBEDDING_MODEL`, default `paraphrase-multilingual-MiniLM-L12-v2`) baked into the image.

## Local development

There is no `python` on PATH — always go through `uv`.

```bash
uv sync                                    # install deps + create the venv
uv run pytest tests/unit                   # unit tests (what pre-commit runs)
uv run ruff check . && uv run ruff format  # lint + format
uv run mypy src                            # strict type check

# Run a document through the full pipeline from the terminal (live LLM):
uv run bulk-insert-cli --file sample.txt

# Serve the FastAPI app locally (factory + lifespan warm-up):
uv run uvicorn "bulk_insert.api.app:create_app" --factory --port 8000
# interactive OpenAPI docs: http://localhost:8000/docs
```

A live `POST /process` needs a valid OpenRouter key:

```bash
echo "OPENROUTER_API_KEY=sk-or-..." >> .env   # or export it
```

## Docker

The service ships as **one self-contained image**: a multi-stage build (`uv` deps → app) with
the embedding model, `contracts/`, and the offline-built `artifacts/` baked in. The only
required runtime input is the OpenRouter API key.

### Build

```bash
# Build for your deployment target. Pin --platform for reproducibility.
docker build --platform linux/amd64 -t bulk-insert:local .
```

The build installs dependencies with `uv` and downloads the embedding model into the image's
Hugging Face cache (`HF_HOME`), so cold-start has no network dependency except OpenRouter.

**Image size (~2.5 GB).** `torch` is pinned to the **CPU-only** PyTorch wheel index on Linux
(`pyproject.toml` → `[tool.uv.sources]`), which drops the ~3.5 GB of CUDA/nvidia + triton GPU
libraries the default wheel pulls — this service only does CPU sentence-transformer inference.
The build further strips debug symbols from shared libraries and skips bytecode precompilation.
(Without the CPU pin, an `amd64` image is ~9.7 GB.) macOS/Windows local dev is unaffected — the
CPU source is scoped to `sys_platform == 'linux'`; elsewhere the default PyPI wheel (already
CPU/MPS) is used.

### Run

```bash
docker run --rm -p 8000:8000 \
  -e OPENROUTER_API_KEY=sk-or-... \
  bulk-insert:local
```

Runtime configuration is injected via environment (never baked into the image):

| Env var | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | — (**required** for `/process`) | Auth for the LLM calls. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter endpoint. |
| `MODEL_LIGHTWEIGHT` / `MODEL_HEAVY` | `openai/gpt-4o-mini` / `openai/gpt-4o` | Swap models with no code change (start-time; see runtime override below). |
| `PORT` | `8000` | Listen port. |
| `UVICORN_LIMIT_CONCURRENCY` | `8` | Max in-flight requests per replica (excess → `503`). |
| `RUNTIME_MODEL_OVERRIDE_ENABLED` | `true` | Allow `POST /config/model` to swap LLM slugs on the running container; `false` pins them. |

All other tunables have sane defaults in `config.py` and are overridable the same way.

### Smoke test

```bash
# Liveness — up immediately.
curl -s http://localhost:8000/health
# {"status":"ok"}

# Readiness — 503 until warm (spaCy + embedding model + artifacts), then 200. Gate traffic on this.
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/ready

# Happy path from a file.
curl -s -w '\nHTTP %{http_code}\n' -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' \
  --data "$(jq -Rs '{id:"doc-123", text:.}' < sample.txt)"

# Inline text.
curl -s -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' \
  -d '{"id":"doc-1","text":"Trattoria da Mario, 48 coperti, aperta nel 2019."}'
```

## API contract

| Method & Path | Purpose | Request | Success |
|---|---|---|---|
| `POST /process` | Run the pipeline on one document (synchronous, seconds→minutes). | `{ "id": "<doc-id>", "text": "<raw text>" }` | `200` → `{ id, asset, confidence, completeness, _models }` |
| `GET /health` | Liveness — process is up. | — | `200` |
| `GET /ready` | Readiness — models + artifacts warmed. | — | `200` once warm, else `503` |
| `GET /config/models` | Report the LLM/embedding slugs currently in effect. | — | `200` → `{ lightweight, heavy, embedding }` |
| `POST /config/model` | Swap one LLM slug on the running container (one role per request). | `{ "role": "heavy"\|"lightweight", "model": "<slug>" }` | `200` → `{ lightweight, heavy, embedding }` |

The success body carries the **full Block 3 `Asset`** under `asset` (with
`evidence`/`flags`/`review_queue`/`completeness` nested inside), and lifts `confidence` +
`completeness` to the top level. A `200` can still need review: **low confidence and a non-empty
`review_queue` are successful extractions**, surfaced in the body — never a non-2xx.

Errors echo the `id` and a structured `PipelineError`, mapped from its category:

| Category | Status | When |
|---|---|---|
| `bad_input` | `400` | Whitespace-only `text` (pipeline short-circuits). |
| `unprocessable` | `422` | No chunk clears the relevance threshold — document can't be categorized. |
| `retryable` | `502` | Transient upstream OpenAI/network failure — safe to resubmit the same `id`. |
| `server` | `500` | Missing/malformed schema, or any uncaught error. |

FastAPI request-validation failures (empty `text`, missing `id`, malformed JSON) return `422`
with FastAPI's default `{"detail": [...]}` envelope, produced before the handler runs.

Full request/response examples and `curl` recipes: [`specs/mission.md`](specs/mission.md)
("API Guide").

### Changing models at runtime

The heavy and lightweight LLM slugs can be swapped on a **running** container without a
redeploy — send one request per model with the bare OpenRouter slug:

```bash
# Read what's in effect.
curl -s http://localhost:8000/config/models
# {"lightweight":"openai/gpt-4o-mini","heavy":"openai/gpt-4o","embedding":"paraphrase-multilingual-MiniLM-L12-v2"}

# Swap the heavy model, then the lightweight model (two requests).
curl -s -X POST http://localhost:8000/config/model \
  -H 'Content-Type: application/json' -d '{"role":"heavy","model":"anthropic/claude-haiku-4.5"}'
curl -s -X POST http://localhost:8000/config/model \
  -H 'Content-Type: application/json' -d '{"role":"lightweight","model":"mistralai/ministral-3b"}'
```

Subsequent `POST /process` calls use the new slugs (visible under `_models` in the response).

> **Caveat — per-replica and ephemeral.** The override applies only to the container that
> received the POST and is lost on restart; it is **not** shared across replicas. With N
> replicas behind a load balancer, POST every replica (or run a single replica) for a
> cluster-wide change — or set `MODEL_HEAVY`/`MODEL_LIGHTWEIGHT` via env and redeploy for a
> durable one. The embedding model is baked into the image and is **not** runtime-swappable.
> Set `RUNTIME_MODEL_OVERRIDE_ENABLED=false` to disable the endpoint (`POST` → `403`). The
> endpoint is currently unauthenticated — protect it with the Phase 9 API-key auth.

## Deployment handoff

The container is **stateless and horizontally scalable** — the system engineer owns the scaling
topology (Kubernetes Deployment + HPA, ECS service, Cloud Run, …).

- Run as many replicas as needed behind a load balancer / autoscaler; no shared state, no sticky
  sessions, no DB.
- **Gate traffic on `GET /ready`** (green once models + artifacts are warm); use `GET /health`
  for liveness restarts.
- **Requests are long-running** (tens of seconds to a couple of minutes per document). Client,
  ingress, and load-balancer **timeouts must be sized accordingly**.
- The load balancer should perform the **whole-document retry on a `502`** (resubmit the `id`).
- Cap per-replica load with `UVICORN_LIMIT_CONCURRENCY`; add replicas for throughput, not
  workers/threads.
- The only required runtime input is the `OPENROUTER_API_KEY` secret plus outbound network
  access to OpenRouter.
