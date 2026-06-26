"""Unit tests for Pydantic data models."""

import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError

from bulk_insert.models import Asset, Chunk, ChunkAnalysis, ChunkScore, Contract, Job, JobStatus


class TestChunk:
    """Tests for the Chunk model."""

    def _make_chunk(self, **kwargs: object) -> Chunk:
        defaults: dict[str, object] = {
            "index": 0,
            "text": "Hello world.",
            "start_sentence": 0,
            "end_sentence": 0,
            "overlap_previous": 0,
            "is_coref_resolved": True,
        }
        defaults.update(kwargs)
        return Chunk(**defaults)  # type: ignore[arg-type]

    def test_basic_creation(self) -> None:
        chunk = self._make_chunk()
        assert chunk.index == 0
        assert chunk.text == "Hello world."
        assert chunk.relevance_score is None

    def test_relevance_score_valid(self) -> None:
        for score in range(1, 11):
            chunk = self._make_chunk(relevance_score=score)
            assert chunk.relevance_score == score

    def test_relevance_score_too_low(self) -> None:
        with pytest.raises(ValidationError):
            self._make_chunk(relevance_score=0)

    def test_relevance_score_too_high(self) -> None:
        with pytest.raises(ValidationError):
            self._make_chunk(relevance_score=11)

    def test_json_round_trip(self) -> None:
        chunk = self._make_chunk(relevance_score=7)
        reloaded = Chunk.model_validate_json(chunk.model_dump_json())
        assert reloaded == chunk

    def test_json_round_trip_no_score(self) -> None:
        chunk = self._make_chunk()
        reloaded = Chunk.model_validate_json(chunk.model_dump_json())
        assert reloaded.relevance_score is None


class TestChunkScore:
    """Tests for the ChunkScore response model."""

    def test_valid_score(self) -> None:
        cs = ChunkScore(score=5)
        assert cs.score == 5

    def test_boundary_low(self) -> None:
        cs = ChunkScore(score=1)
        assert cs.score == 1

    def test_boundary_high(self) -> None:
        cs = ChunkScore(score=10)
        assert cs.score == 10

    def test_score_zero_invalid(self) -> None:
        with pytest.raises(ValidationError):
            ChunkScore(score=0)

    def test_score_eleven_invalid(self) -> None:
        with pytest.raises(ValidationError):
            ChunkScore(score=11)

    def test_json_round_trip(self) -> None:
        cs = ChunkScore(score=8)
        reloaded = ChunkScore.model_validate_json(cs.model_dump_json())
        assert reloaded == cs


class TestChunkAnalysis:
    """Tests for the ChunkAnalysis combined response model."""

    def _make(self, score: int = 7, text: str = "Alice went to Alice's office.") -> ChunkAnalysis:
        return ChunkAnalysis(score=score, resolved_text=text)

    def test_basic_creation(self) -> None:
        a = self._make()
        assert a.score == 7
        assert "Alice" in a.resolved_text

    def test_score_too_low(self) -> None:
        with pytest.raises(ValidationError):
            ChunkAnalysis(score=0, resolved_text="x")

    def test_score_too_high(self) -> None:
        with pytest.raises(ValidationError):
            ChunkAnalysis(score=11, resolved_text="x")

    def test_score_boundaries_valid(self) -> None:
        ChunkAnalysis(score=1, resolved_text="low")
        ChunkAnalysis(score=10, resolved_text="high")

    def test_resolved_text_preserved(self) -> None:
        text = "John went to John's office. John called Maria."
        a = ChunkAnalysis(score=8, resolved_text=text)
        assert a.resolved_text == text

    def test_json_round_trip(self) -> None:
        a = self._make(score=5, text="Carol reviewed the lease.")
        reloaded = ChunkAnalysis.model_validate_json(a.model_dump_json())
        assert reloaded == a


class TestContract:
    """Tests for the Contract model."""

    def _make_contract(self) -> Contract:
        return Contract(
            lv1="Real Estate",
            lv2="Residential",
            lv3="Lease Agreement",
            schema={"type": "object", "properties": {"tenant": {"type": "string"}}},  # alias
        )

    def test_basic_creation(self) -> None:
        c = self._make_contract()
        assert c.lv1 == "Real Estate"
        assert c.lv3 == "Lease Agreement"

    def test_schema_is_dict(self) -> None:
        c = self._make_contract()
        assert isinstance(c.schema_def, dict)

    def test_json_round_trip(self) -> None:
        c = self._make_contract()
        reloaded = Contract.model_validate_json(c.model_dump_json(by_alias=True))
        assert reloaded == c

    def test_nested_schema(self) -> None:
        nested = {"type": "object", "properties": {"items": {"type": "array"}}}
        c = Contract(lv1="A", lv2="B", lv3="C", schema=nested)  # alias
        reloaded = Contract.model_validate_json(c.model_dump_json(by_alias=True))
        assert reloaded.schema_def == nested


