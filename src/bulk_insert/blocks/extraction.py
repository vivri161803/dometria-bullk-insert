"""Block 3 — Asset Extraction.

Public entry point
------------------
:func:`extract_asset` orchestrates the full pipeline::

    Contract(lv1, lv2, lv3) + scored chunks (from Block 1)
        → load_contract_schema   (locate + parse the {id}_{lv3}.json file)
        → build_dynamic_model     (contract fields → all-optional Pydantic model)
        → single GPT-4o call      (Instructor populates fields + self-reports confidence)
        → confidence retry loop   (re-extract while confidence < threshold; keep best)
        → Asset(contract_id, data, confidence)

Design notes
------------
* **Single batched call** — the scored chunks already represent the relevant
  document content; one heavy-model call avoids per-chunk merge logic.
* **All fields optional** — a field the LLM cannot find comes back ``None``
  rather than failing the whole extraction (fail-safe over fail-fast).  The
  contract's ``is_required`` flag is preserved as field metadata for downstream.
* **Confidence-driven retries** — the LLM self-reports an overall confidence;
  extraction is retried while below ``EXTRACTION_MIN_CONFIDENCE``.  On exhausting
  attempts the best attempt is returned (never raises on low confidence).
* **Lean prompt** — fields the LLM cannot derive from free text (foreign ids,
  GPS coordinate pairs, non-text ``native_location`` fields) are excluded.

All functions are independently importable for unit testing.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import instructor
from pydantic import BaseModel, Field, create_model

from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings
from bulk_insert.models.asset import Asset
from bulk_insert.models.chunk import Chunk
from bulk_insert.models.contract import Contract
from bulk_insert.models.contract_loader import contract_id_for, load_contract_schema

logger = logging.getLogger(__name__)

# Default options for boolean fields whose constraints omit an explicit list.
_DEFAULT_BOOLEAN_OPTIONS: tuple[str, ...] = ("si", "no", "non dichiarato")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AssetExtractionError(Exception):
    """Raised on schema/model-construction failures.

    Note: a *low confidence* extraction does NOT raise — the best attempt is
    returned and a warning is logged (fail-safe).  This exception covers
    structural problems such as a malformed contract schema.
    """


# ---------------------------------------------------------------------------
# Dynamic model construction
# ---------------------------------------------------------------------------


def _annotation_for_field(field: dict[str, object]) -> object:
    """Map a contract field definition to a Python/typing annotation.

    text/iso_alpha2 → str, integer → int, decimal/currency → float,
    boolean/select → ``Literal[*options]``.  Unknown types fall back to ``str``.
    """
    field_type = field.get("type")

    if field_type in ("text", "iso_alpha2"):
        return str
    if field_type == "integer":
        return int
    if field_type in ("decimal", "currency"):
        return float
    if field_type in ("boolean", "select"):
        options = _options_for_field(field)
        if options:
            # Literal[(a, b, c)] is equivalent to Literal[a, b, c].
            return Literal[options]
        return str

    # Defensive fallback for any unmapped type.
    return str


def _options_for_field(field: dict[str, object]) -> tuple[str, ...]:
    """Extract the allowed-value options from a field's constraints."""
    constraints = field.get("constraints")
    if isinstance(constraints, dict):
        options = constraints.get("options")
        if isinstance(options, list) and options:
            return tuple(str(o) for o in options)
    if field.get("type") == "boolean":
        return _DEFAULT_BOOLEAN_OPTIONS
    return ()


def _should_include_field(field: dict[str, object], excluded_types: tuple[str, ...]) -> bool:
    """Decide whether a field is LLM-extractable and belongs in the model.

    A field is excluded when its ``type`` is in *excluded_types* (e.g.
    ``foreign_id``, ``decimal_pair``) OR when it is a ``native_location`` field
    that is not plain text (geo-resolution ids and coordinates).
    """
    field_type = field.get("type")
    if field_type in excluded_types:
        return False
    if field.get("source") == "native_location" and field_type != "text":
        return False
    return True


