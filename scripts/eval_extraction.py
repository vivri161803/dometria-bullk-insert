"""Field-level eval / variance harness for the tree-driven extractor (Phase 6.7).

Complements ``scripts/measure_variance.py`` by focusing on **Block 3 v2** quality
inside a stable contract.  Runs the full pipeline on one document N times and, over
the runs that agree on the modal contract, reports per-field:

* **agreement** — modal-value fraction across runs (1.0 = perfectly reproducible);
* **abstention** — fraction of runs where the field was null (absent/ungrounded);
* **hallucination proxy** — fraction of runs where the field was populated *without*
  a verbatim evidence quote (should be ~0 given the quote validator);
* **review-rate** — fraction of runs that routed the field to ``review_queue``.

The aggregate **field-level agreement** is the mean per-field agreement — the
number to watch fall as the extractor becomes more reproducible.

It also reports the **chosen contract** (Block 2) with its run distribution, and a
two-column **extracted / unextracted** split of the product's fields (by modal value
over the runs that agree on the contract).

Diagnostic tool, not a test: it makes real LLM calls and needs
``OPENROUTER_API_KEY`` set (via ``.env`` or the environment).

Usage
-----
    uv run python scripts/eval_extraction.py --file sample.txt -n 10
    uv run python scripts/eval_extraction.py --file sample.txt -n 10 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from bulk_insert.models.asset import Asset
from bulk_insert.pipeline import process_document


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return ("__list__", tuple(value))
    if isinstance(value, dict):
        return ("__dict__", tuple(sorted(value.items())))
    return value


def _run_n(text: str, runs: int, doc_id: str) -> list[Asset]:
    assets: list[Asset] = []
    for i in range(1, runs + 1):
        result = process_document(f"{doc_id}-{i}", text)
        if result.ok and result.asset is not None:
            assets.append(result.asset)
            tag = f"{result.asset.contract_id} conf={result.asset.confidence:.2f}"
        else:
            err = result.error
            tag = "FAILED" if err is None else f"{err.stage}/{err.category}"
        print(f"  run {i}/{runs}: {tag}", file=sys.stderr)
    return assets


def _modal_contract(assets: list[Asset]) -> str | None:
    if not assets:
        return None
    return str(Counter(a.contract_id for a in assets).most_common(1)[0][0])


def _modal_value(assets: list[Asset], key: str) -> Any:
    """The most common (real) value of *key* across *assets*."""
    values = [a.data.get(key) for a in assets]
    top_hashable, _ = Counter(_hashable(v) for v in values).most_common(1)[0]
    for v in values:
        if _hashable(v) == top_hashable:
            return v
    return None


def _extracted_split(assets: list[Asset], field_keys: list[str]) -> tuple[list[str], list[str]]:
    """Split fields by whether their modal value across *assets* is populated.

    A field counts as **extracted** when its most common value over the runs is
    non-null; otherwise it is **unextracted** (the extractor abstains most often).
    """
    extracted: list[str] = []
    unextracted: list[str] = []
    for key in field_keys:
        (extracted if _modal_value(assets, key) is not None else unextracted).append(key)
    return extracted, unextracted


# Positive pass-marker flags that aren't problems and would only add noise to the
# flag breakdown (e.g. the grounding gate's "this field passed" marker).
_POSITIVE_FLAGS = {"grounding"}


def _per_field_metrics(assets: list[Asset]) -> dict[str, dict[str, Any]]:
    n = len(assets)
    keys: set[str] = set()
    for a in assets:
        keys.update(a.data)

    metrics: dict[str, dict[str, Any]] = {}
    for key in sorted(keys):
        values = [a.data.get(key) for a in assets]
        counts = Counter(_hashable(v) for v in values)
        top_hashable, top = counts.most_common(1)[0]
        nulls = sum(1 for v in values if v is None)
        populated_without_quote = sum(
            1 for a in assets if a.data.get(key) is not None and key not in (a.evidence or {})
        )
        reviewed = sum(1 for a in assets if key in (a.review_queue or []))

        # Modal value + the evidence quote from the run that produced it.
        modal_value: Any = None
        modal_quote: str | None = None
        for a in assets:
            if _hashable(a.data.get(key)) == top_hashable:
                modal_value = a.data.get(key)
                modal_quote = (a.evidence or {}).get(key)
                break

        # Per-flag-type tally across runs (ungrounded / voting / reconcile / ...).
        flag_counter: Counter[str] = Counter()
        for a in assets:
            field_flags = (a.flags or {}).get(key) or {}
            for ftype in field_flags:
                if ftype in _POSITIVE_FLAGS:
                    continue
                flag_counter[ftype] += 1

        metrics[key] = {
            "agreement": top / n,
            "abstention": nulls / n,
            "hallucination": populated_without_quote / n,
            "review_rate": reviewed / n,
            "modal_value": modal_value,
            "modal_quote": modal_quote,
            "flags": dict(flag_counter),
        }
    return metrics


def _summarise(assets: list[Asset]) -> dict[str, Any]:
    modal = _modal_contract(assets)
    agreeing = [a for a in assets if a.contract_id == modal]
    field_metrics = _per_field_metrics(agreeing)
    extracted, unextracted = _extracted_split(agreeing, list(field_metrics))
    mean_agreement = (
        sum(m["agreement"] for m in field_metrics.values()) / len(field_metrics)
        if field_metrics
        else 0.0
    )
    mean_hallucination = (
        sum(m["hallucination"] for m in field_metrics.values()) / len(field_metrics)
        if field_metrics
        else 0.0
    )
    return {
        "runs": len(assets),
        "modal_contract": modal,
        "contract_distribution": dict(Counter(a.contract_id for a in assets)),
        "runs_on_modal_contract": len(agreeing),
        "schema_stability": len(agreeing) / len(assets) if assets else 0.0,
        "field_level_agreement": mean_agreement,
        "mean_hallucination_rate": mean_hallucination,
        "extracted_fields": extracted,
        "unextracted_fields": unextracted,
        "fields": field_metrics,
    }


def _fmt_value(value: Any, width: int = 40) -> str:
    """Compact one-line rendering of a field's modal value for the table."""
    if value is None:
        return "-"
    text = str(value).replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _print_two_columns(left: list[str], right: list[str]) -> None:
    """Print two field lists side by side (extracted | unextracted)."""
    width = 45
    left_head = f"EXTRACTED ({len(left)})"
    right_head = f"UNEXTRACTED ({len(right)})"
    print(f"  {left_head:{width}s} | {right_head}")
    print(f"  {'-' * width} | {'-' * width}")
    for i in range(max(len(left), len(right))):
        lcell = left[i] if i < len(left) else ""
        rcell = right[i] if i < len(right) else ""
        print(f"  {lcell[:width]:{width}s} | {rcell[:width]}")


