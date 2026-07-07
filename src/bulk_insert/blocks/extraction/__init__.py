"""Block 3 v2 — Tree-Driven Asset Extraction (Phase 6).

Public entrypoint :func:`extract_asset` is wired in :mod:`.orchestrator`.
"""

from __future__ import annotations

from bulk_insert.blocks.extraction.exceptions import AssetExtractionError
from bulk_insert.blocks.extraction.orchestrator import extract_asset

__all__: list[str] = ["extract_asset", "AssetExtractionError"]
