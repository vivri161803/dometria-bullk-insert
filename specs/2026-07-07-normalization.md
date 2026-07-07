# Normalization Step + Grounded Resolution Escape Hatches (Block 3, Phase 6.10)

> **Status:** proposed · **Date:** 2026-07-07 · **Extends:** the tree-driven extractor
> (`specs/2026-07-03-tree-driven-extraction.md`). Roadmap slot: **Phase 6.10**, after the
> offline compile phases (6.8/6.9) and before the FastAPI service (Phase 7).
>
> **Phase 6.10 bundles three companion features**, all improving how a per-request extracted
> record is finished before assembly:
>
> - **Part A — Normalization (§1–8).** A **runtime, pure-Python** pass that canonicalizes
>   raw LLM-extracted values (Italian numbers, ambiguous area units, dates, whitespace). No
>   LLM, no I/O, no clock — deterministic and reproducible.
> - **Part B — Grounded resolution escape hatches (§9–10).** Two **opt-in, LLM-in-the-loop**
>   resolvers for cases the closed-vocab extractor structurally cannot express, each bounded
>   by verbatim evidence (never the raw document):
>   - **§9 `OUT_OF_VOCAB`** — an enum long-tail escape hatch: *present but no listed value
>     fits* → a scoped judge infers the value from the field's own quote.
>   - **§10 count/aggregate** — *enumerated, not totaled* fields (e.g. bedrooms listed one by
>     one): transcribe the items as a grounded list, then `count` them in code.
>
> Part A is the deterministic core; Part B are separate features that reuse the derivation /
> judge machinery. They share this phase because both operate on the post-extraction record
> and were surfaced by the same `eval_extraction.py` run (a broken heating enum nulled every
> run; a mandatory bedroom count with no single verbatim span).

---

## 1. Why this exists — the "derive" layer has nothing to do, but values are still raw

Block 3 v2 (Phase 6) separates **transcription** (LLM), **deterministic derivation**
(Python arithmetic over sibling fields), and **judgment derivation** (2nd LLM run). An
analysis of the 97-contract catalog found that **essentially zero fields are valid
cross-field derivations**: every `*_totale`/`*_media`/`*_pct` field is a *directly stated*
value in the source document, not a function of its siblings, and declaring them as
`computed` would *overwrite* a correctly-transcribed number with a wrong guess (a computed
value wins Stage-7 reconcile). So the `derivation` layer stays correctly dormant.

What the catalog *does* have is **hundreds of single fields whose extracted value is
correct but not canonical**:

- Italian-formatted numbers — `"1.200,50 €"`, `"3,5 %"`, `"1.500 mq"` — extracted as
  strings or mis-typed numerics.
- **Ambiguous area units** — the 3 `*_mq_o_ha` fields, where `3` may mean 3 hectares or
  3 m² depending on the wording in the quote.
- Dates/years in mixed forms — `"gennaio 2020"`, `"01/2020"`, `"2019"`.
- Free text with stray/duplicated whitespace.

This is the *safe* form of "deterministic scripts" for this dataset: single-field
normalization **cleans** the extracted value in place. Unlike derivation it never
fabricates a value from other fields, so it cannot lower accuracy — the worst case is a
value it can't parse, which it leaves untouched and flags.

### 1.1 Scope of field types

| Normalized | `field_type` | Count | Action |
|---|---|---|---|
| ✅ | `currency`, `decimal`, `percentage` | 356 | Italian number parse → `float` (strip separators, symbols, `%`) |
| ✅ | `integer` | 192 | Italian number parse → `int` |
| ✅ | `year` | 7 | → 4-digit `int` |
| ✅ | `date` | 3 | → ISO `YYYY-MM-DD` |
| ✅ | `text` | 41 | trim + collapse internal whitespace (no case changes) |
| ⛔ skipped | `boolean`, `select` | 480 | already canonical (compiled enums) — left untouched |
| ⛔ n/a | `foreign_id`, `decimal_pair`, non-text geo | 5 | dropped from the schema upstream; never reach normalization |

The `*_mq_o_ha` fields are `decimal`/`currency`-typed but need a **special** normalizer
(unit resolution from the quote) selected via a per-field override, not by type.

