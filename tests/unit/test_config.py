"""Unit tests for bulk_insert.config."""

import pytest

from bulk_insert.config import Settings


class TestSettingsDefaults:
    """Settings load correct defaults when no env vars are set."""

    def test_default_openrouter_base_url(self) -> None:
        s = Settings(_env_file=None)
        assert s.OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"

    def test_default_model_lightweight(self) -> None:
        s = Settings(_env_file=None)
        assert s.MODEL_LIGHTWEIGHT == "openai/gpt-4o-mini"

    def test_default_model_heavy(self) -> None:
        s = Settings(_env_file=None)
        assert s.MODEL_HEAVY == "openai/gpt-4o"

    def test_default_chunk_size(self) -> None:
        s = Settings(_env_file=None)
        assert s.CHUNK_SIZE == 6

    def test_default_chunk_overlap(self) -> None:
        s = Settings(_env_file=None)
        assert s.CHUNK_OVERLAP == 2

    def test_default_api_key_empty_string(self) -> None:
        # No API key in env — should default to empty string (not raise)
        s = Settings(_env_file=None)
        assert isinstance(s.OPENROUTER_API_KEY, str)


class TestSettingsFromEnv:
    """Settings pick up values from environment variables."""

    def test_api_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")
        s = Settings(_env_file=None)
        assert s.OPENROUTER_API_KEY == "sk-test-key"

    def test_model_lightweight_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODEL_LIGHTWEIGHT", "anthropic/claude-3-haiku")
        s = Settings(_env_file=None)
        assert s.MODEL_LIGHTWEIGHT == "anthropic/claude-3-haiku"

    def test_chunk_size_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHUNK_SIZE", "10")
        s = Settings(_env_file=None)
        assert s.CHUNK_SIZE == 10

    def test_chunk_overlap_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHUNK_OVERLAP", "3")
        s = Settings(_env_file=None)
        assert s.CHUNK_OVERLAP == 3

    def test_extra_env_vars_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_UNKNOWN_VAR", "whatever")
        # Should not raise — extra="ignore"
        s = Settings(_env_file=None)
        assert not hasattr(s, "SOME_UNKNOWN_VAR")


class TestSettingsSingleton:
    """The module-level `settings` singleton is importable."""

    def test_singleton_importable(self) -> None:
        from bulk_insert.config import settings

        assert isinstance(settings, Settings)


class TestBlock2Settings:
    """Tests for Block 2 (Contract Determination) settings."""

    def test_default_embedding_model(self) -> None:
        s = Settings(_env_file=None)
        assert s.EMBEDDING_MODEL == "paraphrase-multilingual-MiniLM-L12-v2"

    def test_default_contract_min_score(self) -> None:
        s = Settings(_env_file=None)
        assert s.CONTRACT_MIN_SCORE == 5

    def test_default_contract_top_k(self) -> None:
        s = Settings(_env_file=None)
        assert s.CONTRACT_TOP_K == 5

    def test_embedding_model_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        s = Settings(_env_file=None)
        assert s.EMBEDDING_MODEL == "all-MiniLM-L6-v2"

    def test_contract_min_score_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONTRACT_MIN_SCORE", "3")
        s = Settings(_env_file=None)
        assert s.CONTRACT_MIN_SCORE == 3

    def test_contract_top_k_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONTRACT_TOP_K", "10")
        s = Settings(_env_file=None)
        assert s.CONTRACT_TOP_K == 10


class TestBlock3Settings:
    """Tests for Block 3 (Asset Extraction) settings."""

    def test_default_contracts_dir_points_to_contracts_folder(self) -> None:
        s = Settings(_env_file=None)
        assert s.CONTRACTS_DIR.name == "contracts"
        assert s.CONTRACTS_DIR.is_absolute()

    def test_default_extraction_max_attempts(self) -> None:
        s = Settings(_env_file=None)
        assert s.EXTRACTION_MAX_ATTEMPTS == 3

    def test_default_extraction_min_confidence(self) -> None:
        s = Settings(_env_file=None)
        assert s.EXTRACTION_MIN_CONFIDENCE == 0.6

    def test_default_extraction_excluded_types(self) -> None:
        s = Settings(_env_file=None)
        assert s.EXTRACTION_EXCLUDED_TYPES == ("foreign_id", "decimal_pair")

    def test_extraction_min_confidence_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EXTRACTION_MIN_CONFIDENCE", "0.8")
        s = Settings(_env_file=None)
        assert s.EXTRACTION_MIN_CONFIDENCE == 0.8

    def test_extraction_max_attempts_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EXTRACTION_MAX_ATTEMPTS", "5")
        s = Settings(_env_file=None)
        assert s.EXTRACTION_MAX_ATTEMPTS == 5
