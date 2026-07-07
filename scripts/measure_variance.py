"""Measure run-to-run variance of the extraction pipeline on a single document.

Runs the *full* pipeline (Block 1 → 2 → 3) on the same input N times and reports
where the instability lives:

* **Schema-level** — does Block 2 pick the same contract every run?  If the
  chosen ``contract_id`` flips, the whole schema (and therefore the asset)
  changes; that dwarfs any field-level drift and is a Block 2 problem.
* **Field-level** — within the runs that agree on the modal contract, how often
  does each field produce the same value?  This isolates Block 3 value drift.

This is a diagnostic tool, not a test: it makes real LLM calls and needs
``OPENROUTER_API_KEY`` set (via ``.env`` or the environment).

Usage
-----
    uv run python scripts/measure_variance.py --file sample.txt -n 10
    uv run python scripts/measure_variance.py --file sample.txt -n 10 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from bulk_insert.pipeline import process_document


def _run_n(text: str, runs: int, doc_id: str) -> list[dict[str, Any]]:
    """Execute the pipeline *runs* times, collecting a compact record per run."""
    records: list[dict[str, Any]] = []
    for i in range(1, runs + 1):
        result = process_document(f"{doc_id}-{i}", text)
        if result.ok and result.asset is not None:
            record = {
                "ok": True,
                "contract_id": result.asset.contract_id,
                "confidence": result.asset.confidence,
                "data": result.asset.data,
            }
        else:
            err = result.error
            record = {
                "ok": False,
                "error": None if err is None else f"{err.stage}/{err.category}: {err.message}",
            }
        records.append(record)
        _progress(i, runs, record)
    return records


def _progress(i: int, runs: int, record: dict[str, Any]) -> None:
    """Print a one-line progress marker to stderr (keeps stdout clean for --json)."""
    if record["ok"]:
        tag = f"{record['contract_id']} (conf={record['confidence']:.2f})"
    else:
        tag = f"FAILED {record['error']}"
    print(f"  run {i}/{runs}: {tag}", file=sys.stderr)


def _stable_key(value: object) -> str:
    """Hashable, stable representation of a field value for grouping."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _field_agreement(group: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-field agreement across a group of runs sharing one contract.

    For each field key: the modal value's frequency / group size (1.0 == every
    run agreed), plus the distinct values seen. Union of keys across runs, so a
    field present in some runs but absent in others counts the absences.
    """
    n = len(group)
    keys: set[str] = set()
    for r in group:
        keys.update(r["data"].keys())

    report: dict[str, dict[str, Any]] = {}
    _MISSING = object()
    for key in sorted(keys):
        counter: Counter[str] = Counter()
        sample: dict[str, object] = {}
        for r in group:
            value = r["data"].get(key, _MISSING)
            sk = "<missing>" if value is _MISSING else _stable_key(value)
            counter[sk] += 1
            sample.setdefault(sk, value if value is not _MISSING else None)
        top_key, top_count = counter.most_common(1)[0]
        report[key] = {
            "agreement": top_count / n,
            "distinct": len(counter),
            "modal_value": sample[top_key],
        }
    return report


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-run records into the variance summary."""
    runs = len(records)
    ok = [r for r in records if r["ok"]]
    contract_counts = Counter(r["contract_id"] for r in ok)

    summary: dict[str, Any] = {
        "runs": runs,
        "succeeded": len(ok),
        "failed": runs - len(ok),
        "contract_distribution": dict(contract_counts),
        "schema_stable": len(contract_counts) <= 1,
    }

    if not contract_counts:
        summary["fields"] = {}
        summary["mean_field_agreement"] = None
        return summary

    modal_contract, modal_count = contract_counts.most_common(1)[0]
    group = [r for r in ok if r["contract_id"] == modal_contract]
    fields = _field_agreement(group)
    summary["modal_contract"] = modal_contract
    summary["modal_contract_runs"] = modal_count
    summary["fields"] = fields
    summary["mean_field_agreement"] = (
        sum(f["agreement"] for f in fields.values()) / len(fields) if fields else None
    )
    return summary


def _print_human(summary: dict[str, Any]) -> None:
    print("\n=== Variance report ===")
    print(
        f"runs: {summary['runs']}  succeeded: {summary['succeeded']}  failed: {summary['failed']}"
    )
    print("\nschema-level (contract chosen by Block 2):")
    for cid, count in summary["contract_distribution"].items():
        print(f"  {cid}: {count}")
    print("  -> STABLE" if summary["schema_stable"] else "  -> UNSTABLE (Block 2 flips contracts)")

    fields = summary.get("fields") or {}
    if not fields:
        print("\nno successful runs to compare fields.")
        return

    print(
        f"\nfield-level (within '{summary['modal_contract']}', "
        f"{summary['modal_contract_runs']} run(s)):"
    )
    mean = summary["mean_field_agreement"]
    print(f"  mean field agreement: {mean:.2%}" if mean is not None else "  (no fields)")
    unstable = {k: v for k, v in fields.items() if v["agreement"] < 1.0}
    if not unstable:
        print("  all fields fully stable.")
    else:
        print(f"  {len(unstable)} unstable field(s):")
        for key, info in sorted(unstable.items(), key=lambda kv: kv[1]["agreement"]):
            print(f"    {key:<40} agreement={info['agreement']:.2%}  distinct={info['distinct']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path, help="document to process")
    parser.add_argument("-n", "--runs", type=int, default=10, help="number of runs (default 10)")
    parser.add_argument("--doc-id", default="variance", help="base doc id")
    parser.add_argument("--json", action="store_true", help="emit JSON summary on stdout")
    args = parser.parse_args(argv)

    text = args.file.read_text(encoding="utf-8")
    print(f"running pipeline {args.runs}x on {args.file} ...", file=sys.stderr)
    records = _run_n(text, args.runs, args.doc_id)
    summary = summarise(records)

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    else:
        _print_human(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
