"""Replayable review events for saved guided-stage conversations.

The evaluation workspace is deliberately separate from pathway decisions,
fieldwork evidence, and product telemetry.  Reviewers may classify and annotate
an existing, immutable transcript; those actions never change the transcript or
the organization's canonical adoption decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping


class EvaluationError(ValueError):
    """Raised when evaluation history or a requested operation is invalid."""


class EvaluationConflict(EvaluationError):
    """Raised when optimistic concurrency or idempotency does not match."""

    def __init__(self, message: str, *, current: dict[str, Any] | None = None):
        super().__init__(message)
        self.current = current


class EvaluationNotEligible(EvaluationError):
    """Raised when a saved stage pass is not available for review."""


class EvaluationEventType(str, Enum):
    PLACEMENT_SET = "placement_set"
    NOTE_SET = "note_set"
    ANNOTATION_SET = "annotation_set"
    ANNOTATION_REMOVED = "annotation_removed"


class AnnotationCategory(str, Enum):
    HELPFUL = "helpful"
    UNCLEAR = "unclear"
    INCORRECT = "incorrect"
    UNSAFE = "unsafe"
    OTHER = "other"


STANDARD_BUCKETS: tuple[dict[str, str], ...] = (
    {
        "id": "success",
        "label": "Success",
        "color_key": "green",
        "standard_key": "success",
    },
    {
        "id": "needs",
        "label": "Needs work",
        "color_key": "red",
        "standard_key": "needs-work",
    },
    {
        "id": "handoff",
        "label": "Handoff",
        "color_key": "blue",
        "standard_key": "handoff",
    },
)
STANDARD_BUCKET_IDS = frozenset(item["id"] for item in STANDARD_BUCKETS)
BUCKET_COLORS = frozenset({"blue", "green", "violet", "red"})
MAX_CUSTOM_BUCKETS = 8

_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,119}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


def _aware_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EvaluationError("Evaluation timestamps must include a UTC offset")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def canonical_json(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, datetime):
        value = _aware_timestamp(value)
    if isinstance(value, Mapping):
        value = {
            str(key): json.loads(canonical_json(item))
            for key, item in sorted(value.items())
        }
    elif isinstance(value, (list, tuple)):
        value = [json.loads(canonical_json(item)) for item in value]
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise EvaluationError(f"Unsupported evaluation value: {type(value).__name__}")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_operation_id(value: str) -> str:
    value = value.strip()
    if not _OPERATION_ID.fullmatch(value):
        raise EvaluationError("operation_id must be a stable 8-120 character token")
    return value


@dataclass(frozen=True)
class TranscriptTurn:
    turn_id: str
    role: str
    content: str
    ordinal: int
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.turn_id,
            "role": self.role,
            "content": self.content,
            "ordinal": self.ordinal,
            "created_at": _aware_timestamp(self.created_at),
        }


@dataclass(frozen=True)
class ConversationSnapshot:
    stage_state_id: str
    organization_id: str
    organization_name: str
    record_id: str
    record_title: str
    stage: str
    stage_label: str
    cycle_number: int
    turns: tuple[TranscriptTurn, ...]
    transcript_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.stage_state_id or not self.record_id or self.cycle_number < 1:
            raise EvaluationError(
                "Conversation snapshots require stable stage identity"
            )
        if len({turn.turn_id for turn in self.turns}) != len(self.turns):
            raise EvaluationError("Conversation turn ids must be unique")
        expected_ordinals = list(range(1, len(self.turns) + 1))
        if [turn.ordinal for turn in self.turns] != expected_ordinals:
            raise EvaluationError(
                "Conversation turns must be complete and canonically ordered"
            )
        roles = {turn.role for turn in self.turns}
        if not {"user", "assistant"}.issubset(roles):
            raise EvaluationError(
                "Evaluation requires at least one user and assistant turn"
            )
        object.__setattr__(
            self,
            "transcript_checksum",
            checksum(
                {
                    "stage_state_id": self.stage_state_id,
                    "record_id": self.record_id,
                    "stage": self.stage,
                    "cycle_number": self.cycle_number,
                    "turns": [turn.as_dict() for turn in self.turns],
                }
            ),
        )

    @property
    def last_turn_at(self) -> datetime:
        return self.turns[-1].created_at

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.stage_state_id,
            "organization_id": self.organization_id,
            "organization_name": self.organization_name,
            "record_id": self.record_id,
            "record_title": self.record_title,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "cycle_number": self.cycle_number,
            "turn_count": len(self.turns),
            "last_turn_at": _aware_timestamp(self.last_turn_at),
            "transcript_checksum": self.transcript_checksum,
        }


@dataclass(frozen=True)
class EvaluationEvent:
    event_id: str
    stage_state_id: str
    organization_id: str
    record_id: str
    stage: str
    cycle_number: int
    evaluation_version: int
    operation_id: str
    operation_fingerprint: str
    reviewer_id: str
    event_type: EvaluationEventType
    transcript_checksum: str
    bucket_id: str | None
    note: str | None
    turn_id: str | None
    annotation_category: AnnotationCategory | None
    previous_event_hash: str
    created_at: datetime
    event_hash: str = field(init=False)

    def __post_init__(self) -> None:
        validate_operation_id(self.operation_id)
        if self.evaluation_version < 1 or self.cycle_number < 1:
            raise EvaluationError("Evaluation versions and cycles start at one")
        if not _HASH.fullmatch(self.transcript_checksum):
            raise EvaluationError("Evaluation events require a transcript checksum")
        if not _HASH.fullmatch(self.operation_fingerprint):
            raise EvaluationError("Evaluation events require an operation fingerprint")
        if self.previous_event_hash and not _HASH.fullmatch(self.previous_event_hash):
            raise EvaluationError("Evaluation history has an invalid prior hash")
        _aware_timestamp(self.created_at)
        self._validate_shape()
        object.__setattr__(
            self, "event_hash", checksum(self.as_dict(include_hash=False))
        )

    def _validate_shape(self) -> None:
        if self.event_type is EvaluationEventType.PLACEMENT_SET:
            if (
                self.note is not None
                or self.turn_id is not None
                or self.annotation_category is not None
            ):
                raise EvaluationError("Placement events contain only a bucket")
        elif self.event_type is EvaluationEventType.NOTE_SET:
            if (
                self.bucket_id is not None
                or self.turn_id is not None
                or self.annotation_category is not None
            ):
                raise EvaluationError("Note events contain only the review note")
            if self.note is not None and len(self.note) > 1_000:
                raise EvaluationError("Reviewer notes cannot exceed 1000 characters")
        elif self.event_type is EvaluationEventType.ANNOTATION_SET:
            if (
                self.bucket_id is not None
                or not self.turn_id
                or self.annotation_category is None
            ):
                raise EvaluationError("Annotation events require a turn and category")
            if self.note is not None and len(self.note) > 500:
                raise EvaluationError("Annotation notes cannot exceed 500 characters")
        elif self.event_type is EvaluationEventType.ANNOTATION_REMOVED:
            if (
                self.bucket_id is not None
                or not self.turn_id
                or self.note is not None
                or self.annotation_category is not None
            ):
                raise EvaluationError("Annotation removal contains only a turn id")

    def as_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {
            "event_id": self.event_id,
            "stage_state_id": self.stage_state_id,
            "organization_id": self.organization_id,
            "record_id": self.record_id,
            "stage": self.stage,
            "cycle_number": self.cycle_number,
            "evaluation_version": self.evaluation_version,
            "operation_id": self.operation_id,
            "operation_fingerprint": self.operation_fingerprint,
            "reviewer_id": self.reviewer_id,
            "event_type": self.event_type.value,
            "transcript_checksum": self.transcript_checksum,
            "bucket_id": self.bucket_id,
            "note": self.note,
            "turn_id": self.turn_id,
            "annotation_category": (
                self.annotation_category.value if self.annotation_category else None
            ),
            "previous_event_hash": self.previous_event_hash,
            "created_at": _aware_timestamp(self.created_at),
        }
        if include_hash:
            value["event_hash"] = self.event_hash
        return value


def reduce_events(
    events: Iterable[EvaluationEvent], *, as_of_version: int | None = None
) -> dict[str, Any]:
    bucket_id: str | None = None
    note: str | None = None
    annotations: dict[str, dict[str, Any]] = {}
    expected_version = 1
    prior_hash = ""
    last_checksum = ""
    history: list[dict[str, Any]] = []
    stream_identity: tuple[str, str, str, str, str, int] | None = None
    for event in events:
        if as_of_version is not None and event.evaluation_version > as_of_version:
            break
        if event.evaluation_version != expected_version:
            raise EvaluationError("Evaluation versions are not contiguous")
        identity = (
            event.stage_state_id,
            event.reviewer_id,
            event.organization_id,
            event.record_id,
            event.stage,
            event.cycle_number,
        )
        if stream_identity is None:
            stream_identity = identity
        elif stream_identity != identity:
            raise EvaluationError("Evaluation events cross conversation boundaries")
        if event.previous_event_hash != prior_hash:
            raise EvaluationError("Evaluation event hash chain is invalid")
        if checksum(event.as_dict(include_hash=False)) != event.event_hash:
            raise EvaluationError("Evaluation event content hash is invalid")
        if event.event_type is EvaluationEventType.PLACEMENT_SET:
            bucket_id = event.bucket_id
        elif event.event_type is EvaluationEventType.NOTE_SET:
            note = event.note
        elif event.event_type is EvaluationEventType.ANNOTATION_SET:
            annotations[event.turn_id or ""] = {
                "turn_id": event.turn_id,
                "category": (
                    event.annotation_category.value if event.annotation_category else ""
                ),
                "note": event.note,
                "version": event.evaluation_version,
            }
        elif event.event_type is EvaluationEventType.ANNOTATION_REMOVED:
            annotations.pop(event.turn_id or "", None)
        history.append(event.as_dict())
        prior_hash = event.event_hash
        last_checksum = event.transcript_checksum
        expected_version += 1
    return {
        "evaluation_version": expected_version - 1,
        "transcript_checksum": last_checksum,
        "bucket_id": bucket_id,
        "note": note,
        "annotations": list(annotations.values()),
        "history": history,
        "event_hash": prior_hash,
    }


__all__ = [
    "AnnotationCategory",
    "BUCKET_COLORS",
    "ConversationSnapshot",
    "EvaluationConflict",
    "EvaluationError",
    "EvaluationEvent",
    "EvaluationEventType",
    "EvaluationNotEligible",
    "MAX_CUSTOM_BUCKETS",
    "STANDARD_BUCKETS",
    "STANDARD_BUCKET_IDS",
    "TranscriptTurn",
    "canonical_json",
    "checksum",
    "reduce_events",
    "validate_operation_id",
]
