"""Shared LLM call helpers.

A single source of truth for the per-request sampling parameters sent on every
Instructor ``chat.completions.create`` call across all three blocks (scoring,
lv1, extraction). Centralising this keeps the determinism knobs — temperature,
seed, and OpenRouter provider pinning — consistent and tunable from ``Settings``
rather than duplicated inline in each block.
"""

from __future__ import annotations

from typing import Any

from bulk_insert.config import Settings

__all__: list[str] = ["sampling_params"]


def _provider_routing(cfg: Settings) -> dict[str, Any]:
    """Build the OpenRouter ``provider`` routing block from settings.

    Pinning the upstream provider is the highest-leverage lever against
    run-to-run variance: the same model slug can otherwise be served by
    different providers with different numerics. ``require_parameters`` also
    ensures a request carrying ``seed`` is only routed to a provider that
    actually honours it.
    """
    provider: dict[str, Any] = {
        "allow_fallbacks": cfg.OPENROUTER_ALLOW_FALLBACKS,
    }
    if cfg.OPENROUTER_PROVIDER_ORDER:
        provider["order"] = list(cfg.OPENROUTER_PROVIDER_ORDER)
    if cfg.OPENROUTER_REQUIRE_PARAMETERS:
        provider["require_parameters"] = True
    return provider


def sampling_params(cfg: Settings) -> dict[str, Any]:
    """Return the kwargs controlling sampling + provider routing for one call.

    Includes ``temperature`` always, ``seed`` only when configured (so the call
    site can assert its absence), and ``extra_body={"provider": {...}}`` to pin
    OpenRouter routing. Spread directly into ``client.chat.completions.create``.
    """
    params: dict[str, Any] = {
        "temperature": cfg.LLM_TEMPERATURE,
        "extra_body": {"provider": _provider_routing(cfg)},
    }
    if cfg.LLM_SEED is not None:
        params["seed"] = cfg.LLM_SEED
    return params
