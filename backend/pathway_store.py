"""Durable storage for immutable pathway definitions and transition journals."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

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
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .models import Base, JSON_DOCUMENT, uuid4str
from .pathways import (
    Approval,
    ApprovalStatus,
    FactStatus,
    PathwayDefinition,
    PathwayError,
    PathwayRun,
    RecordFact,
    RouteOutcome,
    RunStatus,
    TransitionDecision,
    UNGUIDED_CHECKPOINT_NODES,
    approved_gate_set,
    checksum,
    confirmed_fact_evidence,
    confirmed_fact_map,
    default_pathway,
)


class PathwayDefinitionRow(Base):
    __tablename__ = "pathway_versions"

    definition_checksum: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_key: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="approved")
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("family_key", "version", name="uq_pathway_family_version"),
        CheckConstraint("status IN ('draft', 'approved', 'retired')", name="ck_pathway_status"),
    )


class PathwayRunRow(Base):
    """Rebuildable current-state cache; transition rows remain the source of truth."""

    __tablename__ = "pathway_runs"

    record_id: Mapped[str] = mapped_column(
        ForeignKey("adoption_records.id", ondelete="CASCADE"), primary_key=True
    )
    definition_checksum: Mapped[str] = mapped_column(
        ForeignKey("pathway_versions.definition_checksum", ondelete="RESTRICT"),
        nullable=False,
    )
    family_key: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    current_node: Mapped[str] = mapped_column(String(80), nullable=False)
    entry_role: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("cycle_number >= 1", name="ck_pathway_run_cycle"),
        Index("ix_pathway_runs_definition", "definition_checksum", "status"),
    )


class PathwayFactRow(Base):
    __tablename__ = "pathway_facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    record_id: Mapped[str] = mapped_column(
        ForeignKey("adoption_records.id", ondelete="CASCADE"), nullable=False
    )
    fact_key: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[Any] = mapped_column(JSON_DOCUMENT, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source_event_ids: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(120), nullable=False)
    confirmed_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("pathway_facts.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected')", name="ck_pathway_fact_status"
        ),
        Index("ix_pathway_facts_record_key", "record_id", "fact_key", "created_at"),
    )


class PathwayApprovalRow(Base):
    __tablename__ = "pathway_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    record_id: Mapped[str] = mapped_column(
        ForeignKey("adoption_records.id", ondelete="CASCADE"), nullable=False
    )
    gate_key: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    subject_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("pathway_approvals.id", ondelete="RESTRICT")
    )
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('approved', 'rejected', 'changes_requested')",
            name="ck_pathway_approval_status",
        ),
        Index("ix_pathway_approvals_record_gate", "record_id", "gate_key", "decided_at"),
    )


class PathwayTransitionRow(Base):
    __tablename__ = "pathway_transitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    record_id: Mapped[str] = mapped_column(
        ForeignKey("adoption_records.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    edge_id: Mapped[str] = mapped_column(String(80), nullable=False)
    from_node: Mapped[str] = mapped_column(String(80), nullable=False)
    to_node: Mapped[str] = mapped_column(String(80), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    evidence_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    pathway_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("record_id", "sequence", name="uq_pathway_transition_sequence"),
        UniqueConstraint(
            "record_id", "idempotency_key", name="uq_pathway_transition_idempotency"
        ),
        CheckConstraint("sequence >= 1", name="ck_pathway_transition_sequence"),
        Index("ix_pathway_transitions_record_time", "record_id", "decided_at"),
    )


def _deny_update(_mapper, _connection, target) -> None:
    raise PathwayError(f"{type(target).__name__} rows are immutable")


def _deny_delete(_mapper, _connection, target) -> None:
    raise PathwayError(f"{type(target).__name__} rows cannot be deleted")


for _immutable_model in (
    PathwayDefinitionRow,
    PathwayFactRow,
    PathwayApprovalRow,
    PathwayTransitionRow,
):
    sqlalchemy_event.listen(_immutable_model, "before_update", _deny_update)
    sqlalchemy_event.listen(_immutable_model, "before_delete", _deny_delete)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _definition_from_row(row: PathwayDefinitionRow) -> PathwayDefinition:
    raw = row.definition
    definition = PathwayDefinition.build(
        family_key=raw["family_key"],
        version=int(raw["version"]),
        entry_node=raw["entry"],
        nodes=raw["nodes"],
        edges=raw["edges"],
    )
    if definition.definition_checksum != row.definition_checksum:
        raise PathwayError("Persisted pathway definition checksum is invalid")
    return definition


def _decision_from_row(row: PathwayTransitionRow) -> TransitionDecision:
    return TransitionDecision(
        sequence=row.sequence,
        edge_id=row.edge_id,
        from_node=row.from_node,
        to_node=row.to_node,
        outcome=RouteOutcome(row.outcome),
        actor_id=row.actor_id,
        rationale=row.rationale,
        evidence_json=json_dumps(row.evidence),
        evidence_checksum=row.evidence_checksum,
        pathway_checksum=row.pathway_checksum,
        previous_decision_hash=row.previous_decision_hash,
        decision_hash=row.decision_hash,
        decided_at=_aware(row.decided_at),
    )


def json_dumps(value: Any) -> str:
    from .pathways import canonical_json

    return canonical_json(value)


class PathwayStore:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def save_definition(
        self,
        definition: PathwayDefinition,
        *,
        actor_id: str,
        status: str = "approved",
        now: datetime | None = None,
    ) -> None:
        if status not in {"draft", "approved", "retired"}:
            raise PathwayError("Unknown pathway definition status")
        timestamp = now or datetime.now(timezone.utc)
        try:
            with self._session_factory() as session, session.begin():
                existing = session.get(PathwayDefinitionRow, definition.definition_checksum)
                if existing:
                    if existing.definition != definition.as_dict():
                        raise PathwayError("A pathway checksum cannot identify different content")
                    return
                session.add(
                    PathwayDefinitionRow(
                        definition_checksum=definition.definition_checksum,
                        family_key=definition.family_key,
                        version=definition.version,
                        definition=definition.as_dict(),
                        status=status,
                        created_by=actor_id,
                        approved_by=actor_id if status == "approved" else "",
                        created_at=timestamp,
                        approved_at=timestamp,
                    )
                )
        except IntegrityError as error:
            raise PathwayError("Pathway version already exists with different content") from error

    def start_run(
        self,
        record_id: str,
        definition: PathwayDefinition,
        *,
        entry_role: str,
        now: datetime | None = None,
    ) -> PathwayRun:
        timestamp = now or datetime.now(timezone.utc)
        run = PathwayRun.start(record_id, definition, entry_role=entry_role)
        with self._session_factory() as session, session.begin():
            if session.get(PathwayRunRow, record_id):
                raise PathwayError("This record already has a pinned pathway run")
            if not session.get(PathwayDefinitionRow, definition.definition_checksum):
                raise PathwayError("Persist the approved pathway definition first")
            session.add(
                PathwayRunRow(
                    record_id=record_id,
                    definition_checksum=definition.definition_checksum,
                    family_key=definition.family_key,
                    version=definition.version,
                    current_node=run.current_node,
                    entry_role=entry_role,
                    status=run.status.value,
                    cycle_number=run.cycle_number,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        return run

    def load_definition(self, checksum_value: str) -> PathwayDefinition:
        with self._session_factory() as session:
            row = session.get(PathwayDefinitionRow, checksum_value)
            if not row:
                raise PathwayError("Pinned pathway definition was not found")
            return _definition_from_row(row)

    def load_run(self, record_id: str) -> tuple[PathwayDefinition, PathwayRun]:
        with self._session_factory() as session:
            row = session.get(PathwayRunRow, record_id)
            if not row:
                raise PathwayError("Pathway run was not found")
            definition_row = session.get(PathwayDefinitionRow, row.definition_checksum)
            if not definition_row:
                raise PathwayError("Pinned pathway definition was not found")
            definition = _definition_from_row(definition_row)
            decisions = tuple(
                _decision_from_row(item)
                for item in session.scalars(
                    select(PathwayTransitionRow)
                    .where(PathwayTransitionRow.record_id == record_id)
                    .order_by(PathwayTransitionRow.sequence)
                )
            )
            stored = PathwayRun(
                record_id=record_id,
                pathway_family=row.family_key,
                pathway_version=row.version,
                pathway_checksum=row.definition_checksum,
                current_node=row.current_node,
                entry_role=row.entry_role,
                status=RunStatus(row.status),
                cycle_number=row.cycle_number,
                decisions=decisions,
            )
        replayed = stored.replay(definition)
        if (
            replayed.current_node != stored.current_node
            or replayed.status != stored.status
            or replayed.cycle_number != stored.cycle_number
        ):
            raise PathwayError("Pathway projection does not match its transition journal")
        return definition, stored

    def ensure_run(
        self,
        record_id: str,
        *,
        entry_role: str = "author",
        actor_id: str = "system",
        now: datetime | None = None,
    ) -> tuple[PathwayDefinition, PathwayRun]:
        try:
            return self.load_run(record_id)
        except PathwayError as error:
            if str(error) != "Pathway run was not found":
                raise
        definition = default_pathway()
        self.save_definition(definition, actor_id=actor_id, now=now)
        try:
            run = self.start_run(
                record_id, definition, entry_role=entry_role, now=now
            )
            return definition, run
        except PathwayError as error:
            if str(error) != "This record already has a pinned pathway run":
                raise
            return self.load_run(record_id)

    def current_evidence(
        self, record_id: str
    ) -> tuple[list[RecordFact], list[Approval]]:
        with self._session_factory() as session:
            if not session.get(PathwayRunRow, record_id):
                raise PathwayError("Pathway run was not found")
            return (
                self._latest_facts(session, record_id),
                self._latest_approvals(session, record_id),
            )

    def state(self, record_id: str) -> dict[str, Any]:
        definition, run = self.load_run(record_id)
        facts, approvals = self.current_evidence(record_id)
        edges = run.available_edges(definition, facts, approvals)
        return {
            "definition": definition.as_dict(),
            "run": {
                "record_id": run.record_id,
                "family_key": run.pathway_family,
                "version": run.pathway_version,
                "definition_checksum": run.pathway_checksum,
                "current_node": run.current_node,
                "entry_role": run.entry_role,
                "status": run.status.value,
                "cycle_number": run.cycle_number,
                "transition_count": len(run.decisions),
                "last_decision_hash": (
                    run.decisions[-1].decision_hash if run.decisions else ""
                ),
            },
            "confirmed_facts": confirmed_fact_map(facts),
            "confirmed_fact_evidence": confirmed_fact_evidence(facts),
            "approved_gates": sorted(approved_gate_set(approvals, facts)),
            "available_transitions": [edge.as_dict() for edge in edges],
            "decisions": [decision.as_dict() for decision in run.decisions],
        }

    def append_fact(
        self,
        record_id: str,
        fact: RecordFact,
        *,
        proposed_by: str,
        supersedes_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        row = PathwayFactRow(
            record_id=record_id,
            fact_key=fact.key,
            value=fact.value,
            status=fact.status.value,
            source_event_ids=list(fact.source_event_ids),
            proposed_by=proposed_by,
            confirmed_by=fact.confirmed_by,
            supersedes_id=supersedes_id,
            created_at=now or datetime.now(timezone.utc),
        )
        with self._session_factory() as session, session.begin():
            if not session.get(PathwayRunRow, record_id):
                raise PathwayError("Pathway run was not found")
            session.add(row)
            session.flush()
            return row.id

    def append_approval(
        self,
        record_id: str,
        approval: Approval,
        *,
        supersedes_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        row = PathwayApprovalRow(
            record_id=record_id,
            gate_key=approval.gate_key,
            status=approval.status.value,
            actor_id=approval.actor_id,
            subject_checksum=approval.subject_checksum,
            rationale=approval.rationale,
            supersedes_id=supersedes_id,
            decided_at=approval.decided_at,
            created_at=now or datetime.now(timezone.utc),
        )
        with self._session_factory() as session, session.begin():
            if not session.get(PathwayRunRow, record_id):
                raise PathwayError("Pathway run was not found")
            session.add(row)
            session.flush()
            return row.id

    @staticmethod
    def _latest_facts(session: Session, record_id: str) -> list[RecordFact]:
        rows = tuple(
            session.scalars(
                select(PathwayFactRow)
                .where(PathwayFactRow.record_id == record_id)
                .order_by(PathwayFactRow.created_at, PathwayFactRow.id)
            )
        )
        latest: dict[str, PathwayFactRow] = {row.fact_key: row for row in rows}
        return [
            RecordFact(
                key=row.fact_key,
                value=row.value,
                status=FactStatus(row.status),
                source_event_ids=tuple(row.source_event_ids),
                confirmed_by=row.confirmed_by,
            )
            for row in latest.values()
        ]

    @staticmethod
    def _latest_approvals(session: Session, record_id: str) -> list[Approval]:
        rows = tuple(
            session.scalars(
                select(PathwayApprovalRow)
                .where(PathwayApprovalRow.record_id == record_id)
                .order_by(PathwayApprovalRow.created_at, PathwayApprovalRow.id)
            )
        )
        latest: dict[str, PathwayApprovalRow] = {row.gate_key: row for row in rows}
        return [
            Approval(
                gate_key=row.gate_key,
                status=ApprovalStatus(row.status),
                actor_id=row.actor_id,
                subject_checksum=row.subject_checksum,
                decided_at=_aware(row.decided_at),
                rationale=row.rationale,
            )
            for row in latest.values()
        ]

    @staticmethod
    def _append_stage_readiness(
        session: Session,
        record_id: str,
        *,
        node: str,
        cycle_number: int,
        ready: bool,
        blocked: bool,
        source_event_id: str,
        actor_id: str,
        now: datetime,
    ) -> None:
        values = {
            "stage_ready": ready,
            "stage_ready_node": node,
            "stage_ready_cycle": cycle_number,
            "stage_blocked": blocked,
        }
        for key, value in values.items():
            session.add(
                PathwayFactRow(
                    record_id=record_id,
                    fact_key=key,
                    value=value,
                    status=FactStatus.CONFIRMED.value,
                    source_event_ids=[source_event_id],
                    proposed_by=actor_id,
                    confirmed_by=actor_id,
                    created_at=now,
                )
            )

    def ensure_stage_cycle_started(
        self,
        record_id: str,
        *,
        node: str,
        cycle_number: int,
        actor_id: str,
        now: datetime | None = None,
    ) -> None:
        """Invalidate prior-pass readiness when a new guided pass begins."""

        timestamp = now or datetime.now(timezone.utc)
        with self._session_factory() as session, session.begin():
            projection = session.get(PathwayRunRow, record_id)
            if not projection:
                raise PathwayError("Pathway run was not found")
            if (
                projection.current_node != node
                or projection.cycle_number != cycle_number
            ):
                raise PathwayError("Guided stage pass does not match the pinned pathway")
            current = confirmed_fact_map(self._latest_facts(session, record_id))
            if (
                current.get("stage_ready_node") == node
                and current.get("stage_ready_cycle") == cycle_number
            ):
                return
            self._append_stage_readiness(
                session,
                record_id,
                node=node,
                cycle_number=cycle_number,
                ready=False,
                blocked=False,
                source_event_id=f"stage-cycle:{node}:{cycle_number}",
                actor_id="system:pathway",
                now=timestamp,
            )

    def record_stage_completion(
        self,
        record_id: str,
        *,
        node: str,
        cycle_number: int,
        completion_id: str,
        actor_id: str,
        blocked: bool = False,
        now: datetime | None = None,
    ) -> None:
        """Bind readiness to one immutable completion in the current pass."""

        timestamp = now or datetime.now(timezone.utc)
        with self._session_factory() as session, session.begin():
            projection = session.get(PathwayRunRow, record_id)
            if not projection:
                raise PathwayError("Pathway run was not found")
            if (
                projection.current_node != node
                or projection.cycle_number != cycle_number
            ):
                raise PathwayError("Stage completion does not match the pinned pathway")
            latest = {
                fact.key: fact for fact in self._latest_facts(session, record_id)
            }
            if all(
                key in latest
                and latest[key].value == value
                and latest[key].source_event_ids == (completion_id,)
                for key, value in {
                    "stage_ready": not blocked,
                    "stage_ready_node": node,
                    "stage_ready_cycle": cycle_number,
                    "stage_blocked": blocked,
                }.items()
            ):
                return
            self._append_stage_readiness(
                session,
                record_id,
                node=node,
                cycle_number=cycle_number,
                ready=not blocked,
                blocked=blocked,
                source_event_id=completion_id,
                actor_id=actor_id,
                now=timestamp,
            )

    def record_stage_blocked(
        self,
        record_id: str,
        *,
        node: str,
        cycle_number: int,
        stage_state_id: str,
        now: datetime | None = None,
    ) -> None:
        """Fail closed when an unresolved guided-stage blocker is observed."""

        timestamp = now or datetime.now(timezone.utc)
        source_event_id = f"stage-state:{stage_state_id}"
        with self._session_factory() as session, session.begin():
            projection = session.get(PathwayRunRow, record_id)
            if not projection:
                raise PathwayError("Pathway run was not found")
            if (
                projection.current_node != node
                or projection.cycle_number != cycle_number
            ):
                raise PathwayError("Blocked stage does not match the pinned pathway")
            latest = {
                fact.key: fact for fact in self._latest_facts(session, record_id)
            }
            blocked = latest.get("stage_blocked")
            if (
                blocked is not None
                and blocked.value is True
                and blocked.source_event_ids == (source_event_id,)
            ):
                return
            self._append_stage_readiness(
                session,
                record_id,
                node=node,
                cycle_number=cycle_number,
                ready=False,
                blocked=True,
                source_event_id=source_event_id,
                actor_id="system:pathway",
                now=timestamp,
            )

    def confirm_unguided_checkpoint(
        self,
        record_id: str,
        *,
        node: str,
        cycle_number: int,
        actor_id: str,
        rationale: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> tuple[str, bool]:
        """Confirm one bounded, server-owned readiness checkpoint."""

        definition, run = self.load_run(record_id)
        if node not in UNGUIDED_CHECKPOINT_NODES:
            raise PathwayError("This node does not support an unguided checkpoint")
        if not any(
            edge.from_node == node and edge.outcome is RouteOutcome.PROCEED
            for edge in definition.edges
        ):
            raise PathwayError("This node does not have a Proceed checkpoint")
        timestamp = now or datetime.now(timezone.utc)
        request_evidence = {
            "record_id": record_id,
            "node": node,
            "cycle_number": cycle_number,
            "actor_id": actor_id,
            "rationale": rationale,
            "idempotency_key": idempotency_key,
        }
        checkpoint_id = f"unguided-checkpoint:{checksum(request_evidence)}"
        values = {
            "stage_ready": True,
            "stage_ready_node": node,
            "stage_ready_cycle": cycle_number,
            "stage_blocked": False,
        }
        fact_ids = {
            key: str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{record_id}:unguided-checkpoint:{idempotency_key}:{key}",
                )
            )
            for key in values
        }
        with self._session_factory() as session, session.begin():
            projection = session.get(PathwayRunRow, record_id)
            if not projection:
                raise PathwayError("Pathway run was not found")
            if (
                run.current_node != node
                or run.cycle_number != cycle_number
                or projection.current_node != node
                or projection.cycle_number != cycle_number
                or projection.status != RunStatus.ACTIVE.value
            ):
                raise PathwayError(
                    "Unguided checkpoint does not match the current pinned node and cycle"
                )
            existing = {
                key: session.get(PathwayFactRow, fact_id)
                for key, fact_id in fact_ids.items()
            }
            if any(existing.values()):
                if not all(existing.values()) or any(
                    row.fact_key != key
                    or row.value != values[key]
                    or row.status != FactStatus.CONFIRMED.value
                    or row.source_event_ids != [checkpoint_id]
                    or row.proposed_by != actor_id
                    or row.confirmed_by != actor_id
                    for key, row in existing.items()
                    if row is not None
                ):
                    raise PathwayError(
                        "Idempotency key was already used for another checkpoint"
                    )
                return checkpoint_id, False
            for key, value in values.items():
                session.add(
                    PathwayFactRow(
                        id=fact_ids[key],
                        record_id=record_id,
                        fact_key=key,
                        value=value,
                        status=FactStatus.CONFIRMED.value,
                        source_event_ids=[checkpoint_id],
                        proposed_by=actor_id,
                        confirmed_by=actor_id,
                        created_at=timestamp,
                    )
                )
            session.flush()
            return checkpoint_id, True

    def transition(
        self,
        record_id: str,
        *,
        outcome: RouteOutcome | str,
        actor_id: str,
        rationale: str,
        decided_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> PathwayRun:
        run, _created = self.transition_with_result(
            record_id,
            outcome=outcome,
            actor_id=actor_id,
            rationale=rationale,
            decided_at=decided_at,
            idempotency_key=idempotency_key,
        )
        return run

    def transition_with_result(
        self,
        record_id: str,
        *,
        outcome: RouteOutcome | str,
        actor_id: str,
        rationale: str,
        decided_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[PathwayRun, bool]:
        """Return the run and whether this request appended a decision."""

        if idempotency_key:
            with self._session_factory() as session:
                existing = session.scalar(
                    select(PathwayTransitionRow).where(
                        PathwayTransitionRow.record_id == record_id,
                        PathwayTransitionRow.idempotency_key == idempotency_key,
                    )
                )
                if existing:
                    requested = (
                        outcome.value if isinstance(outcome, RouteOutcome) else str(outcome)
                    )
                    if (
                        existing.outcome != requested
                        or existing.actor_id != actor_id
                        or existing.rationale != rationale
                    ):
                        raise PathwayError(
                            "Idempotency key was already used for another transition"
                        )
                    return self.load_run(record_id)[1], False
        definition, run = self.load_run(record_id)
        timestamp = decided_at or datetime.now(timezone.utc)
        try:
            with self._session_factory() as session, session.begin():
                projection = session.get(PathwayRunRow, record_id)
                decision_count = session.scalar(
                    select(func.count())
                    .select_from(PathwayTransitionRow)
                    .where(PathwayTransitionRow.record_id == record_id)
                )
                if not projection or len(run.decisions) != decision_count:
                    raise PathwayError("Pathway transition journal changed; retry")
                advanced = run.transition(
                    definition,
                    outcome=outcome,
                    actor_id=actor_id,
                    rationale=rationale,
                    facts=self._latest_facts(session, record_id),
                    approvals=self._latest_approvals(session, record_id),
                    decided_at=timestamp,
                )
                decision = advanced.decisions[-1]
                session.add(
                    PathwayTransitionRow(
                        record_id=record_id,
                        sequence=decision.sequence,
                        edge_id=decision.edge_id,
                        from_node=decision.from_node,
                        to_node=decision.to_node,
                        outcome=decision.outcome.value,
                        actor_id=decision.actor_id,
                        rationale=decision.rationale,
                        evidence=decision.evidence,
                        evidence_checksum=decision.evidence_checksum,
                        pathway_checksum=decision.pathway_checksum,
                        previous_decision_hash=decision.previous_decision_hash,
                        decision_hash=decision.decision_hash,
                        idempotency_key=idempotency_key,
                        decided_at=decision.decided_at,
                    )
                )
                projection.current_node = advanced.current_node
                projection.status = advanced.status.value
                projection.cycle_number = advanced.cycle_number
                projection.updated_at = timestamp
                if advanced.status is RunStatus.ACTIVE and (
                    advanced.current_node != run.current_node
                    or decision.outcome
                    in {RouteOutcome.NEGOTIATE_RETURN, RouteOutcome.REASSESS}
                ):
                    self._append_stage_readiness(
                        session,
                        record_id,
                        node=advanced.current_node,
                        cycle_number=advanced.cycle_number,
                        ready=False,
                        blocked=False,
                        source_event_id=decision.decision_hash,
                        actor_id="system:pathway",
                        now=timestamp,
                    )
                session.flush()
                return advanced, True
        except IntegrityError as error:
            raise PathwayError("Concurrent pathway transition rejected; retry") from error

    def decision_for_idempotency(
        self, record_id: str, idempotency_key: str
    ) -> TransitionDecision:
        with self._session_factory() as session:
            row = session.scalar(
                select(PathwayTransitionRow).where(
                    PathwayTransitionRow.record_id == record_id,
                    PathwayTransitionRow.idempotency_key == idempotency_key,
                )
            )
            if not row:
                raise PathwayError("Pathway transition was not found")
            return _decision_from_row(row)


__all__ = [
    "PathwayApprovalRow",
    "PathwayDefinitionRow",
    "PathwayFactRow",
    "PathwayRunRow",
    "PathwayStore",
    "PathwayTransitionRow",
]
