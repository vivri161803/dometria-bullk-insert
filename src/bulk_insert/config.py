"""Application configuration for the Dometria Bulk Insert Pipeline.

All settings are loaded from environment variables and/or a `.env` file.
Swap LLM providers/models without code changes by editing `.env`.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Queue / Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/dometria_bulk"

    # Chunking parameters
    CHUNK_SIZE: int = 6  # sentences per chunk
    CHUNK_OVERLAP: int = 2  # overlapping sentences between consecutive chunks


settings = Settings()
