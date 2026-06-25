# Plan — Block 3: Asset Extraction

> Implementation order. Each task group is a logical unit that can be code-reviewed independently.
> **No code is written until this spec is approved.**

---

## Task Group 1: Configuration Updates

**Files:** `src/bulk_insert/config.py`, `tests/unit/test_config.py`

1. Add the following settings (with `.env` overridability):
   - `CONTRACTS_DIR: Path = Path("contracts")` — directory holding the 97 JSON contracts (resolved relative to project root).
   - `EXTRACTION_MAX_ATTEMPTS: int = 3` — max extraction attempts for the confidence loop.
   - `EXTRACTION_MIN_CONFIDENCE: float = 0.6` — confidence threshold to short-circuit retries.
   - `EXTRACTION_EXCLUDED_TYPES: tuple[str, ...] = ("foreign_id", "decimal_pair")` — field types always excluded from the dynamic model.
2. (`MODEL_HEAVY` already exists — no change.)

**Tests:** verify new defaults load; verify `.env` override for `EXTRACTION_MIN_CONFIDENCE`.

---

## Task Group 2: Contract Loader

**Files:** `src/bulk_insert/models/contract_loader.py`, `src/bulk_insert/models/__init__.py`

1. Define exception:
   ```python
   class ContractNotFoundError(Exception):
       """Raised when no (or an ambiguous) contract file matches an lv3 code."""
   ```
2. Implement:
   ```python
   def load_contract_schema(lv3: str, contracts_dir: Path | None = None) -> dict[str, object]:
       """Locate contracts/*_{lv3}.json, parse it, return the `data` section."""
   ```
   - Glob `*_{lv3}.json`; 0 matches → `ContractNotFoundError`; >1 → `ContractNotFoundError`.
   - Read + `json.loads`; return `payload["data"]`.
3. Implement:
   ```python
   def contract_id_for(lv3: str, contracts_dir: Path | None = None) -> str:
       """Return the filename stem, e.g. '162_restaurant'."""
   ```
   (or return both path + stem from a single resolve helper to avoid double globbing.)
4. Re-export `load_contract_schema`, `ContractNotFoundError` from `models/__init__.py`.

**Tests:** `tests/unit/test_contract_loader.py`
- `test_load_contract_schema_restaurant`: returns dict with `category` + `fields.native`.
- `test_load_contract_schema_unknown_lv3_raises`: `ContractNotFoundError`.
- `test_contract_id_for_restaurant`: returns `"162_restaurant"`.
- Use a `tmp_path` fixture with a couple of synthetic contract files (do not depend on glob ambiguity in the real folder).

---

## Task Group 3: Dynamic Model Builder

**Files:** `src/bulk_insert/blocks/extraction.py`

1. Type mapping helper:
   ```python
   def _annotation_for_field(field: dict[str, object]) -> object:
       """Map a contract field definition to a Python/typing annotation."""
   ```
   - `text`/`iso_alpha2` → `str`; `integer` → `int`; `decimal`/`currency` → `float`.
   - `boolean`/`select` → `Literal[*options]` from `constraints.options` (boolean falls back to `("si","no","non dichiarato")`).
2. Exclusion predicate:
   ```python
   def _should_include_field(field: dict[str, object], excluded_types: tuple[str, ...]) -> bool:
       """Exclude foreign_id/decimal_pair and non-text native_location fields."""
   ```
3. Builder:
   ```python
   def build_dynamic_model(
       schema: dict[str, object],
       model_name: str = "ExtractedAsset",
       excluded_types: tuple[str, ...] = ("foreign_id", "decimal_pair"),
   ) -> type[BaseModel]:
       """Build an all-optional Pydantic model from schema['fields']['native']."""
   ```
   - For each included field: definition = `(annotation | None, Field(default=None, description=label, json_schema_extra={"required_by_contract": is_required}))`.
   - `return create_model(model_name, **definitions)`.

