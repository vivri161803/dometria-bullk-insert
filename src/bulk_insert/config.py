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


settings = Settings()
