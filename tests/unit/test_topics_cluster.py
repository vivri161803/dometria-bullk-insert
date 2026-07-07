"""Unit tests for embedding-based topic consolidation (Phase 6.8).

The real sentence-transformers model is mocked with a deterministic fake encoder so
the tests are fast and offline; they exercise the clustering, representative choice,
id remapping, and the hyperparameters read from ``Settings``.
"""

from __future__ import annotations

import numpy as np
import pytest

from bulk_insert.blocks.extraction import topics_cluster
from bulk_insert.config import settings


class _FakeModel:
    """Encoder returning a fixed vector per name (controls cosine geometry)."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    def encode(self, names: list[str], **_: object) -> np.ndarray:
        # **_ swallows the real encoder's kwargs (e.g. normalize_embeddings=True).
        return np.asarray([self._vectors[n] for n in names], dtype=np.float32)


@pytest.fixture
def patched_model(monkeypatch: pytest.MonkeyPatch):
    """Patch the module's embedding-model loader with a controllable fake."""

    def _install(vectors: dict[str, list[float]]) -> None:
        monkeypatch.setattr(topics_cluster, "_embedding_model", lambda _name: _FakeModel(vectors))

    return _install


def _cfg(threshold: float = 0.5, linkage: str = "average"):
    return settings.model_copy(
        update={
            "TOPIC_CLUSTER_THRESHOLD": threshold,
            "TOPIC_CLUSTER_LINKAGE": linkage,
        }
    )


def test_merges_near_duplicates_and_remaps_fields(patched_model) -> None:
    # "superfici" and "superfici bar" share a direction → one cluster; "prezzo" apart.
    patched_model({"superfici": [1.0, 0.0], "superfici bar": [1.0, 0.0], "prezzo": [0.0, 1.0]})
    topics = {"superfici": 1, "superfici bar": 2, "prezzo": 3}
    fields = {
        "a": {"topic": 1},
        "b": {"topic": 1},  # "superfici" used twice → wins as representative
        "c": {"topic": 2},  # "superfici bar" → same cluster as superfici
        "d": {"topic": 3},  # "prezzo" → its own cluster
    }

    new_fields, new_topics = topics_cluster.consolidate_topics(fields, topics, _cfg())
    assert len(new_topics) == 2
    # Most-used name is kept; the coupled duplicate is dropped as a name.
    assert "superfici" in new_topics and "superfici bar" not in new_topics
    # All three superfici-side fields land on the same cluster id...
    sup_id = new_topics["superfici"]
    assert new_fields["a"]["topic"] == sup_id
    assert new_fields["c"]["topic"] == sup_id  # remapped across the merge
    # ...and prezzo keeps its own id.
    assert new_fields["d"]["topic"] == new_topics["prezzo"]
    assert new_topics["prezzo"] != sup_id


def test_threshold_controls_granularity(patched_model) -> None:
    # Two near (cosine ~0.6) directions: merge at a low threshold, split at a high one.
    patched_model({"x": [1.0, 0.0], "y": [0.6, 0.8]})  # cosine(x, y) = 0.6
    topics = {"x": 1, "y": 2}
    fields = {"f": {"topic": 1}, "g": {"topic": 2}}

    _, merged = topics_cluster.consolidate_topics(fields, topics, _cfg(threshold=0.5))
    _, split = topics_cluster.consolidate_topics(fields, topics, _cfg(threshold=0.7))
    assert len(merged) == 1  # distance 0.4 ≤ 1 - 0.5
    assert len(split) == 2  # distance 0.4 > 1 - 0.7


def test_singleton_and_non_integer_topics_are_passthrough(patched_model) -> None:
    patched_model({"solo": [1.0, 0.0]})
    # Single topic → guard returns inputs unchanged (no model call needed).
    fields = {"a": {"topic": 1}, "b": {"topic": "unmapped-string"}}
    new_fields, new_topics = topics_cluster.consolidate_topics(fields, {"solo": 1}, _cfg())
    assert new_topics == {"solo": 1}
    assert new_fields["a"]["topic"] == 1
    assert new_fields["b"]["topic"] == "unmapped-string"  # non-int left untouched