---

## 2. Where it lands in the pipeline

**Between Extract and Derive.** The orchestrator calls `normalize_fields` immediately
after `extract_fields` and **before** `derive_computed`/reconcile:

```
Stage 4  extract_fields      → raw primitives + quotes
Stage 4b NORMALIZE (new)     → canonical numbers / dates / units   ← this spec
Stage 5  derive_computed     → arithmetic over canonical numbers
Stage 7  reconcile           → stated-vs-derived compared apples-to-apples
Stage 6  derive_judged
Stage 8  ground + assemble   → Asset
```

Placement rationale: any future `computed` rule and the reconcile cross-check must operate
on real numbers, not `"1.200,50"` strings. Normalizing first makes those layers correct by
construction. The public entrypoint `extract_asset(chunks, contract, cfg) -> Asset` and
`Asset`'s shape are **unchanged**; this is an internal stage.

---

## 3. Component design

### 3.1 Module — `blocks/extraction/normalize.py`

```python
def normalize_fields(
    result: ExtractionOutput,
    nodes: list[FieldNode],
    cfg: Settings | None = None,
) -> None:
    """Canonicalize extracted values in place (Stage 4b). Deterministic, no LLM/IO."""
```

Mirrors `derive.derive_computed`'s shape: mutates `result.values` in place and records
per-field `result.flags[name]` entries. Short-circuits entirely when
`cfg.NORMALIZE_ENABLED is False`.

For each node, the effective normalizer is chosen as:

1. the node's **sidecar override** (`FieldNode.normalization`), if present; else
2. the **type default** from the registry keyed by `field_type`; else
3. **no-op** (unknown/skipped types, enums, booleans).

Each normalizer is a **pure** function `(raw: Any, node: FieldNode, result, cfg) ->
tuple[Any, bool]` returning `(value, ok)`. `raw is None` (LLM abstained) is a no-op —
absence is not a normalization failure.

### 3.2 Normalizer registry

```python
NORMALIZERS: dict[str, Normalizer] = {
    "currency":   _norm_number,     # → float
    "decimal":    _norm_number,     # → float
    "percentage": _norm_percent,    # strip '%', → float
    "integer":    _norm_integer,    # → int
    "year":       _norm_year,       # → 4-digit int
    "date":       _norm_date,       # → ISO 'YYYY-MM-DD'
    "text":       _norm_text,       # trim + collapse whitespace
}
# override-only, never a type default:
OVERRIDE_NORMALIZERS = {"area_unit": _norm_area_unit}
```

- **`_norm_number`** — parse an Italian-formatted numeric string: drop currency symbols,
  units and spaces; treat `.` as thousands separator and `,` as the decimal separator
  (`"1.200,50"` → `1200.5`). Already-numeric input passes through. Returns `(float, True)`
  or `(raw, False)`.
- **`_norm_percent`** — `_norm_number` after stripping a trailing `%`. The value is the
  number as written (`"3,5 %"` → `3.5`), not a 0–1 fraction.
- **`_norm_integer`** — `_norm_number` then reject non-integral results (keep-raw+flag if
  `1200.5` where an int was expected).
- **`_norm_year`** — extract a 4-digit year in `[1800, 2100]`; `("2019","gen 2019")` → `2019`.
- **`_norm_date`** — parse common Italian forms (`"01/2020"`, `"gennaio 2020"`, `"2019"`,
  `"12/03/2021"`) to ISO; a bare year → `YYYY-01-01`. Deterministic month-name table.
- **`_norm_text`** — `re.sub(r"\s+", " ", raw).strip()`; **no** case folding.
- **`_norm_area_unit`** — read the unit token from the field's **evidence quote**
  (`result.quotes[node.name]`): a hectare token (`ha`, `ettar…`) converts via the existing
  `derive.COMPUTED_RULES["ha_to_mq"]`; an `mq`/`m²` token is already canonical. Numeric
  value first passes through `_norm_number`. Converts to `cfg.DEFAULT_AREA_UNIT` (`mq`). If
  no unit token is present in the quote, keep-raw + flag `unit_ambiguous`.

