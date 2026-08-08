"""Append-only SQLAlchemy storage for governed product evolution."""

from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event as sqlalchemy_event,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .evolution import (
    ConsentBasis,
    ConsentStatus,
    DeidentifiedProjection,
    EvaluationResult,
    EvolutionCandidate,
    EvolutionError,
    EvolutionEvidenceManifest,
    EvolutionProposal,
    HumanActor,
    NameSuggestion,
    ProductTelemetryEvent,
    ProjectionAuthorization,
    ProposalReview,
    ProposalType,
    ReviewOutcome,
    RolloutAction,
    RolloutActionKind,
    RolloutPlan,
    SemanticVersionMetadata,
    TelemetryConsentDecision,
    TelemetryManifest,
    TelemetrySensitivity,
    aggregate_telemetry,
)
from .models import Base, JSON_DOCUMENT, uuid4str


class ProductTelemetryEventRow(Base):
    __tablename__ = "product_telemetry_events"

    event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    product_area: Mapped[str] = mapped_column(String(24), nullable=False)
    cohort_key: Mapped[str] = mapped_column(String(120), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    dimensions: Mapped[dict[str, str]] = mapped_column(JSON_DOCUMENT, nullable=False)
    consent_basis: Mapped[str] = mapped_column(String(24), nullable=False)
    consent_scope_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    sensitivity: Mapped[str] = mapped_column(String(24), nullable=False)
    allowed_purposes: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False)
    deidentified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(120), nullable=False)
    app_version: Mapped[str] = mapped_column(String(120), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    previous_event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_product_telemetry_sequence"),
        CheckConstraint("deidentified = true", name="ck_product_telemetry_deidentified"),
        CheckConstraint(
            "product_area IN ('pathway', 'prompt', 'interface', 'name')",
            name="ck_product_telemetry_area",
        ),
        CheckConstraint(
            "consent_basis IN ('not_required', 'granted')",
            name="ck_product_telemetry_consent_basis",
        ),
        CheckConstraint(
            "sensitivity IN ('public', 'internal', 'restricted')",
            name="ck_product_telemetry_sensitivity",
        ),
        CheckConstraint("occurred_at <= committed_at", name="ck_product_telemetry_chronology"),
        Index("ix_product_telemetry_type_time", "event_type", "occurred_at"),
        Index("ix_product_telemetry_cohort_time", "cohort_key", "occurred_at"),
    )


class TelemetryConsentDecisionRow(Base):
    __tablename__ = "product_telemetry_consents"

    decision_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    consent_scope_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(120), nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("product_telemetry_consents.decision_id", ondelete="RESTRICT")
    )
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    __table_args__ = (
        CheckConstraint("status IN ('granted', 'withdrawn')", name="ck_product_consent_status"),
        Index("ix_product_consent_scope_time", "consent_scope_id", "decided_at"),
    )


class EvolutionProposalRow(Base):
    __tablename__ = "evolution_proposals"

    proposal_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    proposal_checksum: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    proposal_type: Mapped[str] = mapped_column(String(24), nullable=False)
    component_key: Mapped[str] = mapped_column(String(120), nullable=False)
    evidence_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "proposal_type IN ('pathway', 'prompt', 'interface', 'name')",
            name="ck_evolution_proposal_type",
        ),
        Index("ix_evolution_proposal_component", "component_key", "created_at"),
    )


class EvolutionReviewRow(Base):
    __tablename__ = "evolution_reviews"

    review_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("evolution_proposals.proposal_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    proposal_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(40), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    review_checksum: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    __table_args__ = (
        CheckConstraint("outcome IN ('approved', 'rejected')", name="ck_evolution_review_outcome"),
        Index("ix_evolution_review_proposal", "proposal_id", "decided_at"),
    )


class EvolutionRolloutActionRow(Base):
    __tablename__ = "evolution_rollout_actions"

    action_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("evolution_proposals.proposal_id", ondelete="RESTRICT"), nullable=False
    )
    proposal_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    review_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    target: Mapped[str] = mapped_column(String(160), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(40), nullable=False)
    performed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    action_checksum: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    __table_args__ = (
        UniqueConstraint("proposal_id", "action", name="uq_evolution_rollout_action"),
        CheckConstraint("action IN ('rollout', 'rollback')", name="ck_evolution_rollout_action"),
        Index("ix_evolution_rollout_proposal", "proposal_id", "performed_at"),
    )


