"""Unit tests for the offline ingest (Block 3 v2, Phase 6.0)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from bulk_insert.blocks.extraction.ingest import build_artifacts, validate_round_trip
from bulk_insert.config import settings

_REPO_CONTRACTS = settings.CONTRACTS_DIR
_REPO_ARTIFACTS = settings.ARTIFACTS_DIR

# A small, deliberately mixed subset (different lv1 roots) to prove N-agnosticism.
_SUBSET = ["162_restaurant.json", "234_villa.json"]


@pytest.fixture
def mini_build(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """Build artifacts from a 2-contract subset in an isolated temp dir."""
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    for name in _SUBSET:
        src = _REPO_CONTRACTS / name
        if not src.exists():  # pragma: no cover - guards a renamed fixture file
            pytest.skip(f"fixture contract {name} missing")
        shutil.copy(src, contracts / name)
    out = tmp_path / "artifacts"
    written = build_artifacts(contracts, out)
    return out, written


class TestBuildArtifacts:
    def test_is_n_agnostic(self, mini_build: tuple[Path, dict[str, Path]]) -> None:
        out, _ = mini_build
        manifests = json.loads((out / "product_manifests.json").read_text())["products"]
        assert set(manifests) == {"162_restaurant", "234_villa"}

    def test_all_four_artifacts_written(self, mini_build: tuple[Path, dict[str, Path]]) -> None:
        _, written = mini_build
        assert set(written) == {
            "field_registry.json",
            "field_tree.json",
            "product_manifests.json",
            "field_metadata.yaml",
        }
        for path in written.values():
            assert path.exists() and path.stat().st_size > 0

    def test_round_trip(self, mini_build: tuple[Path, dict[str, Path]]) -> None:
        out, _ = mini_build
        tree = json.loads((out / "field_tree.json").read_text())["tree"]
        manifests = json.loads((out / "product_manifests.json").read_text())["products"]
        # Must not raise.
        validate_round_trip(tree, manifests)

    def test_lv_path_from_ancestry(self, mini_build: tuple[Path, dict[str, Path]]) -> None:
        out, _ = mini_build
        manifests = json.loads((out / "product_manifests.json").read_text())["products"]
        assert manifests["162_restaurant"]["lv_path"] == [
            "commercial-activities",
            "food-beverage",
            "restaurant",
        ]
        assert manifests["234_villa"]["lv_path"] == [
            "real-estate",
            "prestige-residential-properties",
            "villa",
        ]

    def test_one_topic_per_field(self, mini_build: tuple[Path, dict[str, Path]]) -> None:
        out, _ = mini_build
        meta = yaml.safe_load((out / "field_metadata.yaml").read_text())
        for name, entry in meta["fields"].items():
            assert isinstance(entry["topic"], str) and entry["topic"], name

    def test_enum_status_seeding(self, mini_build: tuple[Path, dict[str, Path]]) -> None:
        out, _ = mini_build
        meta = yaml.safe_load((out / "field_metadata.yaml").read_text())["fields"]
        # A select/boolean field is a closed-vocab candidate (pending), numeric/text
        # fields are not_applicable.
        statuses = {
            name: entry["enum"]["status"]
            for name, entry in meta.items()
            if name in ("commercial_activities_zona", "address_line")
        }
        assert statuses["commercial_activities_zona"] == "pending"
        assert statuses["address_line"] == "not_applicable"
        # Pending enum candidate carries the contract options as seeded values.
        assert set(meta["commercial_activities_zona"]["enum"]["values"]) >= {"centro", "periferia"}

    def test_mandatory_seeded_from_is_required(
        self, mini_build: tuple[Path, dict[str, Path]]
    ) -> None:
        out, _ = mini_build
        meta = yaml.safe_load((out / "field_metadata.yaml").read_text())["fields"]
        assert meta["name"]["mandatory"] is True

    def test_todo_markers_present(self, mini_build: tuple[Path, dict[str, Path]]) -> None:
        out, _ = mini_build
        text = (out / "field_metadata.yaml").read_text()
        assert "TODO: llm-compile description" in text
        assert "TODO: llm-compile enum values" in text
        assert "TODO: human-confirm topic/priority" in text

    def test_empty_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            build_artifacts(tmp_path, tmp_path / "out")


class TestShippedArtifacts:
    """The artifacts committed with the repo satisfy the Phase-0 gates."""

    def test_shipped_round_trip(self) -> None:
        tree = json.loads((_REPO_ARTIFACTS / "field_tree.json").read_text())["tree"]
        manifests = json.loads((_REPO_ARTIFACTS / "product_manifests.json").read_text())["products"]
        assert len(manifests) == 97
        validate_round_trip(tree, manifests)


class TestRoundTripDetectsCorruption:
    def test_mismatch_raises(self) -> None:
        tree = {"lv1": {"lv2": {"lv3": [{"name": "a"}]}}}
        manifests = {"p": {"lv_path": ["lv1", "lv2", "lv3"], "field_keys": ["a", "b"]}}
        with pytest.raises(ValueError, match="Round-trip failed"):
            validate_round_trip(tree, manifests)
