# Requirements — Block 3: Asset Extraction

## Scope

This feature covers **Phase 4 (Block 3: Asset Extraction)** from the [roadmap](file:///Users/vivri161803/Documents/Code/Dometria/bulk_insert/specs/roadmap.md). It implements the logic that takes the `Contract(lv1, lv2, lv3)` produced by Block 2, loads the matching JSON contract schema, builds a dynamic Pydantic model from its field definitions, and populates it with a single high-capacity LLM call — returning a validated `Asset`.

> This block is the **final extraction stage**. It does NOT handle Celery wiring, database persistence, or the FastAPI layer (Phases 6–8).

---

## Context

Block 2 ([`contract.py`](file:///Users/vivri161803/Documents/Code/Dometria/bulk_insert/src/bulk_insert/blocks/contract.py)) returns a `Contract` with `lv1`/`lv2`/`lv3` populated and `schema_def={}` — the category path is known, but the schema is not yet loaded. Each `lv3` code maps 1:1 to a JSON contract file in [`contracts/`](file:///Users/vivri161803/Documents/Code/Dometria/bulk_insert/contracts) following the naming convention `{id}_{lv3}.json` (e.g. `162_restaurant.json`).

### Contract file structure

Each contract is a JSON document with this shape (see [`162_restaurant.json`](file:///Users/vivri161803/Documents/Code/Dometria/bulk_insert/contracts/162_restaurant.json)):

```jsonc
{
  "data": {
    "schema_version": "3",
    "category": { "id": 162, "level": 3, "name": "Ristorante", "parent": { ... } },
    "fields": {
      "native": [
        {
          "key": "restaurant_numero_coperti_interni",
          "label": "Numero coperti interni",
          "source": "custom_fields",        // standard | native_location | custom_fields
          "type": "integer",                 // text|integer|decimal|currency|boolean|select|foreign_id|iso_alpha2|decimal_pair
          "is_required": true,
          "constraints": { "options": [...], "min": 0, ... },
          "unit": null,
          "transform": [...]
        }
      ]
    }
  }
}
```

### Upstream dependency

Block 3 consumes:
- A `list[Chunk]` of scored, coreference-resolved chunks from Block 1.
- A `Contract` from Block 2 (`lv1`, `lv2`, `lv3`; `schema_def` empty).

---

## Functional Requirements

### FR-1: Contract Schema Loading

| ID | Requirement |
|---|---|
| FR-1.1 | A new module `src/bulk_insert/models/contract_loader.py` MUST expose `load_contract_schema(lv3: str, contracts_dir: Path \| None = None) -> dict`. |
| FR-1.2 | The loader MUST locate the contract file by globbing `*_{lv3}.json` in the contracts directory (default from `Settings.CONTRACTS_DIR`). |
| FR-1.3 | If no file matches, the loader MUST raise `ContractNotFoundError`. If more than one file matches, it MUST also raise `ContractNotFoundError` (ambiguous lv3). |
| FR-1.4 | The loader MUST return the parsed `data` section (containing `category` and `fields`), not the outer wrapper. |
| FR-1.5 | A helper MUST derive the `contract_id` as the filename **stem** (e.g. `162_restaurant`) for use on the resulting `Asset`. |

### FR-2: Dynamic Pydantic Model Construction

| ID | Requirement |
|---|---|
| FR-2.1 | `build_dynamic_model(schema: dict, model_name: str = "ExtractedAsset") -> type[BaseModel]` MUST iterate `schema["fields"]["native"]` and build a Pydantic model via `pydantic.create_model()`. |
| FR-2.2 | Field type mapping: `text`→`str`, `integer`→`int`, `decimal`/`currency`→`float`, `iso_alpha2`→`str`. |
| FR-2.3 | `boolean` and `select` fields MUST map to `typing.Literal[*options]`, where `options` come from `constraints.options`. (`boolean` carries `["si", "no", "non dichiarato"]` — the third state MUST be preserved.) |
| FR-2.4 | **All** fields MUST be `Optional[T]` with default `None`, regardless of `is_required`. Field definitions use the `(annotation \| None, None)` tuple form. |
| FR-2.5 | Each field MUST carry its `label` as the Pydantic `Field(description=...)` to guide the LLM. The original `is_required` flag MUST be preserved as field metadata (e.g. `json_schema_extra`) so downstream consumers can flag missing-but-required values — but it MUST NOT make the field required. |
| FR-2.6 | **Field exclusion (lean prompt):** a field MUST be excluded from the dynamic model when `type ∈ Settings.EXTRACTION_EXCLUDED_TYPES` (default `{"foreign_id", "decimal_pair"}`) **OR** when `source == "native_location"` and `type != "text"`. This removes non-LLM-derivable fields (`geo_municipality_id`, `municipality_id`, `latitude`, `longitude`, `country`) while keeping all location text fields. |

### FR-3: LLM Extraction (single batched call)

| ID | Requirement |
|---|---|
| FR-3.1 | The block MUST build a **wrapper response model** with two fields: `fields: <DynamicModel>` and `confidence: float` (constrained `ge=0.0, le=1.0`). This is passed as Instructor's `response_model`. |
| FR-3.2 | The block MUST concatenate the scored chunk texts (ordered by chunk `index`) into a single user message — one batched call, not per-chunk. |
| FR-3.3 | The call MUST use the heavy model (`Settings.MODEL_HEAVY`, default `openai/gpt-4o`) via Instructor's `from_provider()` against OpenRouter, with `max_retries=3` for transient/validation self-correction. |
| FR-3.4 | The system prompt MUST instruct the LLM to populate only fields supported by the document, leave unknown fields as `null`, respect the allowed `Literal` values, and self-report an overall `confidence` in `[0, 1]` reflecting how well the extraction is supported by the text. |

### FR-4: Confidence-Based Re-Extraction

| ID | Requirement |
|---|---|
| FR-4.1 | After each extraction attempt, the block MUST compare `confidence` against `Settings.EXTRACTION_MIN_CONFIDENCE` (default `0.6`). |
| FR-4.2 | While `confidence < threshold`, the block MUST re-run the extraction, up to `Settings.EXTRACTION_MAX_ATTEMPTS` (default `3`) total attempts. |
| FR-4.3 | The block MUST track the **highest-confidence** attempt across all tries. |
| FR-4.4 | On exhausting attempts without reaching the threshold, the block MUST return the best attempt (fail-safe) and log a `WARNING` — it MUST NOT raise on low confidence. |
| FR-4.5 | The first attempt that meets or exceeds the threshold MUST short-circuit the loop. |

### FR-5: Asset Assembly & Output

| ID | Requirement |
|---|---|
| FR-5.1 | The block MUST return an `Asset` with `contract_id` (filename stem), `data` (the dynamic model dumped via `model_dump()`, retaining `None` values so every extracted key is present), and `confidence` (best attempt's value). |
| FR-5.2 | The returned `Asset` MUST pass `Asset` Pydantic validation (`confidence` in `[0, 1]`). |

### FR-6: Public API

| ID | Requirement |
|---|---|
| FR-6.1 | `src/bulk_insert/blocks/extraction.py` MUST expose `extract_asset(chunks: list[Chunk], contract: Contract, cfg: Settings \| None = None) -> Asset` as the Block 3 entry point. |
| FR-6.2 | `load_contract_schema`, `build_dynamic_model`, `extract_asset`, `ContractNotFoundError`, and `AssetExtractionError` MUST be independently importable and re-exported from their package `__init__` files. |

---

## Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-1 | All new code MUST pass `mypy --strict`. Dynamic-model typing may require localized, commented `# type: ignore` where `create_model` defeats static typing. |
| NFR-2 | All new code MUST pass `ruff check` and `ruff format`. |
| NFR-3 | Structured logging at each step: contract file resolved, field count (included vs excluded), each attempt's confidence, final selection. |
| NFR-4 | All thresholds, model name, contracts directory, and excluded types MUST be configurable via `Settings` (sourced from `.env`). |
| NFR-5 | Unit tests MUST mock the LLM; no live network calls in `tests/unit`. A gated integration test (`@pytest.mark.integration`) MAY exercise a real call. |

---

## Design Decisions

| Decision | Rationale |
|---|---|
| **Single batched LLM call** | Mirrors Block 2's simplicity. The scored chunks already represent the relevant document content; one GPT-4o call avoids per-chunk merge-conflict logic and reduces token/latency overhead. |
| **`Literal[*options]` for boolean/select** | Instructor + Pydantic `Literal` constrains the LLM to exact allowed values and preserves the three-state `boolean` (`si`/`no`/`non dichiarato`) instead of collapsing to a Python `bool`. |
| **All fields optional, default `None`** | Aligns with the "fail-safe over fail-fast" principle. A field the LLM cannot find from the text comes back `null` rather than failing the whole extraction. `is_required` is retained as metadata so downstream can flag gaps. |
| **Wrapper response model for confidence** | Adding `confidence` as a sibling of `fields` (rather than injecting a synthetic field into the dynamic model) avoids any key collision with contract field keys and keeps the asset data clean. |
| **Confidence-driven re-extraction** | A configurable threshold (default 0.6) lets low-quality extractions be retried automatically. Returning the best attempt on exhaustion preserves intermediate work; downstream/manual review handles persistently low confidence. |
| **Declarative field exclusion** | Exclusion is driven by the schema's own `type`/`source` metadata, not hardcoded field keys, so it generalizes across all 97 contracts. The `native_location && non-text` guard catches the geo-resolution integer (`municipality_id`) that a pure type denylist misses. |
| **`contract_id` = filename stem** | The stem (`162_restaurant`) is unique and self-documenting, and is produced for free while globbing the file. |

### Known consequence to confirm at implementation

The approved exclusion rule also drops `country` (`source: native_location`, `type: iso_alpha2`), which an LLM *could* plausibly derive. This is collateral of the `native_location && non-text` guard. If keeping `country` is desired, add `iso_alpha2` to a keep-exception during implementation review — both the denylist and the guard are configurable.

---

## Out of Scope

- Celery task wiring / async orchestration (Phase 6).
- Database persistence of assets (Phase 5).
- FastAPI endpoints (Phase 7).
- Per-field confidence (high/medium/low) and the `confidence_constraints` in the contract — only a whole-asset confidence is produced.
- Applying the contract `transform` pipeline (`trim`, `parse_decimal`, geocoding, etc.) — transforms are a downstream/normalization concern.
- Docker / deployment configuration (Phase 8).
