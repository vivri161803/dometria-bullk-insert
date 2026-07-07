# Topic Consolidation — Embedding Clustering of Compiled Topics (Phase 6.9)

> **Status:** implemented · **Date:** 2026-07-07 · **Extends:** Phase 6.8 Offline
> Enum/Description Compile (`specs/roadmap.md`, and §8 "Enum-compile handoff" of
> `specs/2026-07-03-tree-driven-extraction.md`).
>
> A post-compile step that consolidates the model-proposed `topic` names into a small
> set of **embedding clusters**, so `artifacts/topics.yaml` is a compact cluster
> dictionary rather than one entry per raw proposal. Runs **offline** (no LLM) at the
> end of `scripts/compile_metadata.py`, immediately after `plan_compile` and before any
> file write.

---

## 1. Why this exists — topic fragmentation

Phase 6.8's compile assigns each field a short Italian `topic` name and dedupes those
names into `topics.yaml` by **exact string match** (`_allocate_topic`). The first full
compile over the field set produced **236 topics for 1083 fields** — far more than the
~30–40 genuinely distinct groupings present. Three causes:

1. **Batch-local naming.** The compile runs in batches of `COMPILE_BATCH_SIZE` (10),
   each an independent LLM call with no view of the others. A topic named `superfici`
   in one batch, `superfici agricole` in another, and `superfici bar` in a third never
   reconcile — the model cannot reuse a name it proposed elsewhere.
2. **Exact-string dedup only.** `_allocate_topic` collapses only byte-identical names,
   so singular/plural (`produzione`/`produzioni`), synonyms (`prezzo`/`costi`/`valore`),
   and near-paraphrases stay distinct ids.
3. **Product-coupled names.** Seeing a batch of one product's fields, the model prefixes
   the product into the topic — `servizi bar`, `superfici barracks`, `dotazioni
   gastronomia`, `superfici wine bar`. A topic is meant to be a **cross-product** axis
   (the product is already known from the contract), so this shatters one concept
   (`superfici`) across many ids.

`topic` is the partition key in `extract.partition_by_topic`; a fragmented, noisy
dictionary makes the grouping arbitrary and the sidecar hard to reason about.

## 2. Approach — offline agglomerative clustering

Consolidate the proposed topics **after** the compile, using the Block 2 multilingual
embedding model (`EMBEDDING_MODEL`, `paraphrase-multilingual-MiniLM-L12-v2`). No LLM
call is involved; the step is deterministic and cheap.

**Agglomerative clustering** (scikit-learn `AgglomerativeClustering`, `metric="cosine"`)
is used rather than a greedy nearest-representative pass. The greedy approach was tried
first and rejected: seeding clusters by the most-frequent topic let one generic short
word (`superfici`) act as a magnet and absorb loosely related terms (`ubicazione`,
`ricavi`, `ristorazione`) into a single hub cluster (56 members at threshold 0.6),
because this model has a high baseline cosine between short generic Italian nouns.
Agglomerative linkage merges by whole-cluster distance and avoids that hub effect;
`average` linkage is the default (resists both single-link chaining and complete-link
fragmentation).

## 3. Algorithm (`blocks/extraction/topics_cluster.py::consolidate_topics`)