All numeric normalizers reuse the pure helpers already in `derive.py` where possible; the
`ha↔mq` factor lives once in the rule registry (option-b work), not duplicated here.

### 3.3 Failure handling — fail-safe, keep raw + flag

When a normalizer returns `ok=False`:

- `result.values[name]` is **left as the raw extracted value** (never nulled).
- `result.flags[name] = {"normalization": "unparsed", "field_type": ft, "raw": raw}`
  (or `"unit_ambiguous"` for `_norm_area_unit`).
- Never raises. Consistent with the pipeline's fail-safe-over-fail-fast rule.

When a value **is** rewritten, the original is retained for provenance:
`result.flags[name] = {"normalization": "ok", "raw": raw, "value": value}` (raw evidence
also stays in the paired `_quote`). Successful no-op passes (already canonical) record
nothing, keeping `flags` sparse.

### 3.4 Sidecar override — `field_metadata.yaml`

A new optional per-field key, `null`/absent by default (⇒ type default):

```yaml
holiday_village_superficie_complessiva_mq_o_ha:
  # …existing keys…
  normalization:
    normalizer: area_unit      # registry key in OVERRIDE_NORMALIZERS
    canonical: mq              # optional; falls back to DEFAULT_AREA_UNIT
```

- **Ingest** (`ingest.py`) seeds `normalization: null` for every field, and emits the
  `area_unit` stub for fields whose key matches `*_mq_o_ha` (the only known special case).
- **`metadata.py`** parses it (`_parse_normalization`) onto a new optional
  `FieldNode.normalization` (a small dataclass `Normalization{normalizer: str, canonical:
  str | None}`); `null`/absent ⇒ `None` ⇒ type default. String/legacy values fall back
  gracefully like the topic resolver does.

This is the only artifact/shape change, and it is additive — existing sidecars without the
key behave exactly as before.

---

## 4. Configuration (new `Settings` knobs)

| Setting | Default | Purpose |
|---|---|---|
| `NORMALIZE_ENABLED` | `True` | Global off-switch for the whole pass. |
| `DEFAULT_AREA_UNIT` | `"mq"` | Canonical unit for `area_unit` normalization. |
| `NORMALIZE_DECIMAL_LOCALE` | `"it"` | Number grammar (`it` = `.` thousands / `,` decimal). Reserved for future locales. |

Read per-call from `Settings` via the usual `cfg: Settings | None` override, never
hardcoded.

---

## 5. Data contract — unchanged wiring

- `ExtractionOutput.values` now holds canonical primitives (`float`/`int`/ISO-date/clean
  text) instead of raw strings; `quotes` are untouched (raw verbatim evidence).
- `Asset.data` / `evidence` / `flags` shapes are unchanged; `flags` may gain
  `{"normalization": …}` entries (additive, runtime-tolerated, already `dict[str, Any]`).
- `metadata.py` gains one optional field on `FieldNode`; `schema.py`, `extract.py`,
  `ground.py`, and the orchestrator's public signature are otherwise untouched.

---

## 6. Tests (`tests/unit/test_extraction.py` or `test_normalize.py`)

- **Number/currency:** `"1.200,50 €"` → `1200.5`; `"1.500 mq"` → `1500.0`; already-`float`
  passes through; `"abc"` → keep-raw + `unparsed` flag.
- **Percentage:** `"3,5 %"` → `3.5` (not `0.035`).
- **Integer:** `"1.200"` → `1200`; `"1.200,5"` → keep-raw + flag (non-integral).
- **Year:** `"gennaio 2019"` / `"2019"` → `2019`; `"19"` → keep-raw + flag.
- **Date:** `"01/2020"` → `"2020-01-01"`; `"gennaio 2020"` → `"2020-01-01"`; junk → flag.
- **Text:** `"  a   b \n c "` → `"a b c"`; no case change.
- **Area unit:** value `3` with quote `"3 ettari"` → `30000.0` (mq); quote `"320 mq"` →
  `320.0`; quote without a unit → keep-raw + `unit_ambiguous`.