class EvolutionEvaluationRow(Base):
    __tablename__ = "evolution_evaluations"

    evaluation_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("evolution_proposals.proposal_id", ondelete="RESTRICT"), nullable=False
    )
    rollout_action_id: Mapped[str] = mapped_column(
        ForeignKey("evolution_rollout_actions.action_id", ondelete="RESTRICT"), nullable=False
    )
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    evaluator_id: Mapped[str] = mapped_column(String(120), nullable=False)
    evaluator_role: Mapped[str] = mapped_column(String(40), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_projection_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evaluation_checksum: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('met', 'not_met', 'inconclusive')",
            name="ck_evolution_evaluation_outcome",
        ),
        Index("ix_evolution_evaluation_proposal", "proposal_id", "recorded_at"),
    )


def _deny_update(_mapper, _connection, target) -> None:
    raise EvolutionError(f"{type(target).__name__} rows are append-only")


def _deny_delete(_mapper, _connection, target) -> None:
    raise EvolutionError(f"{type(target).__name__} rows cannot be deleted")


for _immutable_model in (
    ProductTelemetryEventRow,
    TelemetryConsentDecisionRow,
    EvolutionProposalRow,
    EvolutionReviewRow,
    EvolutionRolloutActionRow,
    EvolutionEvaluationRow,
):
    sqlalchemy_event.listen(_immutable_model, "before_update", _deny_update)
    sqlalchemy_event.listen(_immutable_model, "before_delete", _deny_delete)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _manifest_from_row(row: ProductTelemetryEventRow) -> TelemetryManifest:
    return TelemetryManifest(
        consent_basis=ConsentBasis(row.consent_basis),
        consent_scope_id=row.consent_scope_id,
        sensitivity=TelemetrySensitivity(row.sensitivity),
        allowed_purposes=tuple(row.allowed_purposes),
        deidentified=row.deidentified,
        schema_version=row.schema_version,
        app_version=row.app_version,
        policy_version=row.policy_version,
    )


def _event_from_row(row: ProductTelemetryEventRow) -> ProductTelemetryEvent:
    event = ProductTelemetryEvent.build(
        event_id=row.event_id,
        sequence=row.sequence,
        event_type=row.event_type,
        product_area=row.product_area,
        cohort_key=row.cohort_key,
        metrics=row.metrics,
        dimensions=row.dimensions,
        manifest=_manifest_from_row(row),
        occurred_at=_aware(row.occurred_at),
        committed_at=_aware(row.committed_at),
        previous_event_hash=row.previous_event_hash,
    )
    if event.event_hash != row.event_hash:
        raise EvolutionError("Persisted product telemetry hash is invalid")
    return event


def _proposal_from_document(document: Mapping[str, Any]) -> EvolutionProposal:
    name_raw = document.get("name_suggestion")
    name = (
        NameSuggestion(
            suggested_name=name_raw["suggested_name"],
            aliases=tuple(name_raw.get("aliases", [])),
            rationale=name_raw["rationale"],
        )
        if name_raw
        else None
    )
    candidate = EvolutionCandidate(
        proposal_type=ProposalType(document["proposal_type"]),
        title=document["title"],
        rationale=document["rationale"],
        change_summary=document["change_summary"],
        name_suggestion=name,
    )
    versions = SemanticVersionMetadata(**document["versions"])
    plan = RolloutPlan(**document["rollout_plan"])
    evidence_raw = dict(document["evidence"])
    stored_evidence_checksum = evidence_raw.pop("evidence_checksum")
    evidence = EvolutionEvidenceManifest(**evidence_raw)
    if evidence.evidence_checksum != stored_evidence_checksum:
        raise EvolutionError("Persisted evolution evidence checksum is invalid")
    proposal = EvolutionProposal.build(
        candidate=candidate,
        versions=versions,
        rollout_plan=plan,
        evidence=evidence,
        created_at=datetime.fromisoformat(document["created_at"]),
    )
    if (
        proposal.proposal_id != document["proposal_id"]
        or proposal.proposal_checksum != document["proposal_checksum"]
    ):
        raise EvolutionError("Persisted evolution proposal checksum is invalid")
    return proposal


def _validated_proposal_row(row: EvolutionProposalRow) -> EvolutionProposal:
    """Rebuild a proposal and verify its searchable provenance columns."""

    try:
        proposal = _proposal_from_document(row.document)
    except (KeyError, TypeError, ValueError) as exc:
        raise EvolutionError("Persisted evolution proposal provenance is invalid") from exc
    if (
        row.document != proposal.as_dict()
        or row.proposal_id != proposal.proposal_id
        or row.proposal_checksum != proposal.proposal_checksum
        or row.proposal_type != proposal.proposal_type.value
        or row.component_key != proposal.versions.component_key
        or row.evidence_checksum != proposal.evidence.evidence_checksum
        or _aware(row.created_at) != proposal.created_at
    ):
        raise EvolutionError("Persisted evolution proposal provenance is invalid")
    return proposal


