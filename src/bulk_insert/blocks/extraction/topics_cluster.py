"""Embedding-based consolidation of proposed compile topics (Phase 6.8).

The compile step proposes a topic name per field, batch by batch, with no global
view — so it emits many near-duplicates (``superfici`` / ``superfici agricole`` /
``superfici bar``) and product-coupled variants.  This module clusters those names
with the Block 2 multilingual embedding model and collapses each cluster to a
single representative id, remapping every field's ``topic`` to its cluster id.

Clustering is **agglomerative** over cosine distance (scikit-learn), which — unlike
a greedy nearest-representative pass — avoids a single generic seed swallowing loosely
related short words into one hub cluster.  The ``linkage`` and similarity ``threshold``
are the tunable hyperparameters.  It runs offline (no LLM) at the end of the compile,
so ``topics.yaml`` becomes a compact cluster dictionary that stays wired to
``field_metadata.yaml`` exactly as before (``metadata.py`` still resolves id → name).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.cluster import AgglomerativeClustering

from bulk_insert.config import Settings

__all__: list[str] = ["consolidate_topics"]

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _embedding_model(model_name: str) -> SentenceTransformer:
    """Load and cache the sentence-transformers model (once per process)."""
    logger.info("Loading embedding model for topic consolidation: '%s' ...", model_name)
    model: SentenceTransformer = SentenceTransformer(model_name)
    return model


def _topic_counts(
    fields_meta: dict[str, dict[str, Any]], id_to_name: dict[int, str]
) -> dict[str, int]:
    """Count how many fields use each topic name (via their integer topic id)."""
    counts: dict[str, int] = {}
    for meta in fields_meta.values():
        tid = meta.get("topic")
        name = id_to_name.get(tid) if isinstance(tid, int) else None
        if name is not None:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _cluster_labels(emb: np.ndarray, threshold: float, linkage: str) -> np.ndarray:
    """Agglomerative cluster labels over cosine distance.

    Two topics merge while their linkage distance stays ≤ ``1 - threshold``; a higher
    similarity *threshold* yields more, tighter clusters.  ``average`` linkage is a
    good default (resists both single-link chaining and complete-link fragmentation).
    """
    n = int(emb.shape[0])
    if n <= 1:
        return np.zeros(n, dtype=int)
    model = AgglomerativeClustering(
        n_clusters=None,
        metric="cosine",
        linkage=linkage,
        distance_threshold=1.0 - threshold,
    )
    return np.asarray(model.fit_predict(emb), dtype=int)


def consolidate_topics(
    fields_meta: dict[str, dict[str, Any]],
    topics: dict[str, int],
    cfg: Settings,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Cluster near-duplicate topics and remap field topic ids to cluster ids.

    Returns a ``(new_fields_meta, new_topics)`` pair.  ``new_topics`` maps each
    cluster's representative (most-used) name → a fresh cluster id; every field in
    ``new_fields_meta`` has its ``topic`` rewritten to its cluster id.  Fields whose
    ``topic`` is not an integer id present in *topics* are left untouched.

    Hyperparameters are read from ``cfg``: ``TOPIC_CLUSTER_THRESHOLD`` (cosine cutoff)
    and ``TOPIC_CLUSTER_LINKAGE`` (``average`` | ``complete`` | ``single``).
    """
    if len(topics) <= 1:
        return {n: dict(m) for n, m in fields_meta.items()}, dict(topics)

    names = list(topics.keys())
    id_to_name = {tid: name for name, tid in topics.items()}
    counts = _topic_counts(fields_meta, id_to_name)

    model = _embedding_model(cfg.EMBEDDING_MODEL)
    emb = np.asarray(model.encode(names, normalize_embeddings=True), dtype=np.float32)
    labels = _cluster_labels(emb, cfg.TOPIC_CLUSTER_THRESHOLD, cfg.TOPIC_CLUSTER_LINKAGE)

    # Group name indices by cluster label.
    clusters: dict[int, list[int]] = {}
    for i, lab in enumerate(labels):
        clusters.setdefault(int(lab), []).append(i)

    # Representative per cluster: most-used topic, ties by shorter then alphabetical.
    def _rep(members: list[int]) -> int:
        return min(members, key=lambda i: (-counts.get(names[i], 0), len(names[i]), names[i]))

    reps = {lab: _rep(members) for lab, members in clusters.items()}
    membership = {
        lab: sum(counts.get(names[i], 0) for i in members) for lab, members in clusters.items()
    }
    # Fresh ids: clusters ordered by total membership (desc), then representative name.
    ordered = sorted(clusters, key=lambda lab: (-membership[lab], names[reps[lab]]))
    lab_to_newid = {lab: new_id for new_id, lab in enumerate(ordered, start=1)}

    new_topics = {names[reps[lab]]: lab_to_newid[lab] for lab in ordered}
    oldid_to_newid = {topics[names[i]]: lab_to_newid[int(labels[i])] for i in range(len(names))}

    new_fields: dict[str, dict[str, Any]] = {}
    for fname, meta in fields_meta.items():
        m = dict(meta)
        tid = m.get("topic")
        if isinstance(tid, int) and tid in oldid_to_newid:
            m["topic"] = oldid_to_newid[tid]
        new_fields[fname] = m

    logger.info(
        "Topic consolidation: %d proposed → %d clusters (linkage=%s, threshold=%.2f).",
        len(names),
        len(new_topics),
        cfg.TOPIC_CLUSTER_LINKAGE,
        cfg.TOPIC_CLUSTER_THRESHOLD,
    )
    return new_fields, new_topics