def _print_human(summary: dict[str, Any]) -> None:
    print(f"\nRuns: {summary['runs']}")

    # Block 2 — the contract chosen after preprocessing.
    print(f"\nChosen contract (Block 2): {summary['modal_contract']}")
    dist = summary["contract_distribution"]
    if len(dist) > 1:
        print("  contract distribution across runs:")
        for cid, count in sorted(dist.items(), key=lambda kv: -kv[1]):
            print(f"    {cid:40s} {count}/{summary['runs']}")
    print(
        f"  schema stability: {summary['schema_stability']:.2%}  "
        f"({summary['runs_on_modal_contract']}/{summary['runs']} runs)"
    )

    # Block 3 — which fields the extractor populated vs abstained on (modal value).
    print("\nExtracted vs unextracted fields (modal value over agreeing runs):")
    _print_two_columns(summary["extracted_fields"], summary["unextracted_fields"])

    print(f"\nField-level agreement: {summary['field_level_agreement']:.2%}")
    print(f"Mean hallucination rate: {summary['mean_hallucination_rate']:.2%}\n")
    fields: dict[str, dict[str, Any]] = summary["fields"]
    print(f"{'field':38s} {'agree':>6s} {'abstn':>6s} {'halluc':>6s} {'review':>6s}  modal value")
    for key, m in fields.items():
        print(
            f"{key[:38]:38s} {m['agreement']:>6.2f} {m['abstention']:>6.2f} "
            f"{m['hallucination']:>6.2f} {m['review_rate']:>6.2f}  {_fmt_value(m['modal_value'])}"
        )

    # Flag breakdown — per-flag-type counts across the agreeing runs.
    flagged = {k: m["flags"] for k, m in fields.items() if m["flags"]}
    if flagged:
        print("\nFlag breakdown (per-flag-type counts across agreeing runs):")
        for key, counts in flagged.items():
            parts = ", ".join(f"{ftype}={c}" for ftype, c in sorted(counts.items()))
            print(f"  {key:38s} {parts}")
    print("\n(evidence quotes per field available in --json output.)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="Path to a document to process.")
    source.add_argument("--text", type=str, help="Inline document text.")
    parser.add_argument("-n", "--runs", type=int, default=10, help="Number of runs.")
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout.")
    args = parser.parse_args()

    text = args.text if args.text is not None else args.file.read_text(encoding="utf-8")
    assets = _run_n(text, args.runs, doc_id="eval")
    summary = _summarise(assets)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_human(summary)


if __name__ == "__main__":
    main()
