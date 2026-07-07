"""Offline build: contracts/ → static extraction artifacts.

Run once (or whenever the contracts change) to regenerate the field tree,
registry, product manifests and metadata sidecar consumed by Block 3 v2::

    uv run python scripts/build_artifacts.py
    uv run python scripts/build_artifacts.py --contracts contracts --out artifacts

The build is N-agnostic (never hardcodes the contract count) and enforces the
Phase-0 round-trip gate before writing anything.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from bulk_insert.blocks.extraction.ingest import build_artifacts
from bulk_insert.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contracts",
        type=Path,
        default=settings.CONTRACTS_DIR,
        help="Directory of {id}_{lv3}.json contract files.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=settings.ARTIFACTS_DIR,
        help="Output directory for the generated artifacts.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    written = build_artifacts(args.contracts, args.out)
    for name, path in written.items():
        print(f"wrote {name} -> {path}")


if __name__ == "__main__":
    main()
