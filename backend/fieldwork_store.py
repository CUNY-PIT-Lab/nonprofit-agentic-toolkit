"""Durable SQLAlchemy adapter for :mod:`backend.fieldwork`.

The domain ledger remains the source of projection semantics.  This adapter
only inserts immutable rows and reconstitutes the ledger while rechecking its
hash chains, so a process restart cannot silently change the evidence stream.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event as sqlalchemy_event,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .fieldwork import (
    AccessScale,
    ActorRef,
    AppendOnlyViolation,
    BranchMode,
    BranchSpec,
    Chronology,
    EpistemicLayer,
    EventKind,
    EvidenceManifest,
    FieldworkError,
    FieldworkEvent,
    FieldworkLedger,
    Sensitivity,
    SourceRef,
    VersionManifest,
    canonical_json,
)
from .models import Base, JSON_DOCUMENT


class FieldworkProjectRow(Base):
    __tablename__ = "fieldwork_projects"

    project_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    canonical_branch_id: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FieldworkCycleRow(Base):
    __tablename__ = "fieldwork_cycles"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("fieldwork_projects.project_id", ondelete="RESTRICT"), primary_key=True
    )
    cycle_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    label: Mapped[str] = mapped_column(String(240), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FieldworkBranchRow(Base):
    __tablename__ = "fieldwork_branches"

    branch_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("fieldwork_projects.project_id", ondelete="RESTRICT"), nullable=False
    )
    cycle_id: Mapped[str | None] = mapped_column(String(120))
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    parent_branch_id: Mapped[str | None] = mapped_column(
        ForeignKey("fieldwork_branches.branch_id", ondelete="RESTRICT")
    )
    base_event_id: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "cycle_id"],
            ["fieldwork_cycles.project_id", "fieldwork_cycles.cycle_id"],
            name="fk_fieldwork_branch_cycle",
        ),
        CheckConstraint(
            "mode IN ('canonical', 'historical', 'counterfactual')",
            name="ck_fieldwork_branch_mode",
        ),
        CheckConstraint(
            "(mode = 'canonical' AND parent_branch_id IS NULL AND base_event_id IS NULL) "
            "OR (mode <> 'canonical' AND parent_branch_id IS NOT NULL "
            "AND base_event_id IS NOT NULL AND cycle_id IS NOT NULL)",
            name="ck_fieldwork_branch_shape",
        ),
        Index("ix_fieldwork_branches_project_cycle", "project_id", "cycle_id"),
    )


class FieldworkEventRow(Base):
    __tablename__ = "fieldwork_events"

    event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("fieldwork_projects.project_id", ondelete="RESTRICT"), nullable=False
    )
    project_position: Mapped[int] = mapped_column(Integer, nullable=False)
    cycle_id: Mapped[str | None] = mapped_column(String(120))
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("fieldwork_branches.branch_id", ondelete="RESTRICT"), nullable=False
    )
    branch_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    epistemic_layer: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(160), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(80), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    causal_event_ids: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON_DOCUMENT, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    canonical_effect: Mapped[bool] = mapped_column(Boolean, nullable=False)
    previous_event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    __table_args__ = (
        UniqueConstraint(
            "project_id", "project_position", name="uq_fieldwork_project_position"
        ),
        UniqueConstraint(
            "branch_id", "branch_sequence", name="uq_fieldwork_branch_sequence"
        ),
        ForeignKeyConstraint(
            ["project_id", "cycle_id"],
            ["fieldwork_cycles.project_id", "fieldwork_cycles.cycle_id"],
            name="fk_fieldwork_event_cycle",
        ),
        CheckConstraint("branch_sequence >= 1", name="ck_fieldwork_branch_sequence"),
        CheckConstraint("project_position >= 1", name="ck_fieldwork_project_position"),
        CheckConstraint(
            "observed_at <= recorded_at AND recorded_at <= committed_at",
            name="ck_fieldwork_event_chronology",
        ),
        Index("ix_fieldwork_events_project_commit", "project_id", "committed_at"),
        Index("ix_fieldwork_events_cycle_kind", "project_id", "cycle_id", "kind"),
        Index("ix_fieldwork_events_actor", "project_id", "actor_id", "actor_role"),
    )


class FieldworkScopeVersionRow(Base):
    __tablename__ = "fieldwork_scope_versions"

    event_id: Mapped[str] = mapped_column(
        ForeignKey("fieldwork_events.event_id", ondelete="RESTRICT"), primary_key=True
    )
    project_id: Mapped[str] = mapped_column(String(120), nullable=False)
    cycle_id: Mapped[str] = mapped_column(String(120), nullable=False)
    branch_id: Mapped[str] = mapped_column(String(160), nullable=False)
    graph_version: Mapped[int] = mapped_column(Integer, nullable=False)
    graph: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "cycle_id",
            "branch_id",
            "graph_version",
            name="uq_fieldwork_scope_version",
        ),
        CheckConstraint("graph_version >= 1", name="ck_fieldwork_scope_version"),
    )


def _deny_update(_mapper, _connection, target) -> None:
    raise AppendOnlyViolation(f"{type(target).__name__} rows are append-only")


def _deny_delete(_mapper, _connection, target) -> None:
    raise AppendOnlyViolation(f"{type(target).__name__} rows cannot be deleted")


for _immutable_model in (
    FieldworkProjectRow,
    FieldworkCycleRow,
    FieldworkBranchRow,
    FieldworkEventRow,
    FieldworkScopeVersionRow,
):
    sqlalchemy_event.listen(_immutable_model, "before_update", _deny_update)
    sqlalchemy_event.listen(_immutable_model, "before_delete", _deny_delete)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _normalized(value: Any) -> Any:
    if isinstance(value, datetime):
        return _aware(value).isoformat(timespec="microseconds")
    if isinstance(value, (dict, list, tuple)):
        return canonical_json(value)
    return value


def _insert_or_verify(
    session: Session,
    model,
    identity: str | tuple[str, str],
    values: Mapping[str, Any],
) -> None:
    existing = session.get(model, identity)
    if existing is None:
        session.add(model(**values))
        session.flush()
        return
    changed = [
        key
        for key, expected in values.items()
        if _normalized(getattr(existing, key)) != _normalized(expected)
    ]
    if changed:
        raise AppendOnlyViolation(
            f"Persisted {model.__name__} conflicts on: {', '.join(changed)}"
        )


def _manifest_from_dict(value: Mapping[str, Any]) -> EvidenceManifest:
    versions = dict(value["versions"])
    return EvidenceManifest(
        sensitivity=Sensitivity(value["sensitivity"]),
        allowed_scales=tuple(AccessScale(item) for item in value["allowed_scales"]),
        versions=VersionManifest(**versions),
        consent_basis=str(value.get("consent_basis", "not_required")),
        consent_subjects=tuple(value.get("consent_subjects", ())),
        authorization_tags=tuple(value.get("authorization_tags", ())),
        scope_node_ids=tuple(value.get("scope_node_ids", ())),
    )


class FieldworkStore:
    """Insert-only repository backed by a SQLAlchemy session factory."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def save(self, ledger: FieldworkLedger) -> None:
        """Persist all new ledger facts; never merge, update, or delete old facts."""

        try:
            with self._session_factory() as session, session.begin():
                events_by_project: dict[str, list[FieldworkEvent]] = defaultdict(list)
                for item in ledger.events:
                    events_by_project[item.project_id].append(item)

                for project_id, canonical_id in sorted(ledger.canonical_branches.items()):
                    created = next(
                        (
                            item
                            for item in events_by_project[project_id]
                            if item.kind is EventKind.PROJECT_CREATED
                        ),
                        None,
                    )
                    if created is None:
                        raise FieldworkError("A durable project requires its creation event")
                    _insert_or_verify(
                        session,
                        FieldworkProjectRow,
                        project_id,
                        {
                            "project_id": project_id,
                            "title": str(created.payload["title"]),
                            "canonical_branch_id": canonical_id,
                            "created_at": created.chronology.committed_at,
                        },
                    )

                for project_id, cycle_id in ledger.cycles:
                    opened = next(
                        (
                            item
                            for item in events_by_project[project_id]
                            if item.cycle_id == cycle_id and item.kind is EventKind.CYCLE_OPENED
                        ),
                        None,
                    )
                    if opened is None:
                        raise FieldworkError("A durable cycle requires its opening event")
                    _insert_or_verify(
                        session,
                        FieldworkCycleRow,
                        (project_id, cycle_id),
                        {
                            "project_id": project_id,
                            "cycle_id": cycle_id,
                            "label": str(opened.payload["label"]),
                            "opened_at": opened.chronology.committed_at,
                        },
                    )

                for branch in ledger.branch_specs:
                    _insert_or_verify(
                        session,
                        FieldworkBranchRow,
                        branch.branch_id,
                        {
                            "branch_id": branch.branch_id,
                            "project_id": branch.project_id,
                            "cycle_id": branch.cycle_id,
                            "mode": branch.mode.value,
                            "parent_branch_id": branch.parent_branch_id,
                            "base_event_id": branch.base_event_id,
                            "created_at": branch.created_at,
                        },
                    )

                positions: dict[str, int] = defaultdict(int)
                for item in ledger.events:
                    positions[item.project_id] += 1
                    values = {
                        "event_id": item.event_id,
                        "project_id": item.project_id,
                        "project_position": positions[item.project_id],
                        "cycle_id": item.cycle_id,
                        "branch_id": item.branch_id,
                        "branch_sequence": item.branch_sequence,
                        "kind": item.kind.value,
                        "epistemic_layer": item.epistemic_layer.value,
                        "actor_id": item.actor.actor_id,
                        "actor_role": item.actor.actor_role,
                        "observed_at": item.chronology.observed_at,
                        "recorded_at": item.chronology.recorded_at,
                        "committed_at": item.chronology.committed_at,
                        "payload": item.payload,
                        "causal_event_ids": list(item.causal_event_ids),
                        "source_refs": [source.as_dict() for source in item.source_refs],
                        "manifest": item.manifest.as_dict(),
                        "canonical_effect": item.canonical_effect,
                        "previous_event_hash": item.previous_event_hash,
                        "event_hash": item.event_hash,
                    }
                    _insert_or_verify(
                        session, FieldworkEventRow, item.event_id, values
                    )

                for item in ledger.events:
                    if item.kind is not EventKind.SCOPE_GRAPH_VERSIONED:
                        continue
                    graph = dict(item.payload["graph"])
                    _insert_or_verify(
                        session,
                        FieldworkScopeVersionRow,
                        item.event_id,
                        {
                            "event_id": item.event_id,
                            "project_id": item.project_id,
                            "cycle_id": str(item.cycle_id),
                            "branch_id": item.branch_id,
                            "graph_version": int(graph["version"]),
                            "graph": graph,
                            "committed_at": item.chronology.committed_at,
                        },
                    )
        except IntegrityError as error:
            raise AppendOnlyViolation("Fieldwork persistence constraint rejected the append") from error

    def load(self, project_id: str | None = None) -> FieldworkLedger:
        """Reconstitute one or all projects and verify rows against stored hashes."""

        with self._session_factory() as session:
            project_query = select(FieldworkProjectRow).order_by(
                FieldworkProjectRow.project_id
            )
            if project_id is not None:
                project_query = project_query.where(
                    FieldworkProjectRow.project_id == project_id
                )
            project_rows = tuple(session.scalars(project_query))
            if not project_rows:
                raise FieldworkError("No persisted fieldwork project matched")
            project_ids = {row.project_id for row in project_rows}

            cycles = tuple(
                session.scalars(
                    select(FieldworkCycleRow)
                    .where(FieldworkCycleRow.project_id.in_(project_ids))
                    .order_by(FieldworkCycleRow.project_id, FieldworkCycleRow.cycle_id)
                )
            )
            branch_rows = tuple(
                session.scalars(
                    select(FieldworkBranchRow)
                    .where(FieldworkBranchRow.project_id.in_(project_ids))
                    .order_by(FieldworkBranchRow.created_at, FieldworkBranchRow.branch_id)
                )
            )
            event_rows = tuple(
                session.scalars(
                    select(FieldworkEventRow)
                    .where(FieldworkEventRow.project_id.in_(project_ids))
                    .order_by(
                        FieldworkEventRow.project_id, FieldworkEventRow.project_position
                    )
                )
            )
            scope_rows = {
                row.event_id: row
                for row in session.scalars(
                    select(FieldworkScopeVersionRow).where(
                        FieldworkScopeVersionRow.project_id.in_(project_ids)
                    )
                )
            }

            branches = tuple(
                BranchSpec(
                    branch_id=row.branch_id,
                    project_id=row.project_id,
                    cycle_id=row.cycle_id,
                    mode=BranchMode(row.mode),
                    parent_branch_id=row.parent_branch_id,
                    base_event_id=row.base_event_id,
                    created_at=_aware(row.created_at),
                )
                for row in branch_rows
            )
            domain_events: list[FieldworkEvent] = []
            for row in event_rows:
                manifest = _manifest_from_dict(row.manifest)
                sources = tuple(SourceRef(**dict(item)) for item in row.source_refs)
                item = FieldworkEvent(
                    event_id=row.event_id,
                    event_hash=row.event_hash,
                    previous_event_hash=row.previous_event_hash,
                    project_id=row.project_id,
                    cycle_id=row.cycle_id,
                    branch_id=row.branch_id,
                    branch_sequence=row.branch_sequence,
                    kind=EventKind(row.kind),
                    epistemic_layer=EpistemicLayer(row.epistemic_layer),
                    actor=ActorRef(row.actor_id, row.actor_role),
                    chronology=Chronology(
                        _aware(row.observed_at),
                        _aware(row.recorded_at),
                        _aware(row.committed_at),
                    ),
                    payload_json=canonical_json(row.payload),
                    causal_event_ids=tuple(row.causal_event_ids),
                    source_refs=sources,
                    manifest=manifest,
                    canonical_effect=row.canonical_effect,
                )
                if item.kind is EventKind.SCOPE_GRAPH_VERSIONED:
                    persisted_scope = scope_rows.get(item.event_id)
                    if (
                        persisted_scope is None
                        or canonical_json(persisted_scope.graph)
                        != canonical_json(item.payload["graph"])
                    ):
                        raise AppendOnlyViolation("Scope version row does not match its event")
                domain_events.append(item)

        return FieldworkLedger.reconstitute(
            canonical_branches={
                row.project_id: row.canonical_branch_id for row in project_rows
            },
            cycles=((row.project_id, row.cycle_id) for row in cycles),
            branches=branches,
            events=domain_events,
        )


__all__ = [
    "FieldworkBranchRow",
    "FieldworkCycleRow",
    "FieldworkEventRow",
    "FieldworkProjectRow",
    "FieldworkScopeVersionRow",
    "FieldworkStore",
]
