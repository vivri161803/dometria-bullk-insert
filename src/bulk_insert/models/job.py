"""Job state tracking Pydantic models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    """Valid pipeline states for a processing job.

    Allowed transitions::

        PENDING → PREPROCESSING → CONTRACTING → EXTRACTING → DONE
                         ↓               ↓             ↓        ↓
                       FAILED          FAILED        FAILED   FAILED

    ``FAILED`` is reachable from any non-terminal state.
    """

    PENDING = "PENDING"
    PREPROCESSING = "PREPROCESSING"
    CONTRACTING = "CONTRACTING"
    EXTRACTING = "EXTRACTING"
    DONE = "DONE"
    FAILED = "FAILED"

    def can_transition_to(self, next_status: JobStatus) -> bool:
        """Return ``True`` if a transition from *self* to *next_status* is valid."""
        allowed = _TRANSITIONS.get(self.value, [])
        return next_status.value in allowed


# Defined *after* the class so the enum members are not accidentally created.
_TRANSITIONS: dict[str, list[str]] = {
    "PENDING": ["PREPROCESSING", "FAILED"],
    "PREPROCESSING": ["CONTRACTING", "FAILED"],
    "CONTRACTING": ["EXTRACTING", "FAILED"],
    "EXTRACTING": ["DONE", "FAILED"],
    "DONE": [],
    "FAILED": [],
}


class Job(BaseModel):
    """Tracks the lifecycle of a single pipeline run.

    Fields
    ------
    job_id:
        Unique identifier (UUID4).
    status:
        Current pipeline state (see :class:`JobStatus`).
    created_at:
        UTC-aware timestamp of job creation.
    updated_at:
        UTC-aware timestamp of the most recent status change.
    block_results:
        Keyed by block name (e.g. ``"preprocessing"``, ``"contracting"``),
        stores the serialisable intermediate output of each completed block.
    """

    job_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    block_results: dict[str, object] = Field(default_factory=dict)