**Tests:** `tests/unit/test_extraction.py` (builder section)
- `test_build_dynamic_model_field_types`: text→str, integer→int, currency→float annotations on the model.
- `test_build_dynamic_model_boolean_is_literal`: boolean field annotation is `Literal["si","no","non dichiarato"]`.
- `test_build_dynamic_model_select_is_literal`: select options become a `Literal`.
- `test_build_dynamic_model_all_optional`: every field defaults to `None`; model instantiates with no args.
- `test_build_dynamic_model_excludes_geo_fields`: `geo_municipality_id`, `municipality_id`, `latitude`, `longitude`, `country` absent; `locality`, `address_line` present.
- `test_build_dynamic_model_preserves_required_metadata`: `is_required` reflected in `json_schema_extra`.

---

## Task Group 4: Extraction Orchestrator

**Files:** `src/bulk_insert/blocks/extraction.py` (continued)

1. Exception:
   ```python
   class AssetExtractionError(Exception):
       """Raised on schema/model-construction failures (NOT low confidence)."""
   ```
2. Wrapper response model factory:
   ```python
   def _build_response_model(asset_model: type[BaseModel]) -> type[BaseModel]:
       """Wrap the dynamic model with a sibling confidence float in [0, 1]."""
       # fields: asset_model ; confidence: float = Field(ge=0, le=1)
   ```
3. Instructor client helper `_build_instructor_client(cfg)` using `MODEL_HEAVY` (mirror `contract.py`).
4. System prompt constant `_EXTRACTION_SYSTEM_PROMPT` (Italian-document extraction; null for unknown; respect Literals; self-report confidence).
5. Single-attempt call:
   ```python
   def _run_extraction(client, response_model, document_text) -> tuple[BaseModel, float]:
       """One Instructor call with max_retries=3; returns (fields_obj, confidence)."""
   ```
6. Public entry point:
   ```python
   def extract_asset(chunks, contract, cfg=None) -> Asset:
       """
       1. load_contract_schema(contract.lv3) + resolve contract_id (stem).
       2. build_dynamic_model -> wrapper response model.
       3. concatenate chunk texts (ordered by index).
       4. confidence loop: up to EXTRACTION_MAX_ATTEMPTS; keep best;
          break when confidence >= EXTRACTION_MIN_CONFIDENCE.
       5. WARNING log if best < threshold (no raise).
       6. return Asset(contract_id, data=best.model_dump(), confidence=best_conf).
       """
   ```
7. Structured logging at each step; `__all__` exports.

**Tests:** `tests/unit/test_extraction.py` (orchestrator section) — LLM mocked
- `test_extract_asset_happy_path`: mock returns high confidence → single attempt; `Asset` has correct `contract_id`, populated `data`, `confidence`.
- `test_extract_asset_retries_until_threshold`: mock yields `[0.4, 0.7]` → 2 attempts; returns the 0.7 result.
- `test_extract_asset_exhausts_retries_returns_best`: mock yields `[0.3, 0.5, 0.4]` → 3 attempts; returns best (0.5); WARNING logged; no exception.
- `test_extract_asset_short_circuits_on_first_pass`: first attempt ≥ threshold → exactly one LLM call.
- `test_extract_asset_unknown_lv3_raises`: `ContractNotFoundError` propagates.
- `test_extract_asset_data_retains_none`: unpopulated fields present as `None` in `Asset.data`.

---

## Task Group 5: Package Exports

**Files:** `src/bulk_insert/blocks/__init__.py`

1. Re-export `extract_asset`, `build_dynamic_model`, `AssetExtractionError`.

---

## Task Group 6: Quality Gates & Manual Dry Run

1. `uv run ruff check . --fix && uv run ruff format .`
2. `uv run mypy src` — zero errors.
3. `uv run pytest tests/unit -v` — all pass (new + existing, no regressions).
4. Manual dry run: build the dynamic model from `162_restaurant.json`, print included/excluded field counts and a sample field's annotation.
5. (Optional) `@pytest.mark.integration` end-to-end with a real GPT-4o call gated behind `OPENROUTER_API_KEY`.
