# syntax=docker/dockerfile:1

# ── Stage 1: builder ───────────────────────────────────────────────────────────
# Resolve + install dependencies with uv into a self-contained /app/.venv, then
# bake the sentence-transformers embedding model into the HF cache so cold-start
# needs no network (except OpenRouter at request time).
FROM python:3.13-slim AS builder

# Pinned uv (matches the project toolchain). Provides the `uv` binary only.
COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# 1) Dependencies only — cached across source-only changes. On Linux, torch
#    resolves to the CPU wheel (no CUDA/nvidia/triton) via the pytorch-cpu index.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) Application + static assets, then install the project itself.
COPY src ./src
COPY artifacts ./artifacts
COPY contracts ./contracts
RUN uv sync --frozen --no-dev

# 3) Bake the embedding model into the HF cache (no network at runtime).
ENV HF_HOME=/opt/hf
RUN /app/.venv/bin/python -c \
    "from sentence_transformers import SentenceTransformer; \
     SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')"

# 4) Slim the venv: strip debug symbols from shared libs (biggest single win
#    after CPU torch) and drop bytecode caches. binutils is installed only to
#    run `strip` and purged in the same layer so it never reaches the runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends binutils \
    && find /app/.venv -name '*.so*' -type f -exec strip --strip-unneeded {} + 2>/dev/null || true \
    && find /app/.venv -type d -name '__pycache__' -prune -exec rm -rf {} + \
    && apt-get purge -y binutils \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2: runtime ───────────────────────────────────────────────────────────
# Minimal image: the venv, the app, the static assets, and the baked model.
FROM python:3.13-slim AS runtime

# Non-root runtime user.
RUN groupadd --system app && useradd --system --gid app --home-dir /app app

WORKDIR /app

# Copy the built venv, app source, static assets, and baked model cache.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=builder --chown=app:app /app/artifacts /app/artifacts
COPY --from=builder --chown=app:app /app/contracts /app/contracts
COPY --from=builder --chown=app:app /opt/hf /opt/hf

ENV PATH="/app/.venv/bin:${PATH}" \
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    UVICORN_LIMIT_CONCURRENCY=8 \
    UVICORN_GRACEFUL_TIMEOUT=30 \
    RUNTIME_MODEL_OVERRIDE_ENABLED=true \
    LOG_JSON=true \
    LOG_LEVEL=INFO
# API_KEY is intentionally not defaulted — inject it at runtime to require the
# X-API-Key header on /process and /config/* (unset ⇒ auth disabled).

USER app

EXPOSE 8000

# Liveness: the /health probe is always 200 once the process is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import sys,urllib.request; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Single process, single uvicorn worker; in-flight requests capped via
# --limit-concurrency. --timeout-graceful-shutdown lets in-flight extractions
# drain on SIGTERM (rolling deploys). Scale throughput by adding replicas.
CMD ["sh", "-c", "exec uvicorn 'bulk_insert.api.app:create_app' --factory --host 0.0.0.0 --port \"${PORT}\" --workers 1 --limit-concurrency \"${UVICORN_LIMIT_CONCURRENCY}\" --timeout-graceful-shutdown \"${UVICORN_GRACEFUL_TIMEOUT}\""]