class TestAsset:
    """Tests for the Asset model."""

    def _make_asset(self, **kwargs: object) -> Asset:
        defaults: dict[str, object] = {
            "contract_id": "contract-123",
            "data": {"tenant": "Alice"},
            "confidence": 0.95,
        }
        defaults.update(kwargs)
        return Asset(**defaults)  # type: ignore[arg-type]

    def test_basic_creation(self) -> None:
        a = self._make_asset()
        assert a.contract_id == "contract-123"
        assert a.confidence == 0.95

    def test_confidence_zero(self) -> None:
        a = self._make_asset(confidence=0.0)
        assert a.confidence == 0.0

    def test_confidence_one(self) -> None:
        a = self._make_asset(confidence=1.0)
        assert a.confidence == 1.0

    def test_confidence_below_zero_invalid(self) -> None:
        with pytest.raises(ValidationError):
            self._make_asset(confidence=-0.1)

    def test_confidence_above_one_invalid(self) -> None:
        with pytest.raises(ValidationError):
            self._make_asset(confidence=1.1)

    def test_json_round_trip(self) -> None:
        a = self._make_asset()
        reloaded = Asset.model_validate_json(a.model_dump_json())
        assert reloaded == a


class TestJobStatus:
    """Tests for the JobStatus enum."""

    def test_all_values_present(self) -> None:
        values = {s.value for s in JobStatus}
        assert values == {"PENDING", "PREPROCESSING", "CONTRACTING", "EXTRACTING", "DONE", "FAILED"}

    def test_valid_transition_pending_to_preprocessing(self) -> None:
        assert JobStatus.PENDING.can_transition_to(JobStatus.PREPROCESSING)

    def test_valid_transition_preprocessing_to_contracting(self) -> None:
        assert JobStatus.PREPROCESSING.can_transition_to(JobStatus.CONTRACTING)

    def test_valid_transition_contracting_to_extracting(self) -> None:
        assert JobStatus.CONTRACTING.can_transition_to(JobStatus.EXTRACTING)

    def test_valid_transition_extracting_to_done(self) -> None:
        assert JobStatus.EXTRACTING.can_transition_to(JobStatus.DONE)

    def test_any_state_can_go_to_failed(self) -> None:
        non_terminal = [
            JobStatus.PENDING,
            JobStatus.PREPROCESSING,
            JobStatus.CONTRACTING,
            JobStatus.EXTRACTING,
        ]
        for status in non_terminal:
            assert status.can_transition_to(JobStatus.FAILED), f"{status} should allow → FAILED"

    def test_done_has_no_transitions(self) -> None:
        assert not JobStatus.DONE.can_transition_to(JobStatus.FAILED)
        assert not JobStatus.DONE.can_transition_to(JobStatus.PENDING)

    def test_failed_has_no_transitions(self) -> None:
        assert not JobStatus.FAILED.can_transition_to(JobStatus.PENDING)
        assert not JobStatus.FAILED.can_transition_to(JobStatus.DONE)

    def test_invalid_transition_pending_to_done(self) -> None:
        assert not JobStatus.PENDING.can_transition_to(JobStatus.DONE)

    def test_invalid_transition_extracting_to_preprocessing(self) -> None:
        assert not JobStatus.EXTRACTING.can_transition_to(JobStatus.PREPROCESSING)


class TestJob:
    """Tests for the Job model."""

    def test_defaults(self) -> None:
        job = Job()
        assert isinstance(job.job_id, uuid.UUID)
        assert job.status == JobStatus.PENDING
        assert isinstance(job.created_at, datetime)
        assert isinstance(job.updated_at, datetime)
        assert job.block_results == {}

    def test_custom_job_id(self) -> None:
        uid = uuid.uuid4()
        job = Job(job_id=uid)
        assert job.job_id == uid

    def test_status_assignment(self) -> None:
        job = Job(status=JobStatus.PREPROCESSING)
        assert job.status == JobStatus.PREPROCESSING

    def test_block_results_stored(self) -> None:
        job = Job(block_results={"preprocessing": {"chunks": 5}})
        assert job.block_results["preprocessing"] == {"chunks": 5}

    def test_json_round_trip(self) -> None:
        job = Job(status=JobStatus.CONTRACTING, block_results={"x": 1})
        reloaded = Job.model_validate_json(job.model_dump_json())
        assert reloaded.job_id == job.job_id
        assert reloaded.status == job.status
        assert reloaded.block_results == job.block_results