def _validated_review_row(row: EvolutionReviewRow) -> ProposalReview:
    try:
        review = ProposalReview(
            review_id=row.review_id,
            proposal_id=row.proposal_id,
            proposal_checksum=row.proposal_checksum,
            outcome=ReviewOutcome(row.outcome),
            actor=HumanActor(row.actor_id, row.actor_role),
            rationale=row.rationale,
            decided_at=_aware(row.decided_at),
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionError("Persisted evolution review provenance is invalid") from exc
    if review.review_checksum != row.review_checksum:
        raise EvolutionError("Persisted evolution review checksum is invalid")
    return review


def _validated_rollout_row(row: EvolutionRolloutActionRow) -> RolloutAction:
    try:
        action = RolloutAction(
            action_id=row.action_id,
            proposal_id=row.proposal_id,
            proposal_checksum=row.proposal_checksum,
            review_checksum=row.review_checksum,
            action=RolloutActionKind(row.action),
            target=row.target,
            actor=HumanActor(row.actor_id, row.actor_role),
            performed_at=_aware(row.performed_at),
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionError("Persisted evolution action provenance is invalid") from exc
    if action.action_checksum != row.action_checksum:
        raise EvolutionError("Persisted evolution action checksum is invalid")
    return action


def _validate_component_proposal(
    proposal: EvolutionProposal,
    *,
    expected_type: ProposalType | None = None,
) -> None:
    """Reject proposal metadata that would poison component projections."""

    component_key = proposal.versions.component_key
    if (
        component_key == "product.identity"
        and proposal.proposal_type is not ProposalType.NAME
    ):
        raise EvolutionError("Component proposal type provenance is invalid")
    if expected_type is not None and proposal.proposal_type is not expected_type:
        raise EvolutionError("Component proposal type provenance is invalid")
    if (
        proposal.rollout_plan.rollout_target
        != f"{component_key}@{proposal.versions.proposed_version}"
        or proposal.rollout_plan.rollback_target
        != f"{component_key}@{proposal.versions.current_version}"
    ):
        raise EvolutionError("Component proposal target provenance is invalid")


_SEMANTIC_VERSION = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class EvolutionStore:
    """Repository whose public read for the worker is an aggregate projection."""

    _component_locks_guard = threading.Lock()
    _component_locks: dict[str, threading.RLock] = {}

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        component_baselines: Mapping[str, str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        baselines: dict[str, str] = {}
        for component, version in (component_baselines or {}).items():
            if (
                not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,119}", component)
                or not _SEMANTIC_VERSION.fullmatch(version)
            ):
                raise EvolutionError("Component baselines require keys and semantic versions")
            baselines[component] = version
        self._component_baselines = baselines

    @classmethod
    @contextmanager
    def _local_component_lock(cls, component_key: str):
        with cls._component_locks_guard:
            lock = cls._component_locks.setdefault(component_key, threading.RLock())
        with lock:
            yield

    @staticmethod
    def _lock_stream(session: Session) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('product_telemetry_events'))")
            )

    @staticmethod
    def _lock_proposal_target(
        session: Session,
        component_key: str,
        rollout_target: str,
    ) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"evolution:{component_key}:{rollout_target}"},
            )

    @staticmethod
    def _lock_component(session: Session, component_key: str) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"evolution-component:{component_key}"},
            )

    def append_consent_decision(
        self,
        *,
        consent_scope_id: str,
        status: ConsentStatus | str,
        actor_id: str,
        actor_role: str,
        reason_code: str,
        decided_at: datetime,
        decision_id: str | None = None,
    ) -> TelemetryConsentDecision:
        decision = TelemetryConsentDecision(
            decision_id=decision_id or uuid4str(),
            consent_scope_id=consent_scope_id,
            status=status if isinstance(status, ConsentStatus) else ConsentStatus(status),
            actor_id=actor_id,
            actor_role=actor_role,
            reason_code=reason_code,
            decided_at=decided_at,
        )
        try:
            with self._session_factory() as session, session.begin():
                existing = session.get(TelemetryConsentDecisionRow, decision.decision_id)
                if existing:
                    if existing.decision_hash != decision.decision_hash:
                        raise EvolutionError("A consent decision id cannot identify different content")
                    return decision
                current = session.scalars(
                    select(TelemetryConsentDecisionRow)
                    .where(TelemetryConsentDecisionRow.consent_scope_id == decision.consent_scope_id)
                    .order_by(
                        TelemetryConsentDecisionRow.decided_at.desc(),
                        TelemetryConsentDecisionRow.decision_id.desc(),
                    )
                    .limit(1)
                ).first()
                session.add(
                    TelemetryConsentDecisionRow(
                        decision_id=decision.decision_id,
                        consent_scope_id=decision.consent_scope_id,
                        status=decision.status.value,
                        actor_id=decision.actor_id,
                        actor_role=decision.actor_role,
                        reason_code=decision.reason_code,
                        supersedes_id=current.decision_id if current else None,
                        decided_at=decision.decided_at,
                        decision_hash=decision.decision_hash,
                    )
                )
            return decision
        except IntegrityError as exc:
            raise EvolutionError("Consent decision conflicts with immutable history") from exc

    def _current_consent(self, session: Session) -> dict[str, ConsentStatus]:
        rows = session.scalars(
            select(TelemetryConsentDecisionRow).order_by(
                TelemetryConsentDecisionRow.decided_at,
                TelemetryConsentDecisionRow.decision_id,
            )
        ).all()
        return {row.consent_scope_id: ConsentStatus(row.status) for row in rows}

    def current_consent_status(self, consent_scope_id: str) -> ConsentStatus | None:
        with self._session_factory() as session:
            return self._current_consent(session).get(consent_scope_id)

    def append_product_event(
        self,
        *,
        event_type: str,
        product_area: ProposalType | str,
        cohort_key: str,
        metrics: Mapping[str, int | float | bool],
        dimensions: Mapping[str, str],
        manifest: TelemetryManifest,
        occurred_at: datetime,
        committed_at: datetime | None = None,
        event_id: str | None = None,
    ) -> ProductTelemetryEvent:
        identifier = event_id or uuid4str()
        committed = committed_at or datetime.now(timezone.utc)
        try:
            with self._session_factory() as session, session.begin():
                self._lock_stream(session)
                existing = session.get(ProductTelemetryEventRow, identifier)
                if existing:
                    stored = _event_from_row(existing)
                    proposed = ProductTelemetryEvent.build(
                        event_id=identifier,
                        sequence=stored.sequence,
                        event_type=event_type,
                        product_area=product_area,
                        cohort_key=cohort_key,
                        metrics=metrics,
                        dimensions=dimensions,
                        manifest=manifest,
                        occurred_at=stored.occurred_at,
                        committed_at=stored.committed_at,
                        previous_event_hash=stored.previous_event_hash,
                    )
                    if proposed.event_hash != stored.event_hash:
                        raise EvolutionError("A product event id cannot identify different content")
                    return stored
                if manifest.consent_basis is ConsentBasis.GRANTED:
                    consent = self._current_consent(session)
                    if consent.get(manifest.consent_scope_id) is not ConsentStatus.GRANTED:
                        raise EvolutionError("Consent-bound telemetry requires a current grant")
                last = session.scalars(
                    select(ProductTelemetryEventRow)
                    .order_by(ProductTelemetryEventRow.sequence.desc())
                    .limit(1)
                ).first()
                event = ProductTelemetryEvent.build(
                    event_id=identifier,
                    sequence=(last.sequence + 1 if last else 1),
                    event_type=event_type,
                    product_area=product_area,
                    cohort_key=cohort_key,
                    metrics=metrics,
                    dimensions=dimensions,
                    manifest=manifest,
                    occurred_at=occurred_at,
                    committed_at=committed,
                    previous_event_hash=(last.event_hash if last else ""),
                )
                session.add(
                    ProductTelemetryEventRow(
                        event_id=event.event_id,
                        sequence=event.sequence,
                        event_type=event.event_type,
                        product_area=event.product_area.value,
                        cohort_key=event.cohort_key,
                        metrics=event.metrics,
                        dimensions=event.dimensions,
                        consent_basis=event.manifest.consent_basis.value,
                        consent_scope_id=event.manifest.consent_scope_id,
                        sensitivity=event.manifest.sensitivity.value,
                        allowed_purposes=list(event.manifest.allowed_purposes),
                        deidentified=event.manifest.deidentified,
                        schema_version=event.manifest.schema_version,
                        app_version=event.manifest.app_version,
                        policy_version=event.manifest.policy_version,
                        occurred_at=event.occurred_at,
                        committed_at=event.committed_at,
                        previous_event_hash=event.previous_event_hash,
                        event_hash=event.event_hash,
                    )
                )
            return event
        except IntegrityError as exc:
            raise EvolutionError("Product telemetry conflicts with immutable history") from exc

    def authorized_projection(
        self,
        authorization: ProjectionAuthorization,
        *,
        minimum_cell_size: int = 3,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> DeidentifiedProjection:
        with self._session_factory() as session:
            rows = session.scalars(
                select(ProductTelemetryEventRow).order_by(ProductTelemetryEventRow.sequence)
            ).all()
            events = tuple(_event_from_row(row) for row in rows)
            consent = self._current_consent(session)
        return aggregate_telemetry(
            events,
            authorization=authorization,
            consent_status=consent,
            minimum_cell_size=minimum_cell_size,
            window_start=window_start,
            window_end=window_end,
        )

    def save_proposal(self, proposal: EvolutionProposal) -> EvolutionProposal:
        try:
            canonical = _proposal_from_document(proposal.as_dict())
        except (KeyError, TypeError, ValueError) as exc:
            raise EvolutionError("Evolution proposal provenance is invalid") from exc
        if canonical != proposal:
            raise EvolutionError("Evolution proposal provenance is invalid")
        _validate_component_proposal(canonical)
        proposal = canonical
        try:
            with self._session_factory() as session, session.begin():
                self._lock_proposal_target(
                    session,
                    proposal.versions.component_key,
                    proposal.rollout_plan.rollout_target,
                )
                rows = session.scalars(
                    select(EvolutionProposalRow)
                    .where(
                        EvolutionProposalRow.component_key
                        == proposal.versions.component_key
                    )
                    .order_by(
                        EvolutionProposalRow.created_at.desc(),
                        EvolutionProposalRow.proposal_id.desc(),
                    )
                ).all()
                for row in rows:
                    existing = _validated_proposal_row(row)
                    if existing.proposal_id == proposal.proposal_id:
                        if existing.proposal_checksum != proposal.proposal_checksum:
                            raise EvolutionError(
                                "A proposal id cannot identify different content"
                            )
                        return existing

                    same_target = (
                        existing.rollout_plan.rollout_target
                        == proposal.rollout_plan.rollout_target
                    )
                    same_relevant_evidence = (
                        existing.evidence.rule_id == proposal.evidence.rule_id
                        and existing.evidence.signal_event_type
                        == proposal.evidence.signal_event_type
                        and existing.evidence.projection_checksum
                        == proposal.evidence.projection_checksum
                    )
                    if not same_target and not same_relevant_evidence:
                        continue

                    review_row = session.scalars(
                        select(EvolutionReviewRow).where(
                            EvolutionReviewRow.proposal_id == existing.proposal_id
                        )
                    ).first()
                    if review_row is None:
                        return existing
                    review = _validated_review_row(review_row)
                    if (
                        review.proposal_checksum != existing.proposal_checksum
                        or review.decided_at < existing.created_at
                    ):
                        raise EvolutionError("Proposal review provenance is invalid")
                    if same_relevant_evidence or review.outcome is ReviewOutcome.APPROVED:
                        return existing
                    # A rejected same-target proposal is reconsidered only after
                    # the signal-scoped projection checksum changes.
                session.add(
                    EvolutionProposalRow(
                        proposal_id=proposal.proposal_id,
                        proposal_checksum=proposal.proposal_checksum,
                        proposal_type=proposal.proposal_type.value,
                        component_key=proposal.versions.component_key,
                        evidence_checksum=proposal.evidence.evidence_checksum,
                        document=proposal.as_dict(),
                        created_at=proposal.created_at,
                    )
                )
            return proposal
        except IntegrityError as exc:
            raise EvolutionError("Evolution proposal conflicts with immutable history") from exc

    def load_proposal(self, proposal_id: str) -> EvolutionProposal:
        with self._session_factory() as session:
            row = session.get(EvolutionProposalRow, proposal_id)
            if row is None:
                raise EvolutionError("Evolution proposal was not found")
            return _validated_proposal_row(row)

    def _component_state_in_session(
        self,
        session: Session,
        component_key: str,
        default_version: str,
        *,
        expected_type: ProposalType | None = None,
    ) -> tuple[
        str,
        EvolutionProposal | None,
        list[str],
        tuple[EvolutionProposal, ...],
    ]:
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,119}", component_key)
            or not _SEMANTIC_VERSION.fullmatch(default_version)
        ):
            raise EvolutionError("Component state requires a key and semantic version")
        configured = self._component_baselines.get(component_key)
        if configured is not None and configured != default_version:
            raise EvolutionError("Component state baseline conflicts with trusted configuration")

        proposal_rows = session.scalars(
            select(EvolutionProposalRow).order_by(
                EvolutionProposalRow.created_at,
                EvolutionProposalRow.proposal_id,
            )
        ).all()
        proposals: dict[str, EvolutionProposal] = {}
        for row in proposal_rows:
            proposal = _validated_proposal_row(row)
            if proposal.versions.component_key != component_key:
                continue
            _validate_component_proposal(proposal, expected_type=expected_type)
            proposals[proposal.proposal_id] = proposal

        if not proposals:
            return default_version, None, [], ()

        proposal_ids = tuple(proposals)
        review_rows = session.scalars(
            select(EvolutionReviewRow).where(
                EvolutionReviewRow.proposal_id.in_(proposal_ids)
            )
        ).all()
        reviews: dict[str, ProposalReview] = {}
        for row in review_rows:
            review = _validated_review_row(row)
            proposal = proposals.get(review.proposal_id)
            if (
                proposal is None
                or review.proposal_checksum != proposal.proposal_checksum
                or review.decided_at < proposal.created_at
            ):
                raise EvolutionError("Component review provenance is invalid")
            reviews[review.proposal_id] = review

        action_rows = session.scalars(
            select(EvolutionRolloutActionRow)
            .where(EvolutionRolloutActionRow.proposal_id.in_(proposal_ids))
            .order_by(
                EvolutionRolloutActionRow.performed_at,
                EvolutionRolloutActionRow.action_id,
            )
        ).all()

        stack: list[str] = []
        action_checksums: list[str] = []
        for row in action_rows:
            action = _validated_rollout_row(row)
            proposal = proposals.get(action.proposal_id)
            review = reviews.get(action.proposal_id)
            if (
                proposal is None
                or review is None
                or review.outcome is not ReviewOutcome.APPROVED
                or action.proposal_checksum != proposal.proposal_checksum
                or action.review_checksum != review.review_checksum
                or action.performed_at < review.decided_at
            ):
                raise EvolutionError("Component rollout provenance is invalid")

            current_version = (
                proposals[stack[-1]].versions.proposed_version
                if stack
                else default_version
            )
            expected_target = (
                proposal.rollout_plan.rollout_target
                if action.action is RolloutActionKind.ROLLOUT
                else proposal.rollout_plan.rollback_target
            )
            if action.target != expected_target:
                raise EvolutionError("Component rollout target provenance is invalid")

            if action.action is RolloutActionKind.ROLLOUT:
                if proposal.versions.current_version != current_version:
                    raise EvolutionError("Component rollout semantic version chain is invalid")
                stack.append(proposal.proposal_id)
            else:
                if not stack or stack[-1] != proposal.proposal_id:
                    raise EvolutionError("Component rollback does not target the active rollout")
                stack.pop()
                restored_version = (
                    proposals[stack[-1]].versions.proposed_version
                    if stack
                    else default_version
                )
                if proposal.versions.current_version != restored_version:
                    raise EvolutionError("Component rollback semantic version chain is invalid")
            action_checksums.append(action.action_checksum)

        active = proposals[stack[-1]] if stack else None
        return (
            active.versions.proposed_version if active else default_version,
            active,
            action_checksums,
            tuple(proposals[proposal_id] for proposal_id in stack),
        )

    def _active_component_state(
        self,
        component_key: str,
        default_version: str,
        *,
        expected_type: ProposalType | None = None,
    ) -> tuple[str, EvolutionProposal | None, list[str]]:
        with self._session_factory() as session:
            version, active, actions, _stack = self._component_state_in_session(
                session,
                component_key,
                default_version,
                expected_type=expected_type,
            )
        return version, active, actions

    def active_component_version(self, component_key: str, default_version: str) -> str:
        """Read the semantic version established by approved rollout actions."""

        version, _proposal, _actions = self._active_component_state(
            component_key,
            default_version,
        )
        return version

    def active_identity(
        self,
        default_name: str,
        default_version: str,
    ) -> dict[str, Any]:
        """Resolve the active human-approved name without mutating any state."""

        normalized_default = default_name.strip()
        if not 2 <= len(normalized_default) <= 80 or "\n" in normalized_default:
            raise EvolutionError("Default identity requires a bounded name")
        version, active, action_checksums = self._active_component_state(
            "product.identity",
            default_version,
            expected_type=ProposalType.NAME,
        )
        if active is None:
            return {
                "display_name": normalized_default,
                "aliases": [],
                "semantic_version": version,
                "proposal_checksum": None,
                "action_checksums": action_checksums,
                "source": "default",
            }
        suggestion = active.name_suggestion
        if suggestion is None:
            raise EvolutionError("Active name proposal has no name suggestion")
        return {
            "display_name": suggestion.suggested_name,
            "aliases": list(suggestion.aliases),
            "semantic_version": version,
            "proposal_checksum": active.proposal_checksum,
            "action_checksums": action_checksums,
            "source": "approved_rollout",
        }

    def record_review(
        self,
        proposal_id: str,
        *,
        outcome: ReviewOutcome | str,
        actor: HumanActor,
        rationale: str,
        decided_at: datetime,
        review_id: str | None = None,
    ) -> ProposalReview:
        proposal = self.load_proposal(proposal_id)
        review = ProposalReview(
            review_id=review_id or uuid4str(),
            proposal_id=proposal.proposal_id,
            proposal_checksum=proposal.proposal_checksum,
            outcome=outcome if isinstance(outcome, ReviewOutcome) else ReviewOutcome(outcome),
            actor=actor,
            rationale=rationale,
            decided_at=decided_at,
        )
        if review.decided_at < proposal.created_at:
            raise EvolutionError("A proposal review cannot precede proposal creation")
        try:
            with self._session_factory() as session, session.begin():
                prior = session.scalars(
                    select(EvolutionReviewRow).where(EvolutionReviewRow.proposal_id == proposal_id)
                ).first()
                if prior:
                    if prior.review_checksum != review.review_checksum:
                        raise EvolutionError("An immutable proposal can have only one final review")
                    return review
                session.add(
                    EvolutionReviewRow(
                        review_id=review.review_id,
                        proposal_id=review.proposal_id,
                        proposal_checksum=review.proposal_checksum,
                        outcome=review.outcome.value,
                        actor_id=review.actor.actor_id,
                        actor_role=review.actor.role,
                        rationale=review.rationale,
                        decided_at=review.decided_at,
                        review_checksum=review.review_checksum,
                    )
                )
            return review
        except IntegrityError as exc:
            raise EvolutionError("Evolution review conflicts with immutable history") from exc

    def rollout_actions(self, proposal_id: str) -> tuple[RolloutAction, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(EvolutionRolloutActionRow)
                .where(EvolutionRolloutActionRow.proposal_id == proposal_id)
                .order_by(EvolutionRolloutActionRow.performed_at)
            ).all()
            return tuple(
                RolloutAction(
                    action_id=row.action_id,
                    proposal_id=row.proposal_id,
                    proposal_checksum=row.proposal_checksum,
                    review_checksum=row.review_checksum,
                    action=RolloutActionKind(row.action),
                    target=row.target,
                    actor=HumanActor(row.actor_id, row.actor_role),
                    performed_at=_aware(row.performed_at),
                )
                for row in rows
            )

    def record_rollout_action(
        self,
        proposal_id: str,
        *,
        action: RolloutActionKind | str,
        actor: HumanActor,
        performed_at: datetime,
        action_id: str | None = None,
    ) -> RolloutAction:
        kind = action if isinstance(action, RolloutActionKind) else RolloutActionKind(action)
        if performed_at.tzinfo is None or performed_at.utcoffset() is None:
            raise EvolutionError("Rollout timestamps must include a UTC offset")
        with self._session_factory() as session:
            discovery = session.get(EvolutionProposalRow, proposal_id)
            if discovery is None:
                raise EvolutionError("Evolution proposal was not found")
            component_key = discovery.component_key
        baseline = self._component_baselines.get(component_key)
        if baseline is None:
            raise EvolutionError(
                "Rollout recording requires a trusted component baseline"
            )
        identifier = action_id or uuid4str()
        try:
            with self._local_component_lock(component_key):
                with self._session_factory() as session, session.begin():
                    self._lock_component(session, component_key)
                    proposal_row = session.get(EvolutionProposalRow, proposal_id)
                    if proposal_row is None:
                        raise EvolutionError("Evolution proposal was not found")
                    proposal = _validated_proposal_row(proposal_row)
                    if proposal.versions.component_key != component_key:
                        raise EvolutionError("Proposal component provenance is invalid")
                    review_row = session.scalars(
                        select(EvolutionReviewRow).where(
                            EvolutionReviewRow.proposal_id == proposal_id
                        )
                    ).first()
                    if review_row is None:
                        raise EvolutionError("A human approval is required before rollout")
                    review = _validated_review_row(review_row)
                    if (
                        review.outcome is not ReviewOutcome.APPROVED
                        or review.proposal_checksum != proposal.proposal_checksum
                        or review.decided_at < proposal.created_at
                    ):
                        raise EvolutionError("A valid human approval is required before rollout")

                    current_version, active, _checksums, stack = (
                        self._component_state_in_session(
                            session,
                            component_key,
                            baseline,
                        )
                    )
                    existing_by_id = session.get(EvolutionRolloutActionRow, identifier)
                    existing_for_kind = session.scalars(
                        select(EvolutionRolloutActionRow).where(
                            EvolutionRolloutActionRow.proposal_id == proposal_id,
                            EvolutionRolloutActionRow.action == kind.value,
                        )
                    ).first()
                    if (
                        existing_by_id is not None
                        and existing_for_kind is not None
                        and existing_by_id.action_id != existing_for_kind.action_id
                    ):
                        raise EvolutionError("Rollout idempotency key conflicts with history")
                    existing = existing_by_id or existing_for_kind
                    target = (
                        proposal.rollout_plan.rollout_target
                        if kind is RolloutActionKind.ROLLOUT
                        else proposal.rollout_plan.rollback_target
                    )
                    if existing is not None:
                        stored = _validated_rollout_row(existing)
                        retry = RolloutAction(
                            action_id=identifier,
                            proposal_id=proposal.proposal_id,
                            proposal_checksum=proposal.proposal_checksum,
                            review_checksum=review.review_checksum,
                            action=kind,
                            target=target,
                            actor=actor,
                            performed_at=stored.performed_at,
                        )
                        if retry.action_checksum != stored.action_checksum:
                            raise EvolutionError("Rollout idempotency key conflicts with history")
                        return stored

                    if performed_at < review.decided_at:
                        raise EvolutionError("Rollout cannot precede its human approval")
                    if kind is RolloutActionKind.ROLLOUT:
                        if proposal.versions.current_version != current_version:
                            raise EvolutionError(
                                "Rollout proposal is stale for the active component version"
                            )
                    else:
                        if active is None or active.proposal_id != proposal.proposal_id:
                            raise EvolutionError(
                                "Rollback must target the active component rollout"
                            )
                        restored_version = (
                            stack[-2].versions.proposed_version
                            if len(stack) > 1
                            else baseline
                        )
                        if proposal.versions.current_version != restored_version:
                            raise EvolutionError(
                                "Rollback target does not restore the prior component version"
                            )

                    latest_row = session.scalars(
                        select(EvolutionRolloutActionRow)
                        .join(
                            EvolutionProposalRow,
                            EvolutionRolloutActionRow.proposal_id
                            == EvolutionProposalRow.proposal_id,
                        )
                        .where(EvolutionProposalRow.component_key == component_key)
                        .order_by(
                            EvolutionRolloutActionRow.performed_at.desc(),
                            EvolutionRolloutActionRow.action_id.desc(),
                        )
                    ).first()
                    if latest_row is not None:
                        latest = _validated_rollout_row(latest_row)
                        if (performed_at, identifier) <= (
                            latest.performed_at,
                            latest.action_id,
                        ):
                            raise EvolutionError(
                                "Rollout action chronology must append after component history"
                            )

                    result = RolloutAction(
                        action_id=identifier,
                        proposal_id=proposal.proposal_id,
                        proposal_checksum=proposal.proposal_checksum,
                        review_checksum=review.review_checksum,
                        action=kind,
                        target=target,
                        actor=actor,
                        performed_at=performed_at,
                    )
                    session.add(
                        EvolutionRolloutActionRow(
                            action_id=result.action_id,
                            proposal_id=result.proposal_id,
                            proposal_checksum=result.proposal_checksum,
                            review_checksum=result.review_checksum,
                            action=result.action.value,
                            target=result.target,
                            actor_id=result.actor.actor_id,
                            actor_role=result.actor.role,
                            performed_at=result.performed_at,
                            action_checksum=result.action_checksum,
                        )
                    )
                    session.flush()
                return result
        except IntegrityError as exc:
            raise EvolutionError("Rollout action conflicts with immutable history") from exc

    def record_evaluation(self, result: EvaluationResult) -> EvaluationResult:
        proposal = self.load_proposal(result.proposal_id)
        with self._session_factory() as session:
            rollout = session.get(EvolutionRolloutActionRow, result.rollout_action_id)
            if (
                rollout is None
                or rollout.proposal_id != proposal.proposal_id
                or rollout.action != RolloutActionKind.ROLLOUT.value
            ):
                raise EvolutionError("Evaluation results must reference this proposal's rollout")
        try:
            with self._session_factory() as session, session.begin():
                existing = session.get(EvolutionEvaluationRow, result.evaluation_id)
                if existing:
                    if existing.evaluation_checksum != result.evaluation_checksum:
                        raise EvolutionError("An evaluation id cannot identify different content")
                    return result
                session.add(
                    EvolutionEvaluationRow(
                        evaluation_id=result.evaluation_id,
                        proposal_id=result.proposal_id,
                        rollout_action_id=result.rollout_action_id,
                        outcome=result.outcome.value,
                        metrics=result.metrics,
                        evaluator_id=result.evaluator.actor_id,
                        evaluator_role=result.evaluator.role,
                        rationale=result.rationale,
                        evidence_projection_checksum=result.evidence_projection_checksum,
                        recorded_at=result.recorded_at,
                        evaluation_checksum=result.evaluation_checksum,
                    )
                )
            return result
        except IntegrityError as exc:
            raise EvolutionError("Evaluation result conflicts with immutable history") from exc
