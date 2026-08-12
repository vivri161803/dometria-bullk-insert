"""FastAPI application factory with lifespan warm-up (Phase 7 + Phase 9)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bulk_insert.api.connection_test import ConnectionTestLoop
from bulk_insert.api.middleware import RequestContextMiddleware
from bulk_insert.api.routes import router
from bulk_insert.blocks import contract as contract_block
from bulk_insert.blocks import preprocessing
from bulk_insert.blocks.extraction import metadata, tree
from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings
from bulk_insert.logging_config import configure_logging

__all__: list[str] = ["create_app"]

logger = logging.getLogger(__name__)

_DESCRIPTION = """\
Stateless, synchronous document-extraction service. Send `{ id, text }` to
`POST /process`; it runs the three-block pipeline in-process and returns the
compiled contract JSON tagged with the same `id`. Scale throughput by running
more replicas — one container processes one document per request.

**Auth:** when `API_KEY` is set, `/process` and `/config/*` require the
`X-API-Key` header. The `/health` and `/ready` probes are always open.
"""

_OPENAPI_TAGS = [
    {"name": "extraction", "description": "Run a document through the pipeline."},
    {"name": "config", "description": "Inspect / override the runtime model slugs."},
    {"name": "probes", "description": "Liveness and readiness probes (unauthenticated)."},
]


def create_app(cfg: Settings | None = None) -> FastAPI:
    """Build the FastAPI application with structured logging + lifespan warm-up."""
    effective_cfg = cfg or _default_settings
    configure_logging(effective_cfg)

    connection_test_loop = ConnectionTestLoop(effective_cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Warm heavy resources before serving; drain cleanly on shutdown."""
        try:
            logger.info("Warming spaCy pipeline …")
            preprocessing._get_nlp()

            logger.info("Warming embedding model '%s' …", effective_cfg.EMBEDDING_MODEL)
            contract_block._get_embedding_model(effective_cfg.EMBEDDING_MODEL)

            logger.info("Loading field tree …")
            tree.load_field_tree(effective_cfg)

            logger.info("Loading field metadata …")
            metadata.load_field_metadata(effective_cfg)

            logger.info("Warm-up complete — ready to serve.")
        except Exception:
            logger.critical("Startup warm-up failed — aborting.", exc_info=True)
            raise

        app.state.ready = True
        connection_test_loop.start()
        yield

        # Graceful shutdown: flip readiness so the load balancer stops routing
        # here, then let uvicorn drain in-flight requests (bounded by its
        # --timeout-graceful-shutdown). No persistent resources to release.
        app.state.ready = False
        connection_test_loop.stop()
        logger.info("Shutdown — readiness withdrawn, draining in-flight requests.")

    app = FastAPI(
        title="Dometria Bulk Insert",
        version="0.1.0",
        description=_DESCRIPTION,
        openapi_tags=_OPENAPI_TAGS,
        lifespan=lifespan,
    )
    app.state.settings = effective_cfg
    app.add_middleware(
        RequestContextMiddleware,
        max_request_bytes=effective_cfg.MAX_REQUEST_BYTES,
    )
    app.include_router(router)
    return app
