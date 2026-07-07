"""Command-line harness for exercising the full pipeline end-to-end.

A thin developer tool (not part of the service contract) for running a real
document through ``process_document`` — Block 1 → 2 → 3 with live LLM calls —
and inspecting the structured result.  Useful for manual verification before the
Phase 7 HTTP layer exists.

Usage
-----
    # from a file
    uv run bulk-insert-cli --file sample.txt

    # inline text
    uv run bulk-insert-cli --text "Ristorante in centro con 40 coperti..."

    # from stdin
    cat sample.txt | uv run bulk-insert-cli

    # machine-readable output
    uv run bulk-insert-cli --file sample.txt --json

Requires ``OPENROUTER_API_KEY`` in the environment / ``.env`` (real LLM calls).
Exit code is ``0`` on a successful extraction, ``1`` on any structured failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from bulk_insert.config import settings
from bulk_insert.models.pipeline import PipelineResult
from bulk_insert.pipeline import process_document


def _read_text(args: argparse.Namespace) -> str:
    """Resolve the document text from --text, --file, or stdin (in that order)."""
    if args.text is not None:
        inline: str = args.text
        return inline
    if args.file is not None:
        path: Path = args.file
        return path.read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("No input. Provide --text, --file PATH, or pipe text via stdin. See --help.")


def _models_used() -> dict[str, str]:
    """The models the pipeline resolves from the process-level settings."""
    return {
        "lightweight": settings.MODEL_LIGHTWEIGHT,  # Block 1 scoring, Block 2 lv1
        "heavy": settings.MODEL_HEAVY,  # Block 3 extraction
        "embedding": settings.EMBEDDING_MODEL,  # Block 2 (lv2/lv3) voting
    }


def _print_human(result: PipelineResult, elapsed: float) -> None:
    """Pretty, colourless summary for terminal use."""
    models = _models_used()
    print(f"\n  id          : {result.id}")
    print(f"  elapsed     : {elapsed:.1f}s")
    print("  models      :")
    print(f"    lightweight : {models['lightweight']}   (Block 1 scoring, Block 2 lv1)")
    print(f"    heavy       : {models['heavy']}   (Block 3 extraction)")
    print(f"    embedding   : {models['embedding']}   (Block 2 voting)")
    if result.ok and result.asset is not None:
        asset = result.asset
        print("  status      : OK")
        print(f"  contract_id : {asset.contract_id}")
        print(f"  confidence  : {asset.confidence:.3f}")
        populated = {k: v for k, v in asset.data.items() if v is not None}
        print(f"  fields      : {len(populated)} populated / {len(asset.data)} total")
        print("\n  data (populated fields only):")
        print(json.dumps(populated, indent=2, ensure_ascii=False))
    else:
        err = result.error
        print("  status      : FAILED")
        if err is not None:
            print(f"  stage       : {err.stage}")
            print(f"  category    : {err.category}")
            print(f"  message     : {err.message}")
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bulk-insert-cli",
        description="Run a document through the full extraction pipeline.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text", help="Inline document text.")
    source.add_argument(
        "--file",
        type=Path,
        help="Path to a UTF-8 text file to process.",
    )
    parser.add_argument(
        "--id",
        dest="doc_id",
        default=None,
        help="Opaque document id (echoed in the result). Defaults to a timestamp.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full PipelineResult as JSON instead of a human summary.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable INFO-level pipeline logging on stderr.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.verbose:
        logging.basicConfig(
            level=logging.INFO,
            stream=sys.stderr,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    text = _read_text(args)
    doc_id = args.doc_id or f"cli-{int(time.time())}"

    if not settings.OPENROUTER_API_KEY:
        print(
            "WARNING: OPENROUTER_API_KEY is not set — live LLM calls will fail.",
            file=sys.stderr,
        )

    start = time.monotonic()
    result = process_document(doc_id, text)
    elapsed = time.monotonic() - start

    if args.json:
        # `_models` is CLI metadata alongside the PipelineResult fields (which
        # remain top-level, so `jq .confidence` etc. still work).
        payload = {**result.model_dump(), "_models": _models_used()}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_human(result, elapsed)

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
