"""Record-scoped API for versioned pathway facts, approvals, and transitions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .pathway_store import PathwayStore
from .pathways import (
    Approval,
    ApprovalStatus,
    FactStatus,
    PathwayError,
    RecordFact,
    RouteOutcome,
    checksum,
    confirmed_fact_evidence,
)

SYSTEM_FACT_KEYS = frozenset(
    {"stage_ready", "stage_ready_node", "stage_ready_cycle", "stage_blocked"}
)


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InitializeBody(StrictBody):
    entry_role: str = Field(default="author", pattern="^(author|reviewer|monitor)$")


class FactBody(StrictBody):
    key: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_.:-]*$")
    value: Any
    status: FactStatus = FactStatus.PROPOSED
    source_event_ids: tuple[str, ...] = ()
    supersedes_id: str | None = Field(default=None, max_length=36)

    @field_validator("source_event_ids")
    @classmethod
    def validate_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not value.strip() for value in values):
            raise ValueError("source_event_ids must be non-blank and unique")
        return values


class ApprovalBody(StrictBody):
    gate_key: str = Field(
        min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_.:-]*$"
    )
    status: ApprovalStatus
    supersedes_id: str | None = Field(default=None, max_length=36)
    rationale: str = Field(min_length=1, max_length=4_000)


class TransitionBody(StrictBody):
    outcome: RouteOutcome
    rationale: str = Field(min_length=1, max_length=4_000)
    idempotency_key: str = Field(min_length=8, max_length=120)


class CheckpointBody(StrictBody):
    node: str = Field(pattern="^(synthesis|pilot|monitoring)$")
    cycle_number: int = Field(ge=1)
    confirmed: Literal[True]
    rationale: str = Field(min_length=8, max_length=4_000)
    idempotency_key: str = Field(min_length=8, max_length=120)


def _actor_id(auth_result: Any) -> str:
    actor = auth_result[0] if isinstance(auth_result, (tuple, list)) else auth_result
    value = actor.get("id") if isinstance(actor, dict) else getattr(actor, "id", None)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(401, "Authenticated actor attribution is unavailable")
    return value


def _domain_error(error: PathwayError) -> HTTPException:
    message = str(error)
    if "not found" in message:
        return HTTPException(404, message)
    if (
        "already" in message
        or "pinned" in message
        or "retry" in message
        or "does not match" in message
        or "does not have" in message
    ):
        return HTTPException(409, message)
    return HTTPException(422, message)


def _audit_commit(
    dbs: Any,
    audit: Callable[..., None],
    event_type: str,
    *,
    actor_id: str,
    record_id: str,
    metadata: dict[str, Any],
) -> None:
    audit(
        dbs,
        event_type,
        actor=actor_id,
        entity_type="record",
        entity_id=record_id,
        metadata=metadata,
    )
    dbs.commit()


def create_pathway_router(
    *,
    db_dependency: Callable[..., Any],
    auth_dependency: Callable[..., Any],
    require_csrf: Callable[[Request, Any], None],
    record_access: Callable[[Any, str, str], Any],
    membership_role: Callable[[Any, Any, str], str],
    audit: Callable[..., None],
    store_factory: Callable[[], PathwayStore],
) -> APIRouter:
    router = APIRouter(prefix="/api/records/{record_id}/pathway", tags=["pathway"])

    def context(record_id: str, dbs: Any, auth_result: Any):
        actor_id = _actor_id(auth_result)
        record = record_access(dbs, actor_id, record_id)
        role = membership_role(dbs, record, actor_id)
        return actor_id, role, record

    @router.post("", status_code=201)
    def initialize_pathway(
        record_id: str,
        body: InitializeBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        actor_id, role, _record = context(record_id, dbs, auth_result)
        require_csrf(request, dbs)
        if body.entry_role != "author" and role not in {"owner", "reviewer"}:
            raise HTTPException(403, "Only an owner or reviewer may choose this entry role")
        try:
            _definition, run = store_factory().ensure_run(
                record_id, entry_role=body.entry_role, actor_id=actor_id
            )
            state = store_factory().state(record_id)
        except PathwayError as error:
            raise _domain_error(error) from error
        _audit_commit(
            dbs,
            audit,
            "pathway.initialized",
            actor_id=actor_id,
            record_id=record_id,
            metadata={
                "definition_checksum": run.pathway_checksum,
                "entry_role": run.entry_role,
            },
        )
        return {"pathway": state}

    @router.get("")
    def get_pathway(
        record_id: str,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        _actor, _role, _record = context(record_id, dbs, auth_result)
        try:
            return {"pathway": store_factory().state(record_id)}
        except PathwayError as error:
            raise _domain_error(error) from error

    @router.post("/facts", status_code=201)
    def append_fact(
        record_id: str,
        body: FactBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        actor_id, _role, _record = context(record_id, dbs, auth_result)
        require_csrf(request, dbs)
        if body.key in SYSTEM_FACT_KEYS:
            raise HTTPException(403, "Stage readiness is recorded by the guided review")
        fact = RecordFact(
            key=body.key,
            value=body.value,
            status=body.status,
            source_event_ids=body.source_event_ids,
            confirmed_by=actor_id if body.status is FactStatus.CONFIRMED else "",
        )
        try:
            fact_id = store_factory().append_fact(
                record_id,
                fact,
                proposed_by=actor_id,
                supersedes_id=body.supersedes_id,
            )
            state = store_factory().state(record_id)
        except PathwayError as error:
            raise _domain_error(error) from error
        _audit_commit(
            dbs,
            audit,
            "pathway.fact_appended",
            actor_id=actor_id,
            record_id=record_id,
            metadata={"fact_id": fact_id, "fact_key": body.key, "status": body.status.value},
        )
        return {"fact_id": fact_id, "pathway": state}

    @router.post("/approvals", status_code=201)
    def append_approval(
        record_id: str,
        body: ApprovalBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        actor_id, role, _record = context(record_id, dbs, auth_result)
        require_csrf(request, dbs)
        if role not in {"owner", "reviewer"}:
            raise HTTPException(403, "Only an owner or reviewer may record an approval")
        store = store_factory()
        try:
            facts, _approvals = store.current_evidence(record_id)
            approval = Approval(
                gate_key=body.gate_key,
                status=body.status,
                actor_id=actor_id,
                subject_checksum=checksum(confirmed_fact_evidence(facts)),
                decided_at=datetime.now(timezone.utc),
                rationale=body.rationale.strip(),
            )
            approval_id = store.append_approval(
                record_id, approval, supersedes_id=body.supersedes_id
            )
            state = store.state(record_id)
        except PathwayError as error:
            raise _domain_error(error) from error
        _audit_commit(
            dbs,
            audit,
            "pathway.approval_appended",
            actor_id=actor_id,
            record_id=record_id,
            metadata={
                "approval_id": approval_id,
                "gate_key": body.gate_key,
                "status": body.status.value,
            },
        )
        return {"approval_id": approval_id, "pathway": state}

    @router.post("/checkpoints", status_code=201)
    def confirm_unguided_checkpoint(
        record_id: str,
        body: CheckpointBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        actor_id, role, _record = context(record_id, dbs, auth_result)
        require_csrf(request, dbs)
        if role not in {"owner", "reviewer"}:
            raise HTTPException(
                403, "Only an owner or reviewer may confirm this checkpoint"
            )
        store = store_factory()
        try:
            checkpoint_id, created = store.confirm_unguided_checkpoint(
                record_id,
                node=body.node,
                cycle_number=body.cycle_number,
                actor_id=actor_id,
                rationale=body.rationale.strip(),
                idempotency_key=body.idempotency_key,
            )
            state = store.state(record_id)
        except PathwayError as error:
            raise _domain_error(error) from error
        if created:
            _audit_commit(
                dbs,
                audit,
                "pathway.unguided_checkpoint_confirmed",
                actor_id=actor_id,
                record_id=record_id,
                metadata={
                    "checkpoint_id": checkpoint_id,
                    "node": body.node,
                    "cycle_number": body.cycle_number,
                },
            )
        return {
            "checkpoint_id": checkpoint_id,
            "idempotent_replay": not created,
            "pathway": state,
        }

    @router.post("/transitions")
    def transition(
        record_id: str,
        body: TransitionBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        actor_id, role, record = context(record_id, dbs, auth_result)
        require_csrf(request, dbs)
        if body.outcome in {
            RouteOutcome.PROCEED,
            RouteOutcome.WALK_AWAY,
            RouteOutcome.NON_AI,
            RouteOutcome.RETIRE,
        } and role not in {"owner", "reviewer"}:
            raise HTTPException(403, "This transition requires an owner or reviewer")
        store = store_factory()
        try:
            run, created = store.transition_with_result(
                record_id,
                outcome=body.outcome,
                actor_id=actor_id,
                rationale=body.rationale,
                idempotency_key=body.idempotency_key,
            )
            decision = store.decision_for_idempotency(
                record_id, body.idempotency_key
            )
            state = store.state(record_id)
        except PathwayError as error:
            raise _domain_error(error) from error
        record.current_stage = run.current_node
        record.status = (
            "stopped"
            if run.status.value in {"walked_away", "non_ai", "retired"}
            else "active"
        )
        if created:
            _audit_commit(
                dbs,
                audit,
                "pathway.transitioned",
                actor_id=actor_id,
                record_id=record_id,
                metadata={
                    "outcome": body.outcome.value,
                    "current_node": run.current_node,
                    "decision_hash": decision.decision_hash,
                    "cycle_number": run.cycle_number,
                },
            )
        return {"pathway": state, "idempotent_replay": not created}

    return router


__all__ = [
    "ApprovalBody",
    "CheckpointBody",
    "FactBody",
    "InitializeBody",
    "TransitionBody",
    "create_pathway_router",
]