def build_dynamic_model(
    schema: dict[str, object],
    model_name: str = "ExtractedAsset",
    excluded_types: tuple[str, ...] = ("foreign_id", "decimal_pair"),
) -> type[BaseModel]:
    """Build an all-optional Pydantic model from ``schema['fields']['native']``.

    Every included field becomes ``Optional`` with a ``None`` default so the LLM
    can omit fields it cannot find.  The contract's ``is_required`` flag is
    preserved in ``json_schema_extra`` for downstream consumers.

    Parameters
    ----------
    schema:
        The ``data`` section of a contract (from :func:`load_contract_schema`).
    model_name:
        Name of the generated Pydantic model.
    excluded_types:
        Field types to drop entirely (non-LLM-derivable).

    Returns
    -------
    type[BaseModel]
        A dynamically constructed Pydantic model class.
    """
    fields_section = schema.get("fields")
    native_fields: list[dict[str, object]] = []
    if isinstance(fields_section, dict):
        raw = fields_section.get("native")
        if isinstance(raw, list):
            native_fields = [f for f in raw if isinstance(f, dict)]

    definitions: dict[str, object] = {}
    excluded_count = 0
    for field in native_fields:
        if not _should_include_field(field, excluded_types):
            excluded_count += 1
            continue

        key = field.get("key")
        if not isinstance(key, str):
            continue

        annotation = _annotation_for_field(field)
        label = field.get("label")
        description = label if isinstance(label, str) else key
        is_required = bool(field.get("is_required", False))

        field_info = Field(
            default=None,
            description=description,
            json_schema_extra={"required_by_contract": is_required},
        )
        # (annotation | None, FieldInfo) — Optional with None default.
        definitions[key] = (annotation | None, field_info)  # type: ignore[operator]

    logger.info(
        "Block 3 — dynamic model '%s': %d field(s) included, %d excluded.",
        model_name,
        len(definitions),
        excluded_count,
    )

    # create_model's signature defeats static typing for dynamic field maps.
    return create_model(model_name, **definitions)  # type: ignore[call-overload, no-any-return]


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM_PROMPT = (
    "You are an expert Italian real-estate and business document extractor.\n\n"
    "From the document content below, populate the structured fields of the "
    "provided schema. Follow these rules strictly:\n"
    "- Extract values ONLY when supported by the document. If a field is not "
    "mentioned or cannot be inferred, leave it null.\n"
    "- For fields with a fixed set of allowed values, you MUST return one of "
    "those exact values.\n"
    "- Do not invent identifiers, coordinates, or figures that are not present.\n"
    "- Also report an overall 'confidence' between 0.0 and 1.0 reflecting how "
    "well the extraction is supported by the document text."
)


def _build_instructor_client(cfg: Settings) -> instructor.Instructor:
    """Construct an Instructor client pointed at OpenRouter using the heavy model."""
    client: instructor.Instructor = instructor.from_provider(
        f"openai/{cfg.MODEL_HEAVY}",
        base_url=cfg.OPENROUTER_BASE_URL,
        api_key=cfg.OPENROUTER_API_KEY,
    )
    return client


def _build_response_model(asset_model: type[BaseModel]) -> type[BaseModel]:
    """Wrap the dynamic asset model with a sibling confidence float in [0, 1]."""
    return create_model(
        f"{asset_model.__name__}Response",
        asset=(asset_model, ...),
        confidence=(float, Field(ge=0.0, le=1.0)),
    )