- **Skips:** `boolean`/`select` values untouched; `None` value untouched.
- **Toggle:** `NORMALIZE_ENABLED = False` short-circuits (values unchanged).
- **Sidecar override:** `normalization: {normalizer: area_unit}` parsed onto `FieldNode`;
  absent ⇒ type default.
- **Placement:** a `computed` field consuming a normalized source sees the canonical
  number (integration of Stage 4b before Stage 5).
- Existing extract/derive/ground/compile tests stay green (additive stage).

---

## 7. Resolved decisions & non-goals

**Resolved:**
- **Runtime, not offline.** Runs per request inside Block 3 (unlike the 6.8/6.9 offline
  compiles). Adds **zero LLM cost** — pure Python.
- **Placement** → between Extract and Derive, so arithmetic/reconcile see canonical values.
- **Rule source** → type-driven default + optional per-field sidecar override.
- **On failure** → keep raw value + flag; never null, never raise.
- **Scope** → numeric/currency, mq/ha unit, date/year, text/whitespace. Enums/booleans
  skipped (already canonical).

**Non-goals (Part A — normalization core):**
- **No arithmetic cross-field derivation over stated siblings.** That layer stays dormant
  (no valid candidates); the normalization pass is strictly single-field. *(A `count`
  derivation over a transcribed companion list is introduced in §10 — a different case.)*
- **No LLM in the normalization pass.** Ambiguous units are resolved from the *quote
  string*, deterministically — never by asking a model. *(The Part B escape hatches in
  §9–10 do use the LLM, but they are separate, opt-in features, not the normalization core.)*
- **No unit inference for genuinely unit-less fields.** Only fields flagged `area_unit`
  (or a future override) get unit handling.
- **No aggressive text rewriting.** Whitespace only; no casing, spelling, or content
  changes to LLM-extracted phrasing.

---

## 8. Suggested module layout

```
src/bulk_insert/blocks/extraction/
  normalize.py    # NORMALIZERS registry + normalize_fields (Stage 4b)
  metadata.py     # + _parse_normalization → FieldNode.normalization
  ingest.py       # seed `normalization: null` (+ area_unit stub for *_mq_o_ha)
  orchestrator.py # call normalize_fields after extract, before derive_computed
config.py         # NORMALIZE_ENABLED, DEFAULT_AREA_UNIT, NORMALIZE_DECIMAL_LOCALE
tests/unit/       # test_normalize.py (or extend test_extraction.py)
```

Prefer stdlib + the existing `derive.py` numeric helpers; add nothing heavier. Declare an
explicit `__all__` and keep normalizers importable for unit testing, as the sibling
modules do.

---

# Part B — Grounded resolution escape hatches

Normalization (Part A) cleans values that were extracted correctly. Part B targets two
cases the closed-vocab, single-span extractor **structurally cannot express** — surfaced
directly by `eval_extraction.py` on `test.txt`:

- A field is **present in the text but no enum value fits** (`villa_impianto_riscaldamento`:
  the doc says *"Autonomo (Gas naturale)"*, but the compiled enum is
  `[Aria, Misto, Pavimento, Pompa di calore, Radiatori]`). Today the model picks the nearest
  wrong value, which the grounding gate nulls every run.
- A field is a **count of items enumerated one by one**, never totaled
  (`villa_numero_camere_da_letto`: 1 matrimoniale + master + seconda = 3, but no span says
  "3"). Today there is no verbatim quote for the number, so grounding nulls it — and it is
  **mandatory**, so it hits `review_queue` every run.

Both are the same underlying move: **an in-band signal from the extractor that a value
can't be expressed as one grounded primitive, plus a scoped, evidence-bounded resolver.**
Neither ever sees the raw document — resolution is bounded by verbatim quotes, preserving
the faithfulness guarantee.

Foundational fact both features rely on: **derived fields are excluded from the extraction
schema** (`schema.is_extractable` → `False` when `node.is_derived`), so the extraction LLM
abstains from them; computed fields are filled in code, judged fields by the isolated
`derive_judged` run over primitives + quotes only.

---

## 9. `OUT_OF_VOCAB` — enum long-tail resolution

