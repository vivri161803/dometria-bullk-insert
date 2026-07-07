"""FastAPI application factory with lifespan warm-up."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bulk_insert.api.routes import router
from bulk_insert.blocks import contract as contract_block
from bulk_insert.blocks import preprocessing
from bulk_insert.blocks.extraction import metadata, tree
from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings

__all__: list[str] = ["create_app"]

logger = logging.getLogger(__name__)


def create_app(cfg: Settings | None = None) -> FastAPI:
    """Build the FastAPI application with lifespan warm-up."""
    effective_cfg = cfg or _default_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Warm heavy resources before the app accepts traffic."""
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
        yield

    app = FastAPI(title="Dometria Bulk Insert", lifespan=lifespan)
    app.state.settings = effective_cfg
    app.include_router(router)
    return app
