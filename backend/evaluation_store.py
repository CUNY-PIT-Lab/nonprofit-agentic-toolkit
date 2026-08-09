"""Append-only storage for reviewer evaluation of canonical stage transcripts."""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event as sqlalchemy_event,
    func,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .evaluation import (
    AnnotationCategory,
    BUCKET_COLORS,
    ConversationSnapshot,
    EvaluationConflict,
    EvaluationError,
    EvaluationEvent,
    EvaluationEventType,
    EvaluationNotEligible,
    MAX_CUSTOM_BUCKETS,
    TranscriptTurn,
    checksum,
    reduce_events,
    validate_operation_id,
)
from .models import (
    AdoptionRecord,
    Base,
    ConversationTurn,
    Organization,
    StageState,
)
from .prompts import STAGE_LABELS


class EvaluationBucketRow(Base):
    __tablename__ = "evaluation_buckets"

    bucket_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    reviewer_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    operation_id: Mapped[str] = mapped_column(String(120), nullable=False)
    label: Mapped[str] = mapped_column(String(40), nullable=False)
    color_key: Mapped[str] = mapped_column(String(24), nullable=False)
    definition_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "reviewer_id", "operation_id", name="uq_evaluation_bucket_operation"
        ),
        CheckConstraint(
            "length(label) BETWEEN 1 AND 40", name="ck_evaluation_bucket_label"
        ),
        CheckConstraint(
            "color_key IN ('blue', 'green', 'violet', 'red')",
            name="ck_evaluation_bucket_color",
        ),
        Index("ix_evaluation_buckets_reviewer", "reviewer_id", "created_at"),
    )


class ConversationEvaluationEventRow(Base):
    __tablename__ = "conversation_evaluation_events"

    event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    stage_state_id: Mapped[str] = mapped_column(
        ForeignKey("stage_states.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    record_id: Mapped[str] = mapped_column(
        ForeignKey("adoption_records.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[str] = mapped_column(String(120), nullable=False)
    operation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewer_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    transcript_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket_id: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)
    # Keep the canonical turn identifier inside the hashed event, but do not
    # cascade deletion of one turn into a hole in a later event stream. The
    # append path validates existence and conversation ownership. Whole-record
    # deletion still cascades through stage_state_id and record_id.
    turn_id: Mapped[str | None] = mapped_column(String(36))
    annotation_category: Mapped[str | None] = mapped_column(String(24))
    previous_event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "stage_state_id",
            "reviewer_id",
            "evaluation_version",
            name="uq_conversation_evaluation_version",
        ),
        UniqueConstraint(
            "stage_state_id",
            "reviewer_id",
            "operation_id",
            name="uq_conversation_evaluation_operation",
        ),
        CheckConstraint("cycle_number >= 1", name="ck_conversation_evaluation_cycle"),
        CheckConstraint(
            "evaluation_version >= 1", name="ck_conversation_evaluation_version"
        ),
        CheckConstraint(
            "event_type IN ('placement_set', 'note_set', 'annotation_set', "
            "'annotation_removed')",
            name="ck_conversation_evaluation_type",
        ),
        CheckConstraint(
            "annotation_category IS NULL OR annotation_category IN "
            "('helpful', 'unclear', 'incorrect', 'unsafe', 'other')",
            name="ck_conversation_evaluation_annotation_category",
        ),
        CheckConstraint(
            "length(transcript_checksum) = 64 AND length(operation_fingerprint) = 64 "
            "AND length(event_hash) = 64 AND "
            "(previous_event_hash = '' OR length(previous_event_hash) = 64)",
            name="ck_conversation_evaluation_hashes",
        ),
        CheckConstraint(
            "(event_type = 'placement_set' AND note IS NULL AND turn_id IS NULL "
            "AND annotation_category IS NULL) OR "
            "(event_type = 'note_set' AND bucket_id IS NULL AND turn_id IS NULL "
            "AND annotation_category IS NULL AND (note IS NULL OR length(note) <= 1000)) OR "
            "(event_type = 'annotation_set' AND bucket_id IS NULL AND turn_id IS NOT NULL "
            "AND annotation_category IS NOT NULL AND (note IS NULL OR length(note) <= 500)) OR "
            "(event_type = 'annotation_removed' AND bucket_id IS NULL "
            "AND note IS NULL AND turn_id IS NOT NULL AND annotation_category IS NULL)",
            name="ck_conversation_evaluation_shape",
        ),
        Index(
            "ix_conversation_evaluation_stage_version",
            "stage_state_id",
            "reviewer_id",
            "evaluation_version",
        ),
        Index("ix_conversation_evaluation_org_time", "organization_id", "created_at"),
    )


def _deny_update(_mapper, _connection, target) -> None:
    raise EvaluationError(f"{type(target).__name__} rows are immutable")