### 9.1 Problem
A closed enum can never enumerate every phrasing real documents use. When the field **is**
present but out-of-vocabulary, the current design offers only `NOT_PRESENT` (semantically
*absent*) — so the model declines to abstain and picks the nearest wrong value, which the
Stage-8 entailment gate then nulls. The information is lost **silently**.

### 9.2 Design — a third enum sentinel
Add a reserved sentinel `OUT_OF_VOCAB` (parallel to `NOT_PRESENT`) to **every compiled
`select`/`boolean` enum** at schema-build time (`schema._annotation_for`). Semantics:

| Token | Meaning | Quote required? | Result |
|---|---|---|---|
| `NOT_PRESENT` | field absent | no (validator-exempt) | `None` |
| a real enum value | present & in vocab | yes | kept as-is (fast path) |
| **`OUT_OF_VOCAB`** | present, but no listed value fits | **yes** | scoped judge infers value from the quote |

- **No quote-validator change needed.** The validator (`schema.py:67`) exempts only `None`
  and `NOT_PRESENT`; `OUT_OF_VOCAB` is therefore **already** required to carry a verbatim
  quote. The model must attach the evidence span (e.g. *"Riscaldamento: Autonomo (Gas
  naturale)"*).
- **Prompt discipline.** `extract.py`'s system prompt gains one rule: *use `OUT_OF_VOCAB`
  only when the field is clearly present, none of the listed values apply, and you can quote
  the evidence.* This keeps the enum a fast path and reserves the escape hatch for the tail.

### 9.3 Resolution stage (new Stage 4c)
For each field whose extracted value is `OUT_OF_VOCAB`, run a **scoped judge** that reuses
`derive.derive_judged`'s isolated-LLM machinery, but with input = *the field description +
its own verbatim quote only* — **never** the raw document. Output is a **free-text** value
(`"Autonomo a gas"`). Optionally the judge first attempts to re-map to the closest enum
value and only emits free-text if none genuinely fits. Vote (`SELF_CONSISTENCY_N`) when the
field is mandatory/high-priority, as elsewhere.

Because the resolved value is inferred **from** the quote, it passes the Stage-8 entailment
gate by construction — the heating field goes from guaranteed-null → correct-and-grounded.

Placement: after normalization (Stage 4b) and before derive/ground. (Enum fields are
skipped by the normalizer, so 4b/4c ordering is independent.)

### 9.4 Telemetry — a free enum-quality signal
Each resolved field is flagged `{"resolution": "out_of_vocab", "inferred": value}`, and the
per-field **OOV rate** is added to `eval_extraction.py`. A field that hits `OUT_OF_VOCAB`
frequently is a loud signal that its compiled enum axis is wrong (exactly the heating case)
— feeding the **Phase 6.8** enum review *without anyone having to foresee the missing
value*. Today such enums fail silently at the grounding gate; here they announce themselves.

### 9.5 Caveats & config
- **Output type widens.** A `select` field may now return a non-enum free-text string.
  Downstream consumers must tolerate this (it is the honest value). The re-map-first option
  keeps most results inside the vocab.
- **Cost/variance are bounded.** One extra LLM call and a little paraphrase variance, but
  **only** for OOV-flagged fields (a small minority), and bounded by the quote.
- Config: `OOV_ENABLED` (default `True`), `OOV_TOKEN` (default `"OUT_OF_VOCAB"`),
  judge reuses `MODEL_HEAVY` and `SELF_CONSISTENCY_N`.

### 9.6 Tests
- `OUT_OF_VOCAB` present in every compiled enum `Literal` (incl. `NOT_PRESENT`).
- A value of `OUT_OF_VOCAB` **without** a quote fails the model validator (Instructor retry).
- Scoped judge sees only the field's quote/description, **not** the raw document (assert the
  document text is absent from the prompt, mirroring the existing `derive_judged` test).
- Resolved value survives the grounding gate (entailed by its quote).
- `OOV_ENABLED = False` leaves `OUT_OF_VOCAB` normalising to `None` (fall back to today's
  behaviour).
- OOV rate surfaced per field in the eval summary.

---

## 10. Count / aggregate fields — transcribe the list, `count` in code

