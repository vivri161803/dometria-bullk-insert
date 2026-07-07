"""Exceptions for Block 3 v2 — Tree-Driven Asset Extraction."""

from __future__ import annotations

__all__: list[str] = ["AssetExtractionError"]


class AssetExtractionError(Exception):
    """Raised on structural extraction failures (schema/model construction).

    A *low confidence* or incomplete extraction never raises — the best-effort
    asset is returned with flags/review-queue populated (fail-safe over
    fail-fast).  This exception covers only structural problems such as a
    malformed field tree or an un-buildable extraction model.
    """