def _deny_delete(_mapper, _connection, target) -> None:
    raise EvaluationError(f"{type(target).__name__} rows cannot be deleted")


for _immutable_model in (EvaluationBucketRow, ConversationEvaluationEventRow):
    sqlalchemy_event.listen(_immutable_model, "before_update", _deny_update)
    sqlalchemy_event.listen(_immutable_model, "before_delete", _deny_delete)


_SQLITE_WRITE_LOCK = threading.RLock()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _event_from_row(row: ConversationEvaluationEventRow) -> EvaluationEvent:
    event = EvaluationEvent(
        event_id=row.event_id,
        stage_state_id=row.stage_state_id,
        organization_id=row.organization_id,
        record_id=row.record_id,
        stage=row.stage,
        cycle_number=row.cycle_number,
        evaluation_version=row.evaluation_version,
        operation_id=row.operation_id,
        operation_fingerprint=row.operation_fingerprint,
        reviewer_id=row.reviewer_id,
        event_type=EvaluationEventType(row.event_type),
        transcript_checksum=row.transcript_checksum,
        bucket_id=row.bucket_id,
        note=row.note,
        turn_id=row.turn_id,
        annotation_category=(
            AnnotationCategory(row.annotation_category)
            if row.annotation_category
            else None
        ),
        previous_event_hash=row.previous_event_hash,
        created_at=_aware(row.created_at),
    )
    if event.event_hash != row.event_hash:
        raise EvaluationError("Persisted evaluation event hash is invalid")
    return event


def _bucket_dict(row: EvaluationBucketRow, sort_position: int) -> dict[str, Any]:
    return {
        "id": row.bucket_id,
        "label": row.label,
        "color_key": row.color_key,
        "standard_key": None,
        "sort_position": sort_position,
        "created_at": _aware(row.created_at).isoformat(),
    }


