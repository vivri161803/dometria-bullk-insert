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


def _per_field_metrics(assets: list[Asset]) -> dict[str, dict[str, float]]:
    n = len(assets)
    keys: set[str] = set()
    for a in assets:
        keys.update(a.data)

    metrics: dict[str, dict[str, float]] = {}
    for key in sorted(keys):
        values = [a.data.get(key) for a in assets]
        counts = Counter(_hashable(v) for v in values)
        _, top = counts.most_common(1)[0]
        nulls = sum(1 for v in values if v is None)
        populated_without_quote = sum(
            1 for a in assets if a.data.get(key) is not None and key not in (a.evidence or {})
        )
        reviewed = sum(1 for a in assets if key in (a.review_queue or []))
        metrics[key] = {
            "agreement": top / n,
            "abstention": nulls / n,
            "hallucination": populated_without_quote / n,
            "review_rate": reviewed / n,
        }
    return metrics


def _summarise(assets: list[Asset]) -> dict[str, Any]:
    modal = _modal_contract(assets)
    agreeing = [a for a in assets if a.contract_id == modal]
    field_metrics = _per_field_metrics(agreeing)
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
        "runs_on_modal_contract": len(agreeing),
        "schema_stability": len(agreeing) / len(assets) if assets else 0.0,
        "field_level_agreement": mean_agreement,
        "mean_hallucination_rate": mean_hallucination,
        "fields": field_metrics,
    }


def _print_human(summary: dict[str, Any]) -> None:
    print(f"\nRuns: {summary['runs']}  modal contract: {summary['modal_contract']}")
    print(
        f"Schema stability: {summary['schema_stability']:.2%}  "
        f"({summary['runs_on_modal_contract']}/{summary['runs']} runs)"
    )
    print(f"Field-level agreement: {summary['field_level_agreement']:.2%}")
    print(f"Mean hallucination rate: {summary['mean_hallucination_rate']:.2%}\n")
    print(f"{'field':45s} {'agree':>7s} {'abstain':>8s} {'halluc':>7s} {'review':>7s}")
    for key, m in summary["fields"].items():
        print(
            f"{key[:45]:45s} {m['agreement']:>7.2f} {m['abstention']:>8.2f} "
            f"{m['hallucination']:>7.2f} {m['review_rate']:>7.2f}"
        )


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
