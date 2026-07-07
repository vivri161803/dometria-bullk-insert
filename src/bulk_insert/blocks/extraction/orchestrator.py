"""Block 3 v2 orchestrator — the public :func:`extract_asset` entrypoint (§2.1).

Composes the tree-driven stages into the stable public contract::

    Contract(lv1, lv2, lv3) + scored chunks (Block 1)
        → select_tree_fields      (Stage 1: prune the tree to this product)
        → enrich                  (overlay the metadata sidecar → FieldNodes)
        → extract_fields          (Stages 3-4: partition by topic, sequential,
                                   strategy-routed, evidence-grounded)
        → normalize_fields        (Stage 4b: deterministic value canonicalization)
        → resolve_out_of_vocab    (Stage 4c: scoped judge for OUT_OF_VOCAB enums)
        → derive_computed         (Stage 5+7: Python derivations + reconcile)
        → derive_judged           (Stage 6: isolated primitives-only LLM run)
        → ground_and_assemble     (Stage 8: entailment gate + completeness)
        → Asset(contract_id, data=record, confidence, evidence, flags, review_queue)

The signature is unchanged from Phase 4, so ``pipeline.py`` and ``PipelineResult``
are untouched.  Fail-safe is preserved: low confidence / missing optional fields
never raise; only structural problems (contract/tree not found) raise.
"""

from __future__ import annotations

import logging

import instructor

from bulk_insert.blocks.extraction.client import build_instructor_client
from bulk_insert.blocks.extraction.derive import derive_computed, derive_judged
from bulk_insert.blocks.extraction.exceptions import AssetExtractionError
from bulk_insert.blocks.extraction.extract import extract_fields
from bulk_insert.blocks.extraction.ground import ground_and_assemble
from bulk_insert.blocks.extraction.metadata import enrich
from bulk_insert.blocks.extraction.normalize import normalize_fields
from bulk_insert.blocks.extraction.resolve import resolve_out_of_vocab
from bulk_insert.blocks.extraction.tree import select_tree_fields
from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings
from bulk_insert.models.asset import Asset
from bulk_insert.models.chunk import Chunk
from bulk_insert.models.contract import Contract
from bulk_insert.models.contract_loader import contract_id_for

logger = logging.getLogger(__name__)

__all__: list[str] = ["extract_asset"]


def extract_asset(
    chunks: list[Chunk],
    contract: Contract,
    cfg: Settings | None = None,
    client: instructor.Instructor | None = None,
) -> Asset:
    """Full Block 3 v2 pipeline: chunks + Contract → evidence-grounded Asset.

    Parameters
    ----------
    chunks:
        Scored, coreference-resolved chunks from Block 1.
    contract:
        The ``Contract`` produced by Block 2 (lv1/lv2/lv3 populated).
    cfg:
        Optional settings override.  Defaults to the process-level singleton.
    client:
        Optional pre-built Instructor client (injected in tests).

    Returns
    -------
    Asset
        ``data`` = validated record, ``confidence`` = grounding × completeness,
        with optional ``evidence`` / ``flags`` / ``review_queue``.

    Raises
    ------
    ContractNotFoundError
        If no contract file matches ``contract.lv3``.
    AssetExtractionError
        On structural failures (missing category path in the field tree).
    """
    effective_cfg = cfg or _default_settings

    # Resolve the contract id (validates the lv3 exists; raises ContractNotFoundError).
    contract_id = contract_id_for(contract.lv3, effective_cfg.CONTRACTS_DIR)

    # Stage 1 — select this product's structural fields from the tree.
    logger.info("Block 3 v2 — Stage 1: selecting fields for '%s'.", contract_id)
    try:
        tree_fields = select_tree_fields(contract, effective_cfg)
    except KeyError as exc:
        raise AssetExtractionError(str(exc)) from exc

    # Overlay the metadata sidecar → enriched field nodes.
    lv_path = (contract.lv1, contract.lv2, contract.lv3)
    nodes = enrich(tree_fields, lv_path, effective_cfg)

    llm = client or build_instructor_client(effective_cfg)

    # Stages 3–4 — partition by topic + sequential, strategy-routed extraction.
    logger.info("Block 3 v2 — Stages 3-4: extraction (%d fields).", len(nodes))
    result = extract_fields(chunks, nodes, effective_cfg, client=llm)

    # Stage 4b — deterministic value normalization (before derivation, so any
    # computed rule / reconcile operates on canonical numbers).
    normalize_fields(result, nodes, effective_cfg)

    # Stage 4c — resolve OUT_OF_VOCAB enum fields from their own quote (§9).
    resolve_out_of_vocab(result, nodes, effective_cfg, client=llm)

    # Stage 5+7 — computed derivations + reconcile (deterministic).
    derive_computed(result, nodes, effective_cfg)

    # Stage 6 — judged derivations (only if any exist).
    derive_judged(result, nodes, effective_cfg, client=llm)

    # Stage 8 — grounding gate + completeness + assemble.
    grounded = ground_and_assemble(chunks, result, nodes, effective_cfg, client=llm)

    asset = Asset(
        contract_id=contract_id,
        data=grounded.record,
        confidence=grounded.confidence,
        evidence=grounded.evidence or None,
        flags=grounded.flags or None,
        review_queue=grounded.review_queue or None,
    )
    logger.info(
        "Block 3 v2 complete. contract='%s' confidence=%.3f review_queue=%d.",
        contract_id,
        grounded.confidence,
        len(grounded.review_queue),
    )
    return asset
