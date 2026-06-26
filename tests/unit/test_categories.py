"""Unit tests for category helper functions (Block 2)."""

import pytest

from bulk_insert.models.categories import (
    CATEGORY_NAMES,
    CATEGORY_TREE,
    get_candidate_labels,
    get_candidate_paths,
)


class TestGetCandidatePaths:
    """Tests for get_candidate_paths()."""

    def test_commercial_activities(self) -> None:
        paths = get_candidate_paths("commercial-activities")
        # Sum all lv3 entries across all lv2 groups under commercial-activities
        expected_count = sum(len(lv3s) for lv3s in CATEGORY_TREE["commercial-activities"].values())
        assert len(paths) == expected_count
        assert expected_count >= 40  # sanity: should be ~49

    def test_businesses(self) -> None:
        paths = get_candidate_paths("businesses")
        # businesses has one lv2 (company-macro-sector) with 12 lv3
        assert len(paths) == 12
        # All paths should have lv2 = "company-macro-sector"
        assert all(lv2 == "company-macro-sector" for lv2, _ in paths)

    def test_real_estate(self) -> None:
        paths = get_candidate_paths("real-estate")
        # 3 lv2 groups
        lv2_codes = {lv2 for lv2, _ in paths}
        assert lv2_codes == {
            "specialist-destinations",
            "non-residential-real-estate",
            "prestige-residential-properties",
        }
        # Verify total count
        expected = sum(len(lv3s) for lv3s in CATEGORY_TREE["real-estate"].values())
        assert len(paths) == expected

    def test_invalid_lv1_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            get_candidate_paths("nonexistent-category")

    def test_all_paths_have_valid_codes(self) -> None:
        for lv1 in CATEGORY_TREE:
            for lv2, lv3 in get_candidate_paths(lv1):
                assert lv2 in CATEGORY_NAMES, f"lv2 code '{lv2}' not in CATEGORY_NAMES"
                assert lv3 in CATEGORY_NAMES, f"lv3 code '{lv3}' not in CATEGORY_NAMES"


class TestGetCandidateLabels:
    """Tests for get_candidate_labels()."""

    def test_label_format(self) -> None:
        labels = get_candidate_labels("commercial-activities")
        # Find the restaurant entry
        restaurant_entries = [
            (key, label) for key, label in labels if key == ("food-beverage", "restaurant")
        ]
        assert len(restaurant_entries) == 1
        _, label = restaurant_entries[0]
        assert label == "Food & Beverage > Ristorante"

    def test_label_count_matches_paths(self) -> None:
        for lv1 in CATEGORY_TREE:
            paths = get_candidate_paths(lv1)
            labels = get_candidate_labels(lv1)
            assert len(labels) == len(paths)

    def test_real_estate_first_three(self) -> None:
        labels = get_candidate_labels("real-estate")
        assert len(labels) >= 3
        # Each entry should be ((lv2, lv3), "Name > Name")
        for (lv2, lv3), label_text in labels[:3]:
            assert " > " in label_text
            lv2_name = CATEGORY_NAMES[lv2]
            lv3_name = CATEGORY_NAMES[lv3]
            assert label_text == f"{lv2_name} > {lv3_name}"

    def test_invalid_lv1_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            get_candidate_labels("invalid")