### 10.1 Problem
`numero_di_X` fields where the document **enumerates** the items rather than stating a total
have no single verbatim span for the number, so the grounding gate nulls them — and several
are **mandatory** (bedrooms), routing to `review_queue` every run. Most `numero_X` fields
are *stated* (`Bagni: 2`, `Locali: 6`) and already extract fine; this targets only the
**enumerated-not-totaled** subset, opted in per field.

### 10.2 Design — transcribe then derive (the project's core principle, one level down)
Do **not** ask a judge to count. Instead:

1. **Transcribe the items as a grounded list.** For a field flagged as a count aggregate,
   `schema.build_extraction_model` synthesizes a **companion extracted field**
   `{field}__items: list[Item]`, each `Item` carrying a value + its verbatim quote. This is
   plain transcription with abstention — the model's comfort zone — and stays *in* the
   extraction schema.
2. **Count in code.** The target `numero_X` is declared **derived**
   (`derivation: {kind: computed, rule: count, source_fields: ["{field}__items"]}`), so it is
   **excluded from extraction** (per the foundational fact above) and filled by a new
   `count` rule in `derive.COMPUTED_RULES`: `count(list) → len(list)` (empty list → `0`;
   a `None`/absent companion → `None`). Provenance = the item quotes.

Result: the count is **deterministic** (zero variance), **grounded** (each item entailed by
its own quote), and **mandatory-satisfiable**. It is the same shape as `sum`, over a
transcribed companion list instead of sibling scalars.

### 10.3 Metadata & schema wiring
A per-field aggregate marker in `field_metadata.yaml` drives both the companion synthesis
and the derivation:

```yaml
villa_numero_camere_da_letto:
  # …existing keys…
  aggregate:
    rule: count                       # → COMPUTED_RULES["count"]
    item_hint: "una camera da letto"  # prompt context for the companion list
  derivation:
    kind: computed
    rule: count
    source_fields: ["villa_numero_camere_da_letto__items"]
```

- `schema.py` — when a node carries `aggregate`, synthesize the `__items` list field
  (grounded, quote-per-item) and **drop** the scalar from extraction (it is derived).
- `metadata.py` — parse `aggregate` onto the `FieldNode`; the companion name is
  conventional (`{name}__items`).
- The companion `__items` field is **internal**: it collapses to the count in `Asset.data`;
  its per-item quotes may be surfaced under `Asset.evidence` for provenance but the list
  itself is not a contract field.

### 10.4 Grounding & completeness
Per-item entailment runs at Stage 8 (each item vs its quote); the derived count is exempt
from the verbatim-quote validator like every computed field. A populated count clears the
mandatory-completeness check, so genuine enumerations no longer hit `review_queue`.

### 10.5 Scope & non-goals
- **Opt-in only.** Applied via the `aggregate` marker to the enumerated-not-totaled subset;
  stated `numero_X` fields stay plain extraction. No global behaviour change.
- **`count` only for v1.** Other aggregates (`sum`/`max` over a transcribed list) are
  possible later via the same companion mechanism, but out of scope here.

### 10.6 Tests
- `count` rule: `len` over a list; empty → `0`; `None`/missing companion → `None`.
- A node with `aggregate` synthesizes `{name}__items` (extracted, grounded) and excludes the
  scalar from the extraction model (it is derived).
- End-to-end: a doc enumerating 3 items yields count `3`, non-null, not in `review_queue`,
  with the 3 item quotes retained as provenance.
- A stated `numero_X` (no `aggregate`) is unchanged (plain extraction).

---

## 11. Delivery order within Phase 6.10

Part A (normalization) and Part B are independent and can ship in either order; suggested:

1. **Part A** — deterministic normalization (§1–8): lowest risk, no LLM, immediate cleanup.
2. **§9 `OUT_OF_VOCAB`** — highest diagnostic value (turns silent enum failures into a
   signal); reuses `derive_judged`.
3. **§10 count/aggregate** — most schema surgery (companion field synthesis); the new `count`
   rule is trivial, the schema wiring is the work.

Each is independently testable and gated behind its own `*_ENABLED` flag, so they can land
and be evaluated one at a time via `scripts/eval_extraction.py`.