Input: the compiled `fields_meta` (each field's `topic` is an integer id) and the
`topics` dict (`name → id`). Output: `(new_fields_meta, new_topics)`.

1. **Count usage** per topic name (how many fields carry its id) — drives representative
   choice and id ordering.
2. **Embed** the topic names with `normalize_embeddings=True` (unit vectors → cosine =
   dot product).
3. **Cluster** with `distance_threshold = 1 - TOPIC_CLUSTER_THRESHOLD` and
   `TOPIC_CLUSTER_LINKAGE`. (Guard: ≤1 topic → return inputs unchanged, no model load.)
4. **Representative per cluster** = the most-used member name; ties broken by shorter
   then alphabetically-earlier name (deterministic). So `superfici` (many fields) wins
   over `superfici bar`.
5. **Fresh ids** 1..K assigned to clusters ordered by total membership (desc), then
   representative name.
6. **Remap** every field's integer `topic` id to its cluster id; non-integer / unmapped
   topics are passed through untouched.

The clustering is **global** over all topics present in the plan, so it also folds in
topics loaded from a prior `topics.yaml` on incremental runs.

## 4. Configuration & CLI

Three `Settings` knobs (`config.py`), all overridable per-run from the compile CLI:

| Setting | Default | CLI flag | Purpose |
|---|---|---|---|
| `TOPIC_CLUSTER_ENABLED` | `True` | `--cluster` / `--no-cluster` | Toggle the consolidation step. |
| `TOPIC_CLUSTER_THRESHOLD` | `0.6` | `--cluster-threshold FLOAT` | Cosine-similarity merge cutoff; higher ⇒ more, tighter clusters. |
| `TOPIC_CLUSTER_LINKAGE` | `average` | `--cluster-linkage {average,complete,single}` | Agglomerative linkage. |

CLI overrides are applied onto a `settings.model_copy(update=...)` in
`scripts/compile_metadata.py`; when no flag is passed the config value is used. Examples:

```bash
uv run python scripts/compile_metadata.py --write                         # config defaults
uv run python scripts/compile_metadata.py --cluster-threshold 0.62 --write
uv run python scripts/compile_metadata.py --cluster-linkage complete --write
uv run python scripts/compile_metadata.py --no-cluster --write            # skip consolidation
```

## 5. Integration point

`scripts/compile_metadata.py::main`, after `plan_compile` and before the diff/write:

```python
if cfg.TOPIC_CLUSTER_ENABLED:
    new_fields, new_topics = consolidate_topics(plan.fields_meta, plan.topics, cfg)
    plan = replace(plan, fields_meta=new_fields, topics=new_topics)
```

The consolidation runs on **both** dry-run and `--write`, so the printed diff already
shows clustered ids. It is offline, so a dry-run remains free of LLM cost for this step.

## 6. Data contract — unchanged wiring

- `topics.yaml` still maps **name → integer id**; only now the name is a cluster
  representative and the set is compact. The file header no longer claims ids are stable
  across re-runs.
- `field_metadata.yaml`'s per-field `topic` is still an integer id, now pointing at a
  cluster.
- **`metadata.py` is untouched** — it resolves `topic` id → name exactly as before; the
  runtime does not know or care that the id denotes a cluster. `extract.partition_by_topic`
  is likewise unaffected.

## 7. Observed results

Sweep over the 236 proposed topics (offline, `average` linkage):

| Threshold | Clusters | Largest cluster |
|---|---|---|
| 0.55 | 50 | 56 |
| 0.62 | 74 | 25 |
| 0.68 | 95 | 17 |

Representative clusters at 0.62 (coherent, product-agnostic):
`[economico] ← fatturato, valore, reddito, dati economici, valutazione economica`;
`[struttura] ← caratteristiche edificio, superfici edifici, dotazioni edificio`;
`[operatività] ← gestione, management, strategia, amministrazione`;
`[servizi] ← servizi professionali, servizi officina, servizi annessi`.
Greedy nearest-representative at the same threshold produced a 56-member hub cluster
mixing `ubicazione`, `ricavi`, `ristorazione` — the reason agglomerative was chosen.

## 8. Resolved decisions & tradeoffs

- **Ids are derived, not stable.** Because clustering is global, ids can change if the
  topic set, embedding model, or threshold change. Treat `topics.yaml` ids as a build
  output, not hand-assigned. (This supersedes the Phase 6.8 note that re-runs keep
  stable ids.)
- **No manual name refinement.** By design the representative name is auto-selected
  (most-used member). The goal is an automatic, compact cluster dictionary, not a curated
  taxonomy.
- **Quality ceiling.** On very short generic Italian nouns the embedding model has
  limited resolution, so a few clusters remain loosely mixed. The `threshold`/`linkage`
  knobs are the levers; the default (`average`, `0.6`) is a reasonable starting point.
- **Not a controlled vocabulary.** A stricter future option is to classify each field
  into a fixed, curated topic set (like Block 2's constrained lv1) — deferred; this
  clustering is the automatic, zero-maintenance path requested.

## 9. Tests

`tests/unit/test_topics_cluster.py` (embedding model mocked with a deterministic fake
encoder, no download): near-duplicate merge + field remap; representative = most-used
name; `threshold` controls granularity (merge vs split at a known cosine); singleton
guard and non-integer topic passthrough. Existing `test_compile.py` (21 cases) stays
green — the compile core is unchanged; consolidation is a separate, additive step.