def _run_extraction(
    client: instructor.Instructor,
    response_model: type[BaseModel],
    document_text: str,
    model: str,
    *,
    temperature: float = 0.0,
    seed: int | None = None,
) -> tuple[BaseModel, float]:
    """Run a single Instructor extraction call.

    Returns
    -------
    tuple[BaseModel, float]
        The populated asset-model instance and the self-reported confidence.
    """
    # Instructor returns an instance of `response_model` (the dynamic wrapper);
    # type it as Any to access the dynamically named `asset`/`confidence` fields.
    sampling: dict[str, Any] = {"temperature": temperature}
    if seed is not None:
        sampling["seed"] = seed
    result: Any = client.chat.completions.create(
        model=model,
        response_model=response_model,
        max_retries=3,
        messages=[
            {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": document_text},
        ],
        **sampling,
    )
    asset_obj: BaseModel = result.asset
    confidence = float(result.confidence)
    return asset_obj, confidence


# ---------------------------------------------------------------------------
# Orchestrator (public API)
# ---------------------------------------------------------------------------


def extract_asset(
    chunks: list[Chunk],
    contract: Contract,
    cfg: Settings | None = None,
) -> Asset:
    """Full Block 3 pipeline: chunks + Contract → populated Asset.

    Pipeline steps
    --------------
    1. **Load** the contract schema by ``contract.lv3``.
    2. **Build** an all-optional dynamic Pydantic model from the fields.
    3. **Extract** via a single GPT-4o Instructor call (with a confidence
       retry loop, keeping the highest-confidence attempt).
    4. **Assemble** and return an ``Asset``.

    Parameters
    ----------
    chunks:
        Scored, coreference-resolved chunks from Block 1.
    contract:
        The ``Contract`` produced by Block 2 (lv1/lv2/lv3 populated).
    cfg:
        Optional settings override.  Defaults to the process-level singleton.

    Returns
    -------
    Asset
        The populated asset.  On persistently low confidence, the best attempt
        is returned with a warning logged — never raised.

    Raises
    ------
    ContractNotFoundError
        If no contract file matches ``contract.lv3``.
    """
    effective_cfg = cfg or _default_settings

    # Step 1: Load schema + resolve contract id.
    logger.info("Block 3 — Step 1/3: Loading contract schema for lv3='%s'.", contract.lv3)
    schema = load_contract_schema(contract.lv3, effective_cfg.CONTRACTS_DIR)
    contract_id = contract_id_for(contract.lv3, effective_cfg.CONTRACTS_DIR)

    # Step 2: Build the dynamic model + wrapper response model.
    logger.info("Block 3 — Step 2/3: Building dynamic model for contract '%s'.", contract_id)
    asset_model = build_dynamic_model(
        schema,
        model_name=f"Asset_{contract.lv3}",
        excluded_types=effective_cfg.EXTRACTION_EXCLUDED_TYPES,
    )
    response_model = _build_response_model(asset_model)

    # Step 3: Extraction with confidence retry loop.
    logger.info("Block 3 — Step 3/3: LLM extraction (heavy model).")
    document_text = "\n---\n".join(c.text for c in sorted(chunks, key=lambda c: c.index))
    client = _build_instructor_client(effective_cfg)
    # `from_provider` uses the `openai/` provider prefix; the model sent to
    # OpenRouter is the bare configured slug (no extra prefix).
    model = effective_cfg.MODEL_HEAVY
    threshold = effective_cfg.EXTRACTION_MIN_CONFIDENCE
    temperature = effective_cfg.LLM_TEMPERATURE
    seed = effective_cfg.LLM_SEED
    # At temperature 0 every attempt is (near-)identical, so best-of-N retries
    # only waste calls — collapse the confidence loop to a single attempt.
    max_attempts = 1 if temperature == 0.0 else effective_cfg.EXTRACTION_MAX_ATTEMPTS

    best_asset: BaseModel | None = None
    best_confidence = -1.0
    for attempt in range(1, max_attempts + 1):
        asset_obj, confidence = _run_extraction(
            client,
            response_model,
            document_text,
            model,
            temperature=temperature,
            seed=seed,
        )
        logger.info(
            "Block 3 — extraction attempt %d/%d: confidence=%.3f.",
            attempt,
            max_attempts,
            confidence,
        )
        if confidence > best_confidence:
            best_confidence = confidence
            best_asset = asset_obj
        if confidence >= threshold:
            break

    # best_asset is guaranteed non-None: the loop runs at least once.
    assert best_asset is not None

    if best_confidence < threshold:
        logger.warning(
            "Block 3 — extraction confidence %.3f below threshold %.3f after %d attempt(s). "
            "Returning best attempt for contract '%s'.",
            best_confidence,
            threshold,
            max_attempts,
            contract_id,
        )

    asset = Asset(
        contract_id=contract_id,
        data=best_asset.model_dump(),
        confidence=best_confidence,
    )
    logger.info(
        "Block 3 complete. Asset for contract '%s' with confidence=%.3f.",
        contract_id,
        best_confidence,
    )
    return asset


__all__: list[str] = [
    "extract_asset",
    "build_dynamic_model",
    "AssetExtractionError",
]
