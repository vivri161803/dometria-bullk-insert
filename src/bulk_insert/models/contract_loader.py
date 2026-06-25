"""Contract schema loading for Block 3 — Asset Extraction.

Each of the 97 contracts lives in :data:`Settings.CONTRACTS_DIR` under the
naming convention ``{id}_{lv3}.json`` (e.g. ``162_restaurant.json``).  This
module resolves a contract by its ``lv3`` code, returning the parsed ``data``
section (which holds ``category`` and ``fields``) and the contract identifier
(the filename stem, e.g. ``162_restaurant``).
"""

from __future__ import annotations

import json
from pathlib import Path

from bulk_insert.config import settings as _default_settings


class ContractNotFoundError(Exception):
    """Raised when no — or an ambiguous — contract file matches an lv3 code."""


def _resolve_contract_path(lv3: str, contracts_dir: Path | None = None) -> Path:
    """Locate the single ``*_{lv3}.json`` contract file for *lv3*.

    Parameters
    ----------
    lv3:
        The level-3 category code (e.g. ``"restaurant"``).
    contracts_dir:
        Directory to search.  Defaults to ``Settings.CONTRACTS_DIR``.

    Raises
    ------
    ContractNotFoundError
        If zero or more than one file matches the ``*_{lv3}.json`` pattern.
    """
    directory = contracts_dir or _default_settings.CONTRACTS_DIR
    matches = sorted(directory.glob(f"*_{lv3}.json"))

    if not matches:
        raise ContractNotFoundError(f"No contract file matching '*_{lv3}.json' in {directory}.")
    if len(matches) > 1:
        raise ContractNotFoundError(
            f"Ambiguous lv3 '{lv3}': {len(matches)} files match "
            f"'*_{lv3}.json' in {directory}: {[p.name for p in matches]}."
        )
    return matches[0]


def load_contract_schema(lv3: str, contracts_dir: Path | None = None) -> dict[str, object]:
    """Load the contract schema for *lv3* and return its ``data`` section.

    Parameters
    ----------
    lv3:
        The level-3 category code (e.g. ``"restaurant"``).
    contracts_dir:
        Directory to search.  Defaults to ``Settings.CONTRACTS_DIR``.

    Returns
    -------
    dict[str, object]
        The ``data`` object containing ``category`` and ``fields``.

    Raises
    ------
    ContractNotFoundError
        If no (or an ambiguous) contract file matches.
    """
    path = _resolve_contract_path(lv3, contracts_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    data: dict[str, object] = payload["data"]
    return data


def contract_id_for(lv3: str, contracts_dir: Path | None = None) -> str:
    """Return the contract identifier (filename stem) for *lv3*.

    Example: ``contract_id_for("restaurant") -> "162_restaurant"``.
    """
    return _resolve_contract_path(lv3, contracts_dir).stem


__all__: list[str] = [
    "ContractNotFoundError",
    "load_contract_schema",
    "contract_id_for",
]
