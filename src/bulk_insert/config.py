"""Application configuration for the Dometria Bulk Insert Pipeline.

All settings are loaded from environment variables and/or a `.env` file.
Swap LLM providers/models without code changes by editing `.env`.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root: src/bulk_insert/config.py -> parents[2] == repo root.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Pipeline-wide configuration, sourced from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM / OpenRouter
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

    # Model names — both must be OpenRouter-compatible slugs
    MODEL_LIGHTWEIGHT: str = "openai/gpt-4o-mini"
    MODEL_HEAVY: str = "openai/gpt-4o"

    # LLM sampling — applied to every LLM call (scoring, lv1, extraction).
    # Temperature 0.0 makes extraction near-deterministic (minimises run-to-run
    # variance in which fields/values are produced). Seed is best-effort
    # reproducibility (honoured only when the provider supports it).
    LLM_TEMPERATURE: float = 0.0
    LLM_SEED: int | None = None

    # OpenRouter provider routing — the dominant source of run-to-run variance.
    # The same model slug can be dispatched to different upstream providers with
    # different numerics, so temperature 0 / seed are not enough on their own.
    # `ORDER` pins the provider preference (e.g. ("openai",)); with
    # `ALLOW_FALLBACKS=False` requests stick to it instead of silently swapping.
    # `REQUIRE_PARAMETERS=True` only routes to providers that honour all sent
    # params (notably `seed`), so reproducibility requests are not dropped.
    # Sent as `extra_body={"provider": {...}}` on every LLM call.
    OPENROUTER_PROVIDER_ORDER: tuple[str, ...] = ()
    OPENROUTER_ALLOW_FALLBACKS: bool = False
    OPENROUTER_REQUIRE_PARAMETERS: bool = True

    # Queue / Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/dometria_bulk"

    # Chunking parameters
    CHUNK_SIZE: int = 6  # sentences per chunk
    CHUNK_OVERLAP: int = 2  # overlapping sentences between consecutive chunks

    # Block 2 — Contract Determination
    EMBEDDING_MODEL: str = "paraphrase-multilingual-MiniLM-L12-v2"
    CONTRACT_MIN_SCORE: int = 5  # minimum relevance_score to include a chunk
    CONTRACT_TOP_K: int = 5  # max top-scoring chunks to use for voting

    # Block 3 — Asset Extraction
    CONTRACTS_DIR: Path = _PROJECT_ROOT / "contracts"  # directory of {id}_{lv3}.json files
    EXTRACTION_MAX_ATTEMPTS: int = 3  # max extraction attempts for the confidence loop
    EXTRACTION_MIN_CONFIDENCE: float = 0.6  # threshold to short-circuit re-extraction
    # Field types the LLM cannot derive from free text — excluded from the dynamic model.
    EXTRACTION_EXCLUDED_TYPES: tuple[str, ...] = ("foreign_id", "decimal_pair")

    # Block 3 v2 — Tree-Driven Extraction (Phase 6)
    # Directory of offline-built static artifacts (field tree, registry, manifests).
    ARTIFACTS_DIR: Path = _PROJECT_ROOT / "artifacts"
    # Runtime-injected metadata sidecar (enums, descriptions, topics, derivations).
    # Editable YAML; overridable path. Relative paths resolve against ARTIFACTS_DIR.
    FIELD_METADATA_PATH: Path = _PROJECT_ROOT / "artifacts" / "field_metadata.yaml"
    # `priority` cutoff that, combined with `mandatory`, triggers self-consistency
    # voting for a topic group. Flag for human confirmation.
    HIGH_PRIORITY: int = 4
    # Number of votes cast per high-value group in the self-consistency tier (3–5).
    SELF_CONSISTENCY_N: int = 3
    # LLM-as-judge entailment gate cutoff; fields scoring below are flagged/dropped.
    JUDGE_THRESHOLD: float = 0.7

    # Phase 6.8 — Offline enum/description compile (metadata sidecar).
    # Model slug for the offline compile step; empty falls back to MODEL_HEAVY.
    MODEL_COMPILE: str = "anthropic/claude-haiku-4.5"
    # How many pending fields to pack into a single compile LLM call.
    COMPILE_BATCH_SIZE: int = 10
    # Topics dictionary (topic name -> integer id) produced by the compile step and
    # resolved back to a name at runtime by metadata.py. Editable YAML.
    TOPICS_PATH: Path = _PROJECT_ROOT / "artifacts" / "topics.yaml"
    # Post-compile topic consolidation: agglomerative clustering of near-duplicate
    # proposed topics with the Block 2 embedding model (offline, no LLM) so
    # topics.yaml is a compact cluster dictionary. THRESHOLD is the cosine-similarity
    # cutoff to merge topics — higher = stricter (more, tighter clusters). LINKAGE is
    # the agglomerative linkage: "average" (default), "complete", or "single".
    # All three are overridable per-run from the compile CLI.
    TOPIC_CLUSTER_ENABLED: bool = True
    TOPIC_CLUSTER_THRESHOLD: float = 0.6
    TOPIC_CLUSTER_LINKAGE: str = "average"


settings = Settings()
