"""Unit tests for bulk_insert.config."""

import pytest
from bulk_insert.config import Settings


class TestSettingsDefaults:
    """Settings load correct defaults when no env vars are set."""

    def test_default_openrouter_base_url(self) -> None:
        s = Settings()
        assert s.OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"

    def test_default_model_lightweight(self) -> None:
        s = Settings()
        assert s.MODEL_LIGHTWEIGHT == "openai/gpt-4o-mini"

    def test_default_model_heavy(self) -> None:
        s = Settings()
        assert s.MODEL_HEAVY == "openai/gpt-4o"

    def test_default_redis_url(self) -> None:
        s = Settings()
        assert s.REDIS_URL == "redis://localhost:6379/0"

    def test_default_database_url(self) -> None:
        s = Settings()
        assert "dometria_bulk" in s.DATABASE_URL

    def test_default_chunk_size(self) -> None:
        s = Settings()
        assert s.CHUNK_SIZE == 6

    def test_default_chunk_overlap(self) -> None:
        s = Settings()
        assert s.CHUNK_OVERLAP == 2

    def test_default_api_key_empty_string(self) -> None:
        # No API key in env — should default to empty string (not raise)
        s = Settings()
        assert isinstance(s.OPENROUTER_API_KEY, str)


class TestSettingsFromEnv:
    """Settings pick up values from environment variables."""

    def test_api_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")
        s = Settings()
        assert s.OPENROUTER_API_KEY == "sk-test-key"

    def test_model_lightweight_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODEL_LIGHTWEIGHT", "anthropic/claude-3-haiku")
        s = Settings()
        assert s.MODEL_LIGHTWEIGHT == "anthropic/claude-3-haiku"

    def test_chunk_size_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHUNK_SIZE", "10")
        s = Settings()
        assert s.CHUNK_SIZE == 10

    def test_chunk_overlap_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHUNK_OVERLAP", "3")
        s = Settings()
        assert s.CHUNK_OVERLAP == 3

    def test_redis_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDIS_URL", "redis://myhost:6380/1")
        s = Settings()
        assert s.REDIS_URL == "redis://myhost:6380/1"

    def test_database_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        custom = "postgresql+asyncpg://user:pass@db:5432/mydb"
        monkeypatch.setenv("DATABASE_URL", custom)
        s = Settings()
        assert s.DATABASE_URL == custom

    def test_extra_env_vars_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_UNKNOWN_VAR", "whatever")
        # Should not raise — extra="ignore"
        s = Settings()
        assert not hasattr(s, "SOME_UNKNOWN_VAR")


class TestSettingsSingleton:
    """The module-level `settings` singleton is importable."""

    def test_singleton_importable(self) -> None:
        from bulk_insert.config import settings

        assert isinstance(settings, Settings)


class TestBlock2Settings:
    """Tests for Block 2 (Contract Determination) settings."""

    def test_default_embedding_model(self) -> None:
        s = Settings()
        assert s.EMBEDDING_MODEL == "paraphrase-multilingual-MiniLM-L12-v2"

    def test_default_contract_min_score(self) -> None:
        s = Settings()
        assert s.CONTRACT_MIN_SCORE == 5

    def test_default_contract_top_k(self) -> None:
        s = Settings()
        assert s.CONTRACT_TOP_K == 5

    def test_embedding_model_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        s = Settings()
        assert s.EMBEDDING_MODEL == "all-MiniLM-L6-v2"

    def test_contract_min_score_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONTRACT_MIN_SCORE", "3")
        s = Settings()
        assert s.CONTRACT_MIN_SCORE == 3

    def test_contract_top_k_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONTRACT_TOP_K", "10")
        s = Settings()
        assert s.CONTRACT_TOP_K == 10
