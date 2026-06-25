# Validation — Block 3: Asset Extraction

> How to know the implementation succeeded and is ready to merge.

---

## Automated Checks (all MUST pass)

### 1. Static Analysis

| Check | Command | Expected |
|---|---|---|
| Linting | `uv run ruff check .` | 0 errors |
| Formatting | `uv run ruff format --check .` | 0 files would be reformatted |
| Type checking | `uv run mypy src` | Success: no issues found |

### 2. Unit Tests

| Command | Expected |
|---|---|
| `uv run pytest tests/unit -v` | All tests pass (existing + new) |

**Required test coverage (by focus area):**

| Test file | What it validates |
|---|---|
| `test_contract_loader.py` | Loads `data` section by lv3; `contract_id` = filename stem; unknown/ambiguous lv3 → `ContractNotFoundError` |
| `test_extraction.py` → `build_dynamic_model` | Type mapping (text/int/float); boolean & select → `Literal`; all fields optional/`None`; geo fields excluded, location text kept; `is_required` preserved as metadata |
| `test_extraction.py` → `extract_asset` | Happy path; retry-until-threshold; exhaust-and-return-best (no raise, WARNING logged); short-circuit on first pass; `ContractNotFoundError` propagation; `data` retains `None` |
| `test_config.py` (extended) | New settings (`CONTRACTS_DIR`, `EXTRACTION_MAX_ATTEMPTS`, `EXTRACTION_MIN_CONFIDENCE`, `EXTRACTION_EXCLUDED_TYPES`) load with correct defaults |

### 3. Existing Tests — Regression

| Command | Expected |
|---|---|
| `uv run pytest tests/unit/test_contract.py -v` | All pass (unchanged) |
| `uv run pytest tests/unit/test_models.py -v` | All pass |
| `uv run pytest tests/unit -v` | No regressions in Block 1 / Block 2 / Phase 1 tests |

### 4. Integration Test (optional, requires API key)

| Command | Expected |
|---|---|
| `uv run pytest tests/integration -v -m integration` | Passes with a valid `OPENROUTER_API_KEY` in `.env` |

Integration flow:
1. Construct chunks from a known Italian restaurant document.
2. Build `Contract(lv1="commercial-activities", lv2="food-beverage", lv3="restaurant")`.
3. Call `extract_asset()` with a real GPT-4o call.
4. Verify a valid `Asset` is returned, `data` keys match the dynamic model, `confidence ∈ [0, 1]`.

---

## Manual Verification Checklist

- [ ] **Contract loads by lv3**: `python -c "from bulk_insert.models.contract_loader import load_contract_schema; d = load_contract_schema('restaurant'); print(d['category']['name'], len(d['fields']['native']))"` — prints `Ristorante` and the native field count.

- [ ] **contract_id stem**: `python -c "from bulk_insert.models.contract_loader import contract_id_for; print(contract_id_for('restaurant'))"` — prints `162_restaurant`.

- [ ] **Dynamic model excludes geo fields**: build the model from `restaurant` schema and confirm `geo_municipality_id`, `municipality_id`, `latitude`, `longitude`, `country` are absent while `locality` and `address_line` are present.

- [ ] **Boolean → Literal**: confirm a boolean field's annotation is `Literal["si", "no", "non dichiarato"]`.

- [ ] **All-optional instantiation**: the dynamic model can be instantiated with no arguments; all fields are `None`.

- [ ] **Confidence loop (mocked)**: with a mock yielding ascending confidences, verify the loop stops at the threshold and returns the passing attempt; with all-low confidences, verify best-attempt return + WARNING and no exception.

---

## Merge Criteria

All of the following MUST be true before merging into `main`:

1. All automated checks pass (ruff, mypy, pytest unit tests).
2. No `# type: ignore` without an accompanying comment explaining why (expected only around `create_model` dynamic typing).
3. Existing Block 1 / Block 2 / Phase 1 tests still pass (no regressions).
4. `config.py` has all new settings with correct defaults, overridable via `.env`.
5. `extraction.py` and `contract_loader.py` define `__all__` / re-exports as specified.
6. Low confidence NEVER raises — `extract_asset` always returns the best `Asset` (fail-safe).
7. Field exclusion is driven by schema metadata (`type`/`source`), not hardcoded field keys.
8. Logging is present at each step (contract resolved, field counts, per-attempt confidence, final selection).

---

## Roadmap Exit Criteria (from `roadmap.md` Phase 4)

> Dati chunk + `Contract(lv1, lv2, lv3)`, carica lo schema corretto, produce un `Asset` valido che passa la validazione Pydantic.

✅ Met when `extract_asset(chunks, contract)` loads the correct contract by `lv3`, builds the dynamic model, and returns an `Asset` that passes `Asset` validation — verified by the unit suite above.
