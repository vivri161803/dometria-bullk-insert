"""Shared Instructor-client helpers for Block 3 v2.

One place to build the OpenRouter-backed Instructor client and to turn Block 1's
scored chunks into the concatenated source text every extraction pass consumes.
"""

from __future__ import annotations

import instructor

from bulk_insert.config import Settings
from bulk_insert.models.chunk import Chunk

__all__: list[str] = ["build_instructor_client", "document_text"]


def build_instructor_client(cfg: Settings, model: str | None = None) -> instructor.Instructor:
    """Construct an Instructor client pointed at OpenRouter.

    The ``openai/`` here is only Instructor's *provider* prefix; the model sent to
    OpenRouter is the bare configured slug, never double-prefixed. ``model``
    defaults to ``cfg.MODEL_HEAVY`` (what the extraction blocks run on); callers on
    a different slug — e.g. the offline compile on ``MODEL_COMPILE`` — pass it
    explicitly so the client's default model, and its init log, match what is
    actually used.
    """
    client: instructor.Instructor = instructor.from_provider(
        f"openai/{model or cfg.MODEL_HEAVY}",
        base_url=cfg.OPENROUTER_BASE_URL,
        api_key=cfg.OPENROUTER_API_KEY,
        # SDK-level backoff: retries 429 / transient upstream errors with
        # exponential backoff, honouring OpenRouter's Retry-After (Phase 9).
        max_retries=cfg.LLM_MAX_RETRIES,
        timeout=cfg.LLM_TIMEOUT,
    )
    return client


def document_text(chunks: list[Chunk]) -> str:
    """Concatenate scored chunks in document order into one source text block."""
    return "\n---\n".join(c.text for c in sorted(chunks, key=lambda c: c.index))
