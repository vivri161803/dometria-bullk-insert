"""Unit tests for the Block 3 contract loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bulk_insert.models.contract_loader import (
    ContractNotFoundError,
    contract_id_for,
    load_contract_schema,
)


def _write_contract(directory: Path, file_id: int, lv3: str, name: str) -> Path:
    payload = {
        "data": {
            "category": {"id": file_id, "level": 3, "name": name},
            "fields": {"native": [{"key": "name", "type": "text", "is_required": True}]},
        }
    }
    path = directory / f"{file_id}_{lv3}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestLoadContractSchema:
    """Tests for load_contract_schema()."""

    def test_returns_data_section(self, tmp_path: Path) -> None:
        _write_contract(tmp_path, 162, "restaurant", "Ristorante")
        data = load_contract_schema("restaurant", contracts_dir=tmp_path)
        assert data["category"]["name"] == "Ristorante"  # type: ignore[index]
        assert "native" in data["fields"]  # type: ignore[operator]

    def test_unknown_lv3_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ContractNotFoundError, match="No contract file"):
            load_contract_schema("does-not-exist", contracts_dir=tmp_path)

    def test_ambiguous_lv3_raises(self, tmp_path: Path) -> None:
        _write_contract(tmp_path, 162, "restaurant", "Ristorante")
        _write_contract(tmp_path, 999, "restaurant", "Ristorante Duplicate")
        with pytest.raises(ContractNotFoundError, match="Ambiguous"):
            load_contract_schema("restaurant", contracts_dir=tmp_path)


class TestContractIdFor:
    """Tests for contract_id_for()."""

    def test_returns_filename_stem(self, tmp_path: Path) -> None:
        _write_contract(tmp_path, 162, "restaurant", "Ristorante")
        assert contract_id_for("restaurant", contracts_dir=tmp_path) == "162_restaurant"


class TestRealContracts:
    """Tests against the real contracts/ directory shipped with the repo."""

    def test_real_restaurant_contract_loads(self) -> None:
        data = load_contract_schema("restaurant")
        assert data["category"]["name"] == "Ristorante"  # type: ignore[index]
        assert contract_id_for("restaurant") == "162_restaurant"