class EvaluationStore:
    """Persist reviewer labels without writing any canonical toolkit entity."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    @contextmanager
    def _write_lock(self, session: Session, key: str) -> Iterator[None]:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": key},
            )
            yield
        else:
            with _SQLITE_WRITE_LOCK:
                yield

    def custom_buckets(self, reviewer_id: str) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(EvaluationBucketRow)
                .where(EvaluationBucketRow.reviewer_id == reviewer_id)
                .order_by(EvaluationBucketRow.created_at, EvaluationBucketRow.bucket_id)
            ).all()
        return [_bucket_dict(row, 40 + index * 10) for index, row in enumerate(rows)]

    def create_bucket(
        self,
        *,
        reviewer_id: str,
        operation_id: str,
        label: str,
        color_key: str,
        created_at: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        operation_id = validate_operation_id(operation_id)
        label = label.strip()
        if not 1 <= len(label) <= 40:
            raise EvaluationError("Bucket labels must be 1-40 characters")
        if color_key not in BUCKET_COLORS:
            raise EvaluationError("Bucket color is not registered")
        definition_hash = checksum(
            {
                "reviewer_id": reviewer_id,
                "operation_id": operation_id,
                "label": label,
                "color_key": color_key,
            }
        )
        bucket_id = (
            "bucket."
            + hashlib.sha256(
                f"{reviewer_id}\x1f{operation_id}".encode("utf-8")
            ).hexdigest()
        )
        when = _aware(created_at or datetime.now(timezone.utc))
        with self.session_factory() as session, session.begin():
            with self._write_lock(session, f"evaluation-buckets:{reviewer_id}"):
                existing = session.scalar(
                    select(EvaluationBucketRow).where(
                        EvaluationBucketRow.reviewer_id == reviewer_id,
                        EvaluationBucketRow.operation_id == operation_id,
                    )
                )
                if existing:
                    if existing.definition_hash != definition_hash:
                        raise EvaluationConflict(
                            "operation_id already identifies a different bucket definition"
                        )
                    position = (
                        40
                        + list(
                            session.scalars(
                                select(EvaluationBucketRow)
                                .where(EvaluationBucketRow.reviewer_id == reviewer_id)
                                .order_by(
                                    EvaluationBucketRow.created_at,
                                    EvaluationBucketRow.bucket_id,
                                )
                            ).all()
                        ).index(existing)
                        * 10
                    )
                    return _bucket_dict(existing, position), False
                count = session.scalar(
                    select(func.count())
                    .select_from(EvaluationBucketRow)
                    .where(EvaluationBucketRow.reviewer_id == reviewer_id)
                )
                if int(count or 0) >= MAX_CUSTOM_BUCKETS:
                    raise EvaluationConflict(
                        f"A reviewer may define at most {MAX_CUSTOM_BUCKETS} custom buckets"
                    )
                row = EvaluationBucketRow(
                    bucket_id=bucket_id,
                    reviewer_id=reviewer_id,
                    operation_id=operation_id,
                    label=label,
                    color_key=color_key,
                    definition_hash=definition_hash,
                    created_at=when,
                )
                session.add(row)
                try:
                    session.flush()
                except IntegrityError as error:
                    raise EvaluationConflict(
                        "Bucket operation raced with another request"
                    ) from error
                return _bucket_dict(row, 40 + int(count or 0) * 10), True

    def validate_custom_bucket(self, reviewer_id: str, bucket_id: str) -> None:
        with self.session_factory() as session:
            row = session.get(EvaluationBucketRow, bucket_id)
            if not row or row.reviewer_id != reviewer_id:
                raise EvaluationError("Custom bucket is not available to this reviewer")

    def candidate_stage_state_ids(
        self, organization_ids: list[str], *, limit: int
    ) -> list[str]:
        if not organization_ids:
            return []
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(StageState.id)
                    .join(AdoptionRecord, AdoptionRecord.id == StageState.record_id)
                    .where(AdoptionRecord.organization_id.in_(organization_ids))
                    .order_by(StageState.updated_at.desc(), StageState.id)
                    .limit(limit)
                ).all()
            )

    def snapshot(
        self,
        stage_state_id: str,
        *,
        min_inactive_seconds: int,
        now: datetime | None = None,
    ) -> ConversationSnapshot:
        with self.session_factory() as session:
            row = session.execute(
                select(StageState, AdoptionRecord, Organization)
                .join(AdoptionRecord, AdoptionRecord.id == StageState.record_id)
                .join(Organization, Organization.id == AdoptionRecord.organization_id)
                .where(StageState.id == stage_state_id)
            ).one_or_none()
            if not row:
                raise EvaluationNotEligible(
                    "Conversation is not available for evaluation"
                )
            state, record, organization = row
            turn_rows = session.scalars(
                select(ConversationTurn)
                .where(
                    ConversationTurn.record_id == record.id,
                    ConversationTurn.stage == state.stage,
                    ConversationTurn.cycle_number == state.cycle_number,
                )
                .order_by(ConversationTurn.ordinal, ConversationTurn.id)
            ).all()
            try:
                snapshot = ConversationSnapshot(
                    stage_state_id=state.id,
                    organization_id=organization.id,
                    organization_name=organization.name,
                    record_id=record.id,
                    record_title=record.title,
                    stage=state.stage,
                    stage_label=STAGE_LABELS.get(
                        state.stage, state.stage.replace("_", " ").title()
                    ),
                    cycle_number=state.cycle_number,
                    turns=tuple(
                        TranscriptTurn(
                            turn_id=turn.id,
                            role=turn.role,
                            content=turn.content,
                            ordinal=turn.ordinal,
                            created_at=_aware(turn.created_at),
                        )
                        for turn in turn_rows
                    ),
                )
            except EvaluationError as error:
                raise EvaluationNotEligible(
                    "Conversation is not available for evaluation"
                ) from error
        current_time = _aware(now or datetime.now(timezone.utc))
        if snapshot.last_turn_at > current_time - timedelta(
            seconds=min_inactive_seconds
        ):
            raise EvaluationNotEligible("Conversation is not yet inactive")
        return snapshot

    def events(
        self,
        stage_state_id: str,
        reviewer_id: str,
        *,
        as_of_version: int | None = None,
    ) -> list[EvaluationEvent]:
        with self.session_factory() as session:
            statement = (
                select(ConversationEvaluationEventRow)
                .where(
                    ConversationEvaluationEventRow.stage_state_id == stage_state_id,
                    ConversationEvaluationEventRow.reviewer_id == reviewer_id,
                )
                .order_by(ConversationEvaluationEventRow.evaluation_version)
            )
            if as_of_version is not None:
                statement = statement.where(
                    ConversationEvaluationEventRow.evaluation_version <= as_of_version
                )
            return [_event_from_row(row) for row in session.scalars(statement).all()]

    def state(
        self,
        stage_state_id: str,
        reviewer_id: str,
        *,
        as_of_version: int | None = None,
    ) -> dict[str, Any]:
        return reduce_events(
            self.events(
                stage_state_id,
                reviewer_id,
                as_of_version=as_of_version,
            ),
            as_of_version=as_of_version,
        )

    def append_event(
        self,
        snapshot: ConversationSnapshot,
        *,
        reviewer_id: str,
        event_type: EvaluationEventType,
        operation_id: str,
        expected_version: int,
        expected_transcript_checksum: str,
        bucket_id: str | None = None,
        note: str | None = None,
        turn_id: str | None = None,
        annotation_category: AnnotationCategory | None = None,
        created_at: datetime | None = None,
    ) -> tuple[EvaluationEvent, dict[str, Any], bool]:
        operation_id = validate_operation_id(operation_id)
        request_fingerprint = checksum(
            {
                "stage_state_id": snapshot.stage_state_id,
                "reviewer_id": reviewer_id,
                "event_type": event_type.value,
                "operation_id": operation_id,
                "expected_version": expected_version,
                "expected_transcript_checksum": expected_transcript_checksum,
                "bucket_id": bucket_id,
                "note": note,
                "turn_id": turn_id,
                "annotation_category": (
                    annotation_category.value if annotation_category else None
                ),
            }
        )
        when = _aware(created_at or datetime.now(timezone.utc))
        with self.session_factory() as session, session.begin():
            with self._write_lock(
                session,
                f"evaluation:{snapshot.stage_state_id}:{reviewer_id}",
            ):
                rows = session.scalars(
                    select(ConversationEvaluationEventRow)
                    .where(
                        ConversationEvaluationEventRow.stage_state_id
                        == snapshot.stage_state_id,
                        ConversationEvaluationEventRow.reviewer_id == reviewer_id,
                    )
                    .order_by(ConversationEvaluationEventRow.evaluation_version)
                ).all()
                events = [_event_from_row(row) for row in rows]
                current = reduce_events(events)
                existing_row = next(
                    (row for row in rows if row.operation_id == operation_id), None
                )
                if existing_row:
                    if existing_row.operation_fingerprint != request_fingerprint:
                        raise EvaluationConflict(
                            "operation_id already identifies a different evaluation request",
                            current=current,
                        )
                    existing = _event_from_row(existing_row)
                    prior_result = reduce_events(
                        events, as_of_version=existing.evaluation_version
                    )
                    return existing, prior_result, False
                current_with_checksum = {
                    **current,
                    "transcript_checksum": snapshot.transcript_checksum,
                }
                if expected_transcript_checksum != snapshot.transcript_checksum:
                    raise EvaluationConflict(
                        "The canonical transcript changed; reload before reviewing",
                        current=current_with_checksum,
                    )
                if expected_version != current["evaluation_version"]:
                    raise EvaluationConflict(
                        "The evaluation advanced; reload before reviewing",
                        current=current_with_checksum,
                    )
                if turn_id and turn_id not in {turn.turn_id for turn in snapshot.turns}:
                    raise EvaluationError(
                        "Annotation turn does not belong to this conversation"
                    )
                evaluation_version = current["evaluation_version"] + 1
                event_id = (
                    "evaluation."
                    + hashlib.sha256(
                        (
                            f"{snapshot.stage_state_id}\x1f{reviewer_id}\x1f{operation_id}"
                        ).encode("utf-8")
                    ).hexdigest()
                )
                event = EvaluationEvent(
                    event_id=event_id,
                    stage_state_id=snapshot.stage_state_id,
                    organization_id=snapshot.organization_id,
                    record_id=snapshot.record_id,
                    stage=snapshot.stage,
                    cycle_number=snapshot.cycle_number,
                    evaluation_version=evaluation_version,
                    operation_id=operation_id,
                    operation_fingerprint=request_fingerprint,
                    reviewer_id=reviewer_id,
                    event_type=event_type,
                    transcript_checksum=snapshot.transcript_checksum,
                    bucket_id=bucket_id,
                    note=note,
                    turn_id=turn_id,
                    annotation_category=annotation_category,
                    previous_event_hash=current["event_hash"],
                    created_at=when,
                )
                session.add(
                    ConversationEvaluationEventRow(
                        event_id=event.event_id,
                        stage_state_id=event.stage_state_id,
                        organization_id=event.organization_id,
                        record_id=event.record_id,
                        stage=event.stage,
                        cycle_number=event.cycle_number,
                        evaluation_version=event.evaluation_version,
                        operation_id=event.operation_id,
                        operation_fingerprint=event.operation_fingerprint,
                        reviewer_id=event.reviewer_id,
                        event_type=event.event_type.value,
                        transcript_checksum=event.transcript_checksum,
                        bucket_id=event.bucket_id,
                        note=event.note,
                        turn_id=event.turn_id,
                        annotation_category=(
                            event.annotation_category.value
                            if event.annotation_category
                            else None
                        ),
                        previous_event_hash=event.previous_event_hash,
                        event_hash=event.event_hash,
                        created_at=event.created_at,
                    )
                )
                try:
                    session.flush()
                except IntegrityError as error:
                    raise EvaluationConflict(
                        "The evaluation advanced; reload before reviewing",
                        current=current_with_checksum,
                    ) from error
                return event, reduce_events([*events, event]), True


__all__ = [
    "ConversationEvaluationEventRow",
    "EvaluationBucketRow",
    "EvaluationStore",
]
