"""Record-scoped HTTP contract for replayable fieldwork.

The router owns request validation and delegates persistence and replay
semantics to :mod:`backend.fieldwork`.  It deliberately exposes typed entry
routes rather than a generic ``event kind`` input: an HTTP client cannot forge
project, cycle, branch, consent, or output lifecycle events.

The containing application supplies its own database, authentication, CSRF,
record-access, audit, version, and authorization callbacks.  ``store_factory``
must return a :class:`FieldworkStore` backed by an independent session factory;
the request session must never be handed to the store because the durable
adapter controls its own transaction lifecycle.
"""

from __future__ import annotations

import hashlib
import inspect
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .fieldwork import (
    AccessScale,
    ActorRef,
    AppendOnlyViolation,
    AuthorizationContext,
    AuthorizationDenied,
    BranchMode,
    Chronology,
    EpistemicLayer,
    EventKind,
    EvidenceManifest,
    FieldworkError,
    FieldworkEvent,
    FieldworkLedger,
    ReplayMode,
    ScopeEdge,
    ScopeGraph,
    ScopeKind,
    ScopeNode,
    SENSITIVITY_RANK,
    Sensitivity,
    SourceRef,
    VersionManifest,
    canonical_json,
)
from .fieldwork_store import FieldworkStore


MAX_CONTENT_LENGTH = 30_000
_CONSENT_BASES = {"not_required", "granted", "pending"}


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChronologyBody(StrictBody):
    observed_at: datetime
    recorded_at: datetime


class ManifestBody(StrictBody):
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    allowed_scales: tuple[AccessScale, ...] = (AccessScale.ORGANIZATION,)
    consent_basis: str = Field(default="not_required", max_length=24)
    consent_subjects: tuple[str, ...] = ()
    authorization_tags: tuple[str, ...] = ()
    scope_node_ids: tuple[str, ...] = ()

    @field_validator("consent_basis")
    @classmethod
    def validate_consent_basis(cls, value: str) -> str:
        value = value.strip()
        if value not in _CONSENT_BASES:
            raise ValueError("consent_basis must be not_required, granted, or pending")
        return value

    @field_validator("consent_subjects", "authorization_tags", "scope_node_ids")
    @classmethod
    def validate_unique_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("manifest collections must contain unique values")
        for value in values:
            if not value.strip():
                raise ValueError("manifest references cannot be blank")
        return values

    @field_validator("allowed_scales")
    @classmethod
    def validate_allowed_scales(
        cls, values: tuple[AccessScale, ...]
    ) -> tuple[AccessScale, ...]:
        if not values or len(set(values)) != len(values):
            raise ValueError("allowed_scales must be non-empty and unique")
        return values


class SourceRefBody(StrictBody):
    source_id: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=120)
    locator: str = Field(default="", max_length=500)
    content_sha256: str = Field(default="", max_length=64)


class CycleCreateBody(ChronologyBody):
    cycle_id: str | None = Field(default=None, min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=240)

    @field_validator("cycle_id")
    @classmethod
    def validate_cycle_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or any(char not in _IDENTIFIER_CHARS for char in value):
            raise ValueError("cycle_id contains unsupported characters")
        return value


class EntryBody(ChronologyBody, ManifestBody):
    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    branch_id: str | None = Field(default=None, min_length=1, max_length=160)
    causal_event_ids: tuple[str, ...] = ()
    source_refs: tuple[SourceRefBody, ...] = ()
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("content")
    @classmethod
    def content_cannot_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content cannot be blank")
        return value

    @field_validator("branch_id")
    @classmethod
    def validate_branch_id(cls, value: str | None) -> str | None:
        return _validated_identifier(value, "branch_id")

    @field_validator("causal_event_ids")
    @classmethod
    def validate_causes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _nonblank_unique(values, "causal_event_ids")


class StoredOutputBody(ChronologyBody, ManifestBody):
    output_id: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    generator: str = Field(min_length=1, max_length=160)
    branch_id: str | None = Field(default=None, min_length=1, max_length=160)
    input_event_ids: tuple[str, ...] = Field(min_length=1)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("output_id", "branch_id")
    @classmethod
    def validate_ids(cls, value: str | None) -> str | None:
        return _validated_identifier(value, "output or branch id")

    @field_validator("content", "generator")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be blank")
        return value

    @field_validator("input_event_ids")
    @classmethod
    def validate_inputs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _nonblank_unique(values, "input_event_ids")


class ScopeNodeBody(StrictBody):
    node_id: str = Field(min_length=1, max_length=160)
    kind: ScopeKind
    label: str = Field(min_length=1, max_length=240)
    attributes: dict[str, Any] = Field(default_factory=dict)


class ScopeEdgeBody(StrictBody):
    source: str = Field(min_length=1, max_length=160)
    target: str = Field(min_length=1, max_length=160)
    relation: str = Field(min_length=1, max_length=120)


class ScopeGraphBody(ChronologyBody, ManifestBody):
    version: int = Field(ge=1)
    nodes: tuple[ScopeNodeBody, ...] = Field(min_length=1)
    edges: tuple[ScopeEdgeBody, ...] = ()
    branch_id: str | None = Field(default=None, min_length=1, max_length=160)
    causal_event_ids: tuple[str, ...] = ()
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("branch_id")
    @classmethod
    def validate_branch_id(cls, value: str | None) -> str | None:
        return _validated_identifier(value, "branch_id")

    @field_validator("causal_event_ids")
    @classmethod
    def validate_causes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _nonblank_unique(values, "causal_event_ids")


class ConsentBody(ChronologyBody):
    subject_id: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=2_000)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("subject_id", "reason")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value cannot be blank")
        return value


class ForkMode(str, Enum):
    HISTORICAL = BranchMode.HISTORICAL.value
    COUNTERFACTUAL = BranchMode.COUNTERFACTUAL.value


class BranchForkBody(ChronologyBody):
    mode: ForkMode
    parent_branch_id: str | None = Field(default=None, min_length=1, max_length=160)
    base_event_id: str = Field(min_length=1, max_length=160)
    rationale: str = Field(min_length=1, max_length=4_000)
    branch_id: str | None = Field(default=None, min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("parent_branch_id", "branch_id")
    @classmethod
    def validate_branch_ids(cls, value: str | None) -> str | None:
        return _validated_identifier(value, "branch_id")

    @field_validator("base_event_id", "rationale")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value cannot be blank")
        return value


_IDENTIFIER_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)


def _validated_identifier(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if (
        not value
        or len(value) > 160
        or any(char not in _IDENTIFIER_CHARS for char in value)
    ):
        raise ValueError(f"{label} contains unsupported characters")
    return value


def _nonblank_unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError(f"{label} cannot contain blank references")
    if len(set(cleaned)) != len(cleaned):
        raise ValueError(f"{label} must contain unique references")
    return cleaned


@dataclass(frozen=True)
class EntrySpec:
    slug: str
    entry_type: str
    kind: EventKind
    layer: EpistemicLayer
    requires_cause: bool = False
    requires_consent_subject: bool = False


@dataclass(frozen=True)
class ConsentAuthority:
    """Trusted authority for one authenticated principal's consent writes.

    This value must be derived server-side from organizational policy and an
    authenticated participant binding.  Request bodies deliberately cannot
    set these fields.
    """

    principal_id: str
    actor_role: str
    bound_subject_id: str | None = None
    can_act_for_other_subjects: bool = False
    sensitivity: Sensitivity = Sensitivity.RESTRICTED
    allowed_scales: tuple[AccessScale, ...] = (AccessScale.ORGANIZATION,)
    authorization_tags: frozenset[str] = frozenset()
    scope_node_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.principal_id.strip() or not self.actor_role.strip():
            raise ValueError("Consent authority requires principal and actor role")
        if self.bound_subject_id is not None and not self.bound_subject_id.strip():
            raise ValueError("Consent subject bindings cannot be blank")
        if not isinstance(self.sensitivity, Sensitivity):
            raise ValueError("Consent authority requires a known sensitivity")
        if (
            SENSITIVITY_RANK[self.sensitivity]
            < SENSITIVITY_RANK[Sensitivity.RESTRICTED]
        ):
            raise ValueError("Consent events require restricted or sensitive handling")
        if not self.allowed_scales or len(set(self.allowed_scales)) != len(
            self.allowed_scales
        ):
            raise ValueError("Consent authority requires unique access scales")
        if any(not isinstance(scale, AccessScale) for scale in self.allowed_scales):
            raise ValueError("Consent authority requires known access scales")
        if any(not value.strip() for value in self.authorization_tags):
            raise ValueError("Consent authority tags cannot be blank")
        if any(not value.strip() for value in self.scope_node_ids):
            raise ValueError("Consent authority scope nodes cannot be blank")


ENTRY_SPECS = (
    EntrySpec(
        "observations",
        "observation",
        EventKind.OBSERVATION_RECORDED,
        EpistemicLayer.OBSERVATION,
    ),
    EntrySpec(
        "participant-accounts",
        "participant_account",
        EventKind.OBSERVATION_RECORDED,
        EpistemicLayer.PARTICIPANT_ACCOUNT,
        requires_consent_subject=True,
    ),
    EntrySpec(
        "reflexive-memos",
        "reflexive_memo",
        EventKind.OBSERVATION_RECORDED,
        EpistemicLayer.REFLEXIVE_MEMO,
    ),
    EntrySpec(
        "positionality-memos",
        "positionality_memo",
        EventKind.OBSERVATION_RECORDED,
        EpistemicLayer.POSITIONALITY,
    ),
    EntrySpec(
        "interpretations",
        "interpretation",
        EventKind.INTERPRETATION_COMMITTED,
        EpistemicLayer.INTERPRETATION,
        requires_cause=True,
    ),
    EntrySpec(
        "member-checks",
        "member_check",
        EventKind.INTERPRETATION_COMMITTED,
        EpistemicLayer.MEMBER_CHECK,
        requires_cause=True,
        requires_consent_subject=True,
    ),
    EntrySpec(
        "decisions",
        "decision",
        EventKind.DECISION_COMMITTED,
        EpistemicLayer.DECISION,
        requires_cause=True,
    ),
    EntrySpec(
        "interventions",
        "intervention",
        EventKind.OBSERVATION_RECORDED,
        EpistemicLayer.INTERVENTION,
        requires_cause=True,
    ),
    EntrySpec(
        "after-effects",
        "after_effect",
        EventKind.OBSERVATION_RECORDED,
        EpistemicLayer.AFTER_EFFECT,
        requires_cause=True,
    ),
)


def _actor_identity(auth_context: Any) -> tuple[Any, str]:
    actor = auth_context[0] if isinstance(auth_context, (tuple, list)) else auth_context
    if isinstance(actor, Mapping):
        actor_id = actor.get("id")
    else:
        actor_id = getattr(actor, "id", None)
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise HTTPException(401, "Authenticated actor attribution is unavailable")
    return actor, actor_id.strip()


def _chronology(body: ChronologyBody) -> Chronology:
    try:
        return Chronology(
            observed_at=body.observed_at,
            recorded_at=body.recorded_at,
            committed_at=datetime.now(timezone.utc),
        )
    except FieldworkError as error:
        raise HTTPException(422, str(error)) from error


def _event_id(*parts: str) -> str:
    seed = "\x1f".join(parts)
    return f"fwapi_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"


def _record_title(record: Any) -> str:
    value = (
        record.get("title")
        if isinstance(record, Mapping)
        else getattr(record, "title", None)
    )
    return str(value or "Fieldwork record").strip()[:240] or "Fieldwork record"


def _version_manifest(
    metadata_source: Mapping[str, Any]
    | Callable[[Any], Mapping[str, Any] | VersionManifest],
    record: Any,
    *,
    scope_graph_version: int = 0,
) -> VersionManifest:
    raw = metadata_source(record) if callable(metadata_source) else metadata_source
    if isinstance(raw, VersionManifest):
        values = raw.as_dict()
    elif isinstance(raw, Mapping):
        values = dict(raw)
    else:
        raise RuntimeError("version_metadata must return a mapping or VersionManifest")
    values["scope_graph_version"] = scope_graph_version
    allowed = set(inspect.signature(VersionManifest).parameters)
    try:
        return VersionManifest(
            **{key: value for key, value in values.items() if key in allowed}
        )
    except (TypeError, ValueError, FieldworkError) as error:
        raise RuntimeError("Invalid fieldwork version metadata") from error


def _manifest(
    body: ManifestBody,
    versions: VersionManifest,
) -> EvidenceManifest:
    try:
        return EvidenceManifest(
            sensitivity=body.sensitivity,
            allowed_scales=tuple(body.allowed_scales),
            versions=versions,
            consent_basis=body.consent_basis,
            consent_subjects=tuple(body.consent_subjects),
            authorization_tags=tuple(body.authorization_tags),
            scope_node_ids=tuple(body.scope_node_ids),
        )
    except (TypeError, ValueError, FieldworkError) as error:
        raise HTTPException(422, str(error)) from error


def _consent_manifest(
    authority: ConsentAuthority, versions: VersionManifest
) -> EvidenceManifest:
    try:
        return EvidenceManifest(
            sensitivity=authority.sensitivity,
            allowed_scales=authority.allowed_scales,
            versions=versions,
            consent_basis="not_required",
            authorization_tags=tuple(sorted(authority.authorization_tags)),
            scope_node_ids=tuple(sorted(authority.scope_node_ids)),
        )
    except (TypeError, ValueError, FieldworkError) as error:
        raise HTTPException(422, str(error)) from error


def _source_refs(body: EntryBody) -> tuple[SourceRef, ...]:
    try:
        return tuple(
            SourceRef(
                source_id=item.source_id.strip(),
                version=item.version.strip(),
                locator=item.locator,
                content_sha256=item.content_sha256.lower(),
            )
            for item in body.source_refs
        )
    except FieldworkError as error:
        raise HTTPException(422, str(error)) from error


def _branch(ledger: FieldworkLedger, project_id: str, branch_id: str | None):
    resolved = branch_id or ledger.canonical_branch_id(project_id)
    branch = next(
        (item for item in ledger.branch_specs if item.branch_id == resolved), None
    )
    if branch is None or branch.project_id != project_id:
        raise HTTPException(404, "Fieldwork branch not found")
    return branch


def _load(store: FieldworkStore, project_id: str) -> FieldworkLedger:
    try:
        return store.load(project_id)
    except FieldworkError as error:
        if str(error) == "No persisted fieldwork project matched":
            raise HTTPException(
                404, "Fieldwork has not been opened for this record"
            ) from error
        raise


def _find_event(ledger: FieldworkLedger, event_id: str) -> FieldworkEvent | None:
    return next((event for event in ledger.events if event.event_id == event_id), None)


def _scope_version(ledger: FieldworkLedger, cycle_id: str, branch_id: str) -> int:
    versions = [
        int(event.payload["graph"]["version"])
        for event in ledger.effective_events(branch_id)
        if event.cycle_id == cycle_id
        and event.kind is EventKind.SCOPE_GRAPH_VERSIONED
        and isinstance(event.payload.get("graph"), Mapping)
    ]
    return max(versions, default=0)


def _serialize_event(event: FieldworkEvent) -> dict[str, Any]:
    return event.as_dict()


def _save_or_http(store: FieldworkStore, ledger: FieldworkLedger) -> None:
    try:
        store.save(ledger)
    except AppendOnlyViolation as error:
        raise HTTPException(409, str(error)) from error
    except FieldworkError as error:
        raise HTTPException(422, str(error)) from error


def _domain_http(error: FieldworkError) -> HTTPException:
    if isinstance(error, AuthorizationDenied):
        return HTTPException(403, str(error))
    if isinstance(error, AppendOnlyViolation):
        return HTTPException(409, str(error))
    message = str(error)
    status = 404 if "Unknown" in message or "not found" in message else 422
    return HTTPException(status, message)


def _audit_commit(
    dbs: Any,
    audit: Callable[..., None],
    event_type: str,
    *,
    actor_id: str,
    record_id: str,
    metadata: Mapping[str, Any],
) -> None:
    audit(
        dbs,
        event_type,
        actor=actor_id,
        entity_type="record",
        entity_id=record_id,
        metadata=dict(metadata),
    )
    commit = getattr(dbs, "commit", None)
    if callable(commit):
        commit()


def _authorization(
    resolver: Callable[..., AuthorizationContext],
    *,
    dbs: Any,
    auth_context: Any,
    record: Any,
    project_id: str,
    cycle_id: str,
    branch_id: str,
    actor_id: str,
    ledger: FieldworkLedger,
) -> AuthorizationContext:
    resolved = resolver(
        dbs,
        auth_context,
        record,
        project_id,
        cycle_id,
        branch_id,
        ledger,
    )
    if not isinstance(resolved, AuthorizationContext):
        raise HTTPException(500, "Fieldwork authorization callback failed closed")
    if resolved.principal_id != actor_id:
        raise HTTPException(
            403, "Fieldwork authorization principal does not match the actor"
        )
    return resolved


def _authorize_write(
    resolver: Callable[..., AuthorizationContext],
    *,
    dbs: Any,
    auth_context: Any,
    record: Any,
    project_id: str,
    cycle_id: str,
    branch_id: str,
    actor: ActorRef,
    ledger: FieldworkLedger,
    manifest: EvidenceManifest,
    layer: EpistemicLayer,
) -> AuthorizationContext:
    resolved = _authorization(
        resolver,
        dbs=dbs,
        auth_context=auth_context,
        record=record,
        project_id=project_id,
        cycle_id=cycle_id,
        branch_id=branch_id,
        actor_id=actor.actor_id,
        ledger=ledger,
    )
    try:
        for scale in manifest.allowed_scales:
            resolved.require_target(project_id, cycle_id, branch_id, scale)
        if (
            SENSITIVITY_RANK[manifest.sensitivity]
            > SENSITIVITY_RANK[resolved.max_sensitivity]
        ):
            raise AuthorizationDenied(
                "Principal cannot write evidence at the requested sensitivity"
            )
        if not set(manifest.authorization_tags).issubset(resolved.authorization_tags):
            raise AuthorizationDenied(
                "Principal cannot write evidence with the requested authorization tags"
            )
        if not set(manifest.scope_node_ids).issubset(resolved.scope_node_ids):
            raise AuthorizationDenied(
                "Principal cannot write evidence for the requested scope nodes"
            )
        if resolved.epistemic_layers and layer not in resolved.epistemic_layers:
            raise AuthorizationDenied(
                "Principal cannot write the requested epistemic layer"
            )
    except AuthorizationDenied as error:
        raise HTTPException(403, str(error)) from error
    return resolved


def _consent_authority(
    resolver: Callable[..., ConsentAuthority],
    *,
    dbs: Any,
    auth_context: Any,
    record: Any,
    project_id: str,
    cycle_id: str,
    branch_id: str,
    actor_id: str,
    ledger: FieldworkLedger,
) -> ConsentAuthority:
    """Resolve consent authority exclusively from trusted server state."""

    try:
        resolved = resolver(
            dbs,
            auth_context,
            record,
            project_id,
            cycle_id,
            branch_id,
            ledger,
        )
    except HTTPException:
        raise
    except (TypeError, ValueError, FieldworkError) as error:
        raise HTTPException(
            500, "Fieldwork consent authority callback failed closed"
        ) from error
    if not isinstance(resolved, ConsentAuthority):
        raise HTTPException(500, "Fieldwork consent authority callback failed closed")
    if resolved.principal_id != actor_id:
        raise HTTPException(
            403, "Fieldwork consent authority principal does not match the actor"
        )
    return resolved


def _authorize_consent_target(
    resolver: Callable[..., AuthorizationContext],
    *,
    dbs: Any,
    auth_context: Any,
    record: Any,
    project_id: str,
    cycle_id: str,
    branch_id: str,
    actor_id: str,
    ledger: FieldworkLedger,
    authority: ConsentAuthority,
) -> None:
    """Check record targets while leaving consent classification to policy.

    A participant may need to append a restricted opt-out even when their
    ordinary evidence-reading ceiling is internal.  The dedicated, trusted
    consent authority therefore owns sensitivity and actor role; general
    record authorization still owns project, cycle, branch, scale, tags, and
    scope.
    """

    resolved = _authorization(
        resolver,
        dbs=dbs,
        auth_context=auth_context,
        record=record,
        project_id=project_id,
        cycle_id=cycle_id,
        branch_id=branch_id,
        actor_id=actor_id,
        ledger=ledger,
    )
    try:
        for scale in authority.allowed_scales:
            resolved.require_target(project_id, cycle_id, branch_id, scale)
        if not authority.authorization_tags.issubset(resolved.authorization_tags):
            raise AuthorizationDenied(
                "Principal cannot write consent with the required authorization tags"
            )
        if not authority.scope_node_ids.issubset(resolved.scope_node_ids):
            raise AuthorizationDenied(
                "Principal cannot write consent for the required scope nodes"
            )
    except AuthorizationDenied as error:
        raise HTTPException(403, str(error)) from error


def create_fieldwork_router(
    *,
    db_dependency: Callable[..., Any],
    auth_dependency: Callable[..., Any],
    require_csrf: Callable[[Request, Any], None],
    record_access: Callable[[Any, str, str], Any],
    actor_role: Callable[..., str],
    audit: Callable[..., None],
    version_metadata: Mapping[str, Any]
    | Callable[[Any], Mapping[str, Any] | VersionManifest],
    store_factory: Callable[[], FieldworkStore],
    authorization_context: Callable[..., AuthorizationContext],
    consent_authority: Callable[..., ConsentAuthority],
) -> APIRouter:
    """Build the fieldwork router without importing application internals.

    ``authorization_context`` is called with ``(dbs, auth_result, record,
    project_id, cycle_id, branch_id, ledger)``.  It must derive permissions
    from trusted membership or policy state, never from the requested scale.
    ``consent_authority`` receives the same arguments and must additionally
    derive any authenticated participant binding and on-behalf-of authority.
    Returning no binding and no on-behalf-of authority denies consent writes.
    ``actor_role`` receives ``(dbs, auth_result, record, actor_id)`` after
    record access succeeds. It must derive a non-empty provenance role from
    trusted server state; request bodies never participate in attribution.
    """

    router = APIRouter(prefix="/api/records/{record_id}/fieldwork", tags=["fieldwork"])

    def request_context(record_id: str, dbs: Any, auth_result: Any):
        _actor_object, actor_id = _actor_identity(auth_result)
        record = record_access(dbs, actor_id, record_id)
        try:
            resolved_role = actor_role(dbs, auth_result, record, actor_id)
        except HTTPException:
            raise
        except (TypeError, ValueError, FieldworkError) as error:
            raise HTTPException(
                500, "Fieldwork actor-role callback failed closed"
            ) from error
        if not isinstance(resolved_role, str) or not resolved_role.strip():
            raise HTTPException(500, "Fieldwork actor-role callback failed closed")
        try:
            actor = ActorRef(actor_id, resolved_role.strip()[:80])
        except FieldworkError as error:
            raise HTTPException(
                500, "Fieldwork actor-role callback failed closed"
            ) from error
        return actor, record

    @router.post("/cycles", status_code=201)
    def create_cycle(
        record_id: str,
        body: CycleCreateBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        actor, record = request_context(record_id, dbs, auth_result)
        actor_id = actor.actor_id
        require_csrf(request, dbs)
        store = store_factory()
        try:
            ledger = store.load(record_id)
        except FieldworkError as error:
            if str(error) != "No persisted fieldwork project matched":
                raise _domain_http(error) from error
            ledger = FieldworkLedger()
            project_manifest = EvidenceManifest(
                sensitivity=Sensitivity.INTERNAL,
                allowed_scales=(AccessScale.ORGANIZATION,),
                versions=_version_manifest(version_metadata, record),
            )
            try:
                ledger.create_project(
                    record_id,
                    _record_title(record),
                    _chronology(body),
                    project_manifest,
                    canonical_branch_id=f"{record_id}:canonical",
                    actor=actor,
                )
            except FieldworkError as domain_error:
                raise _domain_http(domain_error) from domain_error
        cycle_id = body.cycle_id or f"cycle_{uuid.uuid4().hex}"
        cycle_manifest = EvidenceManifest(
            sensitivity=Sensitivity.INTERNAL,
            allowed_scales=(AccessScale.ORGANIZATION,),
            versions=_version_manifest(version_metadata, record),
        )
        try:
            event = ledger.open_cycle(
                record_id,
                cycle_id,
                body.label.strip(),
                _chronology(body),
                cycle_manifest,
                actor=actor,
            )
        except FieldworkError as error:
            raise _domain_http(error) from error
        _authorize_write(
            authorization_context,
            dbs=dbs,
            auth_context=auth_result,
            record=record,
            project_id=record_id,
            cycle_id=cycle_id,
            branch_id=ledger.canonical_branch_id(record_id),
            actor=actor,
            ledger=ledger,
            manifest=cycle_manifest,
            layer=EpistemicLayer.RESEARCHER_RECORD,
        )
        _save_or_http(store, ledger)
        _audit_commit(
            dbs,
            audit,
            "fieldwork.cycle_opened",
            actor_id=actor_id,
            record_id=record_id,
            metadata={"cycle_id": cycle_id, "fieldwork_event_id": event.event_id},
        )
        return {
            "cycle": {
                "cycle_id": cycle_id,
                "label": body.label.strip(),
                "branch_id": ledger.canonical_branch_id(record_id),
                "opened_by": actor_id,
                "event": _serialize_event(event),
            }
        }

    @router.get("/cycles")
    def list_cycles(
        record_id: str,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        _actor_ref, _record = request_context(record_id, dbs, auth_result)
        try:
            ledger = store_factory().load(record_id)
        except FieldworkError as error:
            if str(error) == "No persisted fieldwork project matched":
                return {"cycles": []}
            raise _domain_http(error) from error
        opened = {
            str(event.cycle_id): event
            for event in ledger.events
            if event.kind is EventKind.CYCLE_OPENED
        }
        return {
            "cycles": [
                {
                    "cycle_id": cycle_id,
                    "label": str(opened[cycle_id].payload["label"]),
                    "opened_at": opened[cycle_id].chronology.committed_at.isoformat(),
                    "event_id": opened[cycle_id].event_id,
                }
                for project_id, cycle_id in ledger.cycles
                if project_id == record_id and cycle_id in opened
            ]
        }

    def append_entry(
        spec: EntrySpec,
        *,
        record_id: str,
        cycle_id: str,
        body: EntryBody,
        request: Request,
        dbs: Any,
        auth_result: Any,
    ) -> dict[str, Any]:
        actor, record = request_context(record_id, dbs, auth_result)
        actor_id = actor.actor_id
        require_csrf(request, dbs)
        store = store_factory()
        ledger = _load(store, record_id)
        branch = _branch(ledger, record_id, body.branch_id)
        if branch.cycle_id is not None and branch.cycle_id != cycle_id:
            raise HTTPException(404, "Fieldwork branch is outside this cycle")
        if (record_id, cycle_id) not in ledger.cycles:
            raise HTTPException(404, "Fieldwork cycle not found")
        if spec.requires_cause and not body.causal_event_ids:
            raise HTTPException(
                422, f"{spec.entry_type} entries require causal_event_ids"
            )
        if spec.requires_consent_subject and (
            body.consent_basis not in {"granted", "pending"}
            or not body.consent_subjects
        ):
            raise HTTPException(
                422,
                f"{spec.entry_type} entries require granted or pending consent subjects",
            )
        layer = (
            EpistemicLayer.COUNTERFACTUAL
            if branch.mode is BranchMode.COUNTERFACTUAL
            else spec.layer
        )
        assigned_id = _event_id(
            record_id,
            cycle_id,
            branch.branch_id,
            spec.entry_type,
            actor_id,
            body.idempotency_key,
        )
        payload = {
            "entry_type": spec.entry_type,
            "content": body.content,
        }
        if branch.mode is BranchMode.COUNTERFACTUAL:
            payload["represented_epistemic_layer"] = spec.layer.value
            payload["simulation_only"] = True
        versions = _version_manifest(
            version_metadata,
            record,
            scope_graph_version=_scope_version(ledger, cycle_id, branch.branch_id),
        )
        manifest = _manifest(body, versions)
        sources = _source_refs(body)
        _authorize_write(
            authorization_context,
            dbs=dbs,
            auth_context=auth_result,
            record=record,
            project_id=record_id,
            cycle_id=cycle_id,
            branch_id=branch.branch_id,
            actor=actor,
            ledger=ledger,
            manifest=manifest,
            layer=layer,
        )
        existing = _find_event(ledger, assigned_id)
        if existing is not None:
            expected = {
                "kind": spec.kind.value,
                "layer": layer.value,
                "payload": payload,
                "causes": list(body.causal_event_ids),
                "sources": [source.as_dict() for source in sources],
                "manifest": manifest.as_dict(),
                "actor": actor.as_dict(),
            }
            actual = {
                "kind": existing.kind.value,
                "layer": existing.epistemic_layer.value,
                "payload": existing.payload,
                "causes": list(existing.causal_event_ids),
                "sources": [source.as_dict() for source in existing.source_refs],
                "manifest": existing.manifest.as_dict(),
                "actor": existing.actor.as_dict(),
            }
            if canonical_json(actual) != canonical_json(expected):
                raise HTTPException(
                    409, "Idempotency key was already used for other content"
                )
            event = existing
            created = False
        else:
            try:
                event = ledger.append(
                    project_id=record_id,
                    cycle_id=cycle_id,
                    branch_id=branch.branch_id,
                    kind=spec.kind,
                    epistemic_layer=layer,
                    chronology=_chronology(body),
                    payload=payload,
                    manifest=manifest,
                    actor=actor,
                    causal_event_ids=body.causal_event_ids,
                    source_refs=sources,
                    event_id=assigned_id,
                )
            except FieldworkError as error:
                raise _domain_http(error) from error
            _save_or_http(store, ledger)
            created = True
            _audit_commit(
                dbs,
                audit,
                f"fieldwork.{spec.entry_type}_appended",
                actor_id=actor_id,
                record_id=record_id,
                metadata={
                    "cycle_id": cycle_id,
                    "branch_id": branch.branch_id,
                    "fieldwork_event_id": event.event_id,
                    "canonical_effect": event.canonical_effect,
                },
            )
        return {"event": _serialize_event(event), "created": created}

    def entry_endpoint(spec: EntrySpec):
        def endpoint(
            record_id: str,
            cycle_id: str,
            body: EntryBody,
            request: Request,
            dbs: Any = Depends(db_dependency),
            auth_result: Any = Depends(auth_dependency),
        ):
            return append_entry(
                spec,
                record_id=record_id,
                cycle_id=cycle_id,
                body=body,
                request=request,
                dbs=dbs,
                auth_result=auth_result,
            )

        endpoint.__name__ = f"append_fieldwork_{spec.entry_type}"
        endpoint.__doc__ = f"Append a typed {spec.entry_type.replace('_', ' ')} entry."
        return endpoint

    for entry_spec in ENTRY_SPECS:
        router.add_api_route(
            f"/cycles/{{cycle_id}}/{entry_spec.slug}",
            entry_endpoint(entry_spec),
            methods=["POST"],
            status_code=201,
            name=f"append-fieldwork-{entry_spec.entry_type}",
        )

    @router.post("/cycles/{cycle_id}/scope-graphs", status_code=201)
    def version_scope_graph(
        record_id: str,
        cycle_id: str,
        body: ScopeGraphBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        actor, record = request_context(record_id, dbs, auth_result)
        actor_id = actor.actor_id
        require_csrf(request, dbs)
        store = store_factory()
        ledger = _load(store, record_id)
        branch = _branch(ledger, record_id, body.branch_id)
        if branch.cycle_id is not None and branch.cycle_id != cycle_id:
            raise HTTPException(404, "Fieldwork branch is outside this cycle")
        try:
            graph = ScopeGraph(
                version=body.version,
                nodes=tuple(
                    ScopeNode.build(
                        node.node_id, node.kind, node.label, node.attributes
                    )
                    for node in body.nodes
                ),
                edges=tuple(
                    ScopeEdge(edge.source, edge.target, edge.relation.strip())
                    for edge in body.edges
                ),
            )
        except FieldworkError as error:
            raise _domain_http(error) from error
        edge_keys = {(edge.source, edge.target, edge.relation) for edge in graph.edges}
        if len(edge_keys) != len(graph.edges):
            raise HTTPException(422, "Scope graph edges must be unique")
        assigned_id = _event_id(
            record_id,
            cycle_id,
            branch.branch_id,
            "scope_graph",
            actor_id,
            body.idempotency_key,
        )
        manifest = _manifest(
            body,
            _version_manifest(
                version_metadata, record, scope_graph_version=graph.version
            ),
        )
        layer = (
            EpistemicLayer.COUNTERFACTUAL
            if branch.mode is BranchMode.COUNTERFACTUAL
            else EpistemicLayer.RESEARCHER_RECORD
        )
        payload = {"graph": graph.as_dict()}
        _authorize_write(
            authorization_context,
            dbs=dbs,
            auth_context=auth_result,
            record=record,
            project_id=record_id,
            cycle_id=cycle_id,
            branch_id=branch.branch_id,
            actor=actor,
            ledger=ledger,
            manifest=manifest,
            layer=layer,
        )
        existing = _find_event(ledger, assigned_id)
        if existing is not None:
            if (
                existing.kind is not EventKind.SCOPE_GRAPH_VERSIONED
                or existing.epistemic_layer is not layer
                or existing.payload != payload
                or existing.causal_event_ids != body.causal_event_ids
                or existing.manifest.as_dict() != manifest.as_dict()
                or existing.actor != actor
            ):
                raise HTTPException(
                    409, "Idempotency key was already used for another graph"
                )
            return {
                "event": _serialize_event(existing),
                "graph": graph.as_dict(),
                "created": False,
            }
        expected_version = _scope_version(ledger, cycle_id, branch.branch_id) + 1
        if body.version != expected_version:
            raise HTTPException(
                409, f"The next scope graph version is {expected_version}"
            )
        try:
            event = ledger.append(
                project_id=record_id,
                cycle_id=cycle_id,
                branch_id=branch.branch_id,
                kind=EventKind.SCOPE_GRAPH_VERSIONED,
                epistemic_layer=layer,
                chronology=_chronology(body),
                payload=payload,
                manifest=manifest,
                actor=actor,
                causal_event_ids=body.causal_event_ids,
                event_id=assigned_id,
            )
        except FieldworkError as error:
            raise _domain_http(error) from error
        _save_or_http(store, ledger)
        _audit_commit(
            dbs,
            audit,
            "fieldwork.scope_graph_versioned",
            actor_id=actor_id,
            record_id=record_id,
            metadata={
                "cycle_id": cycle_id,
                "branch_id": branch.branch_id,
                "graph_version": graph.version,
                "fieldwork_event_id": event.event_id,
                "canonical_effect": event.canonical_effect,
            },
        )
        return {
            "event": _serialize_event(event),
            "graph": graph.as_dict(),
            "created": True,
        }

    def change_consent(
        granted: bool,
        *,
        record_id: str,
        cycle_id: str,
        body: ConsentBody,
        request: Request,
        dbs: Any,
        auth_result: Any,
    ) -> dict[str, Any]:
        actor, record = request_context(record_id, dbs, auth_result)
        actor_id = actor.actor_id
        require_csrf(request, dbs)
        store = store_factory()
        ledger = _load(store, record_id)
        branch_id = ledger.canonical_branch_id(record_id)
        if (record_id, cycle_id) not in ledger.cycles:
            raise HTTPException(404, "Fieldwork cycle not found")
        authority = _consent_authority(
            consent_authority,
            dbs=dbs,
            auth_context=auth_result,
            record=record,
            project_id=record_id,
            cycle_id=cycle_id,
            branch_id=branch_id,
            actor_id=actor_id,
            ledger=ledger,
        )
        if (
            authority.bound_subject_id != body.subject_id
            and not authority.can_act_for_other_subjects
        ):
            raise HTTPException(
                403,
                "Authenticated actor is not authorized for this consent subject",
            )
        consent_actor = ActorRef(actor_id, authority.actor_role)
        kind = EventKind.CONSENT_GRANTED if granted else EventKind.CONSENT_WITHDRAWN
        assigned_id = _event_id(
            record_id,
            cycle_id,
            branch_id,
            kind.value,
            actor_id,
            body.idempotency_key,
        )
        payload = {
            "subject_id": body.subject_id,
            "reason": body.reason,
        }
        manifest = _consent_manifest(
            authority,
            _version_manifest(
                version_metadata,
                record,
                scope_graph_version=_scope_version(ledger, cycle_id, branch_id),
            ),
        )
        _authorize_consent_target(
            authorization_context,
            dbs=dbs,
            auth_context=auth_result,
            record=record,
            project_id=record_id,
            cycle_id=cycle_id,
            branch_id=branch_id,
            actor_id=actor_id,
            ledger=ledger,
            authority=authority,
        )
        existing = _find_event(ledger, assigned_id)
        if existing is not None:
            if (
                existing.kind is not kind
                or existing.payload != payload
                or existing.manifest.as_dict() != manifest.as_dict()
                or existing.actor != consent_actor
            ):
                raise HTTPException(
                    409, "Idempotency key was already used for another consent event"
                )
            event = existing
            created = False
        else:
            try:
                event = ledger.append(
                    project_id=record_id,
                    cycle_id=cycle_id,
                    branch_id=branch_id,
                    kind=kind,
                    epistemic_layer=EpistemicLayer.RESEARCHER_RECORD,
                    chronology=_chronology(body),
                    payload=payload,
                    manifest=manifest,
                    actor=consent_actor,
                    event_id=assigned_id,
                )
            except FieldworkError as error:
                raise _domain_http(error) from error
            _save_or_http(store, ledger)
            created = True
            _audit_commit(
                dbs,
                audit,
                "fieldwork.consent_granted"
                if granted
                else "fieldwork.consent_withdrawn",
                actor_id=actor_id,
                record_id=record_id,
                metadata={
                    "cycle_id": cycle_id,
                    "fieldwork_event_id": event.event_id,
                },
            )
        return {"event": _serialize_event(event), "created": created}

    @router.post("/cycles/{cycle_id}/consent/grants", status_code=201)
    def grant_consent(
        record_id: str,
        cycle_id: str,
        body: ConsentBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        return change_consent(
            True,
            record_id=record_id,
            cycle_id=cycle_id,
            body=body,
            request=request,
            dbs=dbs,
            auth_result=auth_result,
        )

    @router.post("/cycles/{cycle_id}/consent/withdrawals", status_code=201)
    def withdraw_consent(
        record_id: str,
        cycle_id: str,
        body: ConsentBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        return change_consent(
            False,
            record_id=record_id,
            cycle_id=cycle_id,
            body=body,
            request=request,
            dbs=dbs,
            auth_result=auth_result,
        )

    @router.post("/cycles/{cycle_id}/branches", status_code=201)
    def fork_branch(
        record_id: str,
        cycle_id: str,
        body: BranchForkBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        actor, record = request_context(record_id, dbs, auth_result)
        actor_id = actor.actor_id
        require_csrf(request, dbs)
        store = store_factory()
        ledger = _load(store, record_id)
        parent_id = body.parent_branch_id or ledger.canonical_branch_id(record_id)
        parent = _branch(ledger, record_id, parent_id)
        mode = BranchMode(body.mode.value)
        generated_branch_id = (
            f"{record_id}:{mode.value}:"
            f"{hashlib.sha256((actor_id + ':' + body.idempotency_key).encode()).hexdigest()[:24]}"
        )
        branch_id = body.branch_id or generated_branch_id
        fork_manifest = EvidenceManifest(
            sensitivity=Sensitivity.INTERNAL,
            allowed_scales=(AccessScale.ORGANIZATION,),
            versions=_version_manifest(
                version_metadata,
                record,
                scope_graph_version=_scope_version(ledger, cycle_id, parent.branch_id),
            ),
        )
        _authorize_write(
            authorization_context,
            dbs=dbs,
            auth_context=auth_result,
            record=record,
            project_id=record_id,
            cycle_id=cycle_id,
            branch_id=parent.branch_id,
            actor=actor,
            ledger=ledger,
            manifest=fork_manifest,
            layer=(
                EpistemicLayer.COUNTERFACTUAL
                if mode is BranchMode.COUNTERFACTUAL
                else EpistemicLayer.INTERPRETATION
            ),
        )
        existing_branch = next(
            (item for item in ledger.branch_specs if item.branch_id == branch_id), None
        )
        if existing_branch is not None:
            if (
                existing_branch.project_id != record_id
                or existing_branch.cycle_id != cycle_id
                or existing_branch.mode is not mode
                or existing_branch.parent_branch_id != parent.branch_id
                or existing_branch.base_event_id != body.base_event_id
            ):
                raise HTTPException(
                    409, "Branch id or idempotency key is already in use"
                )
            fork_event = next(
                event
                for event in ledger.events
                if event.branch_id == existing_branch.branch_id
                and event.kind is EventKind.BRANCH_FORKED
            )
            if (
                fork_event.actor != actor
                or fork_event.payload.get("rationale") != body.rationale
            ):
                raise HTTPException(
                    409, "Idempotency key was already used for another branch"
                )
            return {
                "branch": {
                    "branch_id": existing_branch.branch_id,
                    "mode": existing_branch.mode.value,
                    "parent_branch_id": existing_branch.parent_branch_id,
                    "base_event_id": existing_branch.base_event_id,
                    "canonical_writes": False,
                    "forked_by": actor_id,
                },
                "event": _serialize_event(fork_event),
                "created": False,
            }
        try:
            event = ledger.fork(
                project_id=record_id,
                cycle_id=cycle_id,
                branch_id=branch_id,
                parent_branch_id=parent.branch_id,
                base_event_id=body.base_event_id,
                mode=mode,
                chronology=_chronology(body),
                manifest=fork_manifest,
                rationale=body.rationale,
                actor=actor,
                event_id=_event_id(
                    record_id,
                    cycle_id,
                    branch_id,
                    "fork",
                    actor_id,
                    body.idempotency_key,
                ),
            )
        except FieldworkError as error:
            raise _domain_http(error) from error
        _save_or_http(store, ledger)
        _audit_commit(
            dbs,
            audit,
            "fieldwork.branch_forked",
            actor_id=actor_id,
            record_id=record_id,
            metadata={
                "cycle_id": cycle_id,
                "branch_id": branch_id,
                "mode": mode.value,
                "base_event_id": body.base_event_id,
                "fieldwork_event_id": event.event_id,
                "canonical_effect": False,
            },
        )
        return {
            "branch": {
                "branch_id": branch_id,
                "mode": mode.value,
                "parent_branch_id": parent.branch_id,
                "base_event_id": body.base_event_id,
                "canonical_writes": False,
                "forked_by": actor_id,
            },
            "event": _serialize_event(event),
            "created": True,
        }

    @router.get("/cycles/{cycle_id}/replay")
    def replay_projection(
        record_id: str,
        cycle_id: str,
        scale: AccessScale = Query(...),
        branch_id: str | None = Query(default=None, min_length=1, max_length=160),
        as_of_event_id: str | None = Query(default=None, min_length=1, max_length=160),
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        actor, record = request_context(record_id, dbs, auth_result)
        actor_id = actor.actor_id
        ledger = _load(store_factory(), record_id)
        branch = _branch(ledger, record_id, branch_id)
        authz = _authorization(
            authorization_context,
            dbs=dbs,
            auth_context=auth_result,
            record=record,
            project_id=record_id,
            cycle_id=cycle_id,
            branch_id=branch.branch_id,
            actor_id=actor_id,
            ledger=ledger,
        )
        try:
            projection = ledger.project(
                project_id=record_id,
                cycle_id=cycle_id,
                branch_id=branch.branch_id,
                auth=authz,
                scale=scale,
                as_of_event_id=as_of_event_id,
            )
        except FieldworkError as error:
            raise _domain_http(error) from error
        return {
            "replay": {
                "mode": "stored_ledger_projection",
                "as_of_event_id": as_of_event_id,
                "selected_scale": scale.value,
                "stored_evidence_exact": True,
                "model_regenerated": False,
            },
            "state_hash": projection.state_hash,
            "projection": projection.state,
        }

    @router.post("/cycles/{cycle_id}/outputs", status_code=201)
    def store_output(
        record_id: str,
        cycle_id: str,
        body: StoredOutputBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        actor, record = request_context(record_id, dbs, auth_result)
        actor_id = actor.actor_id
        require_csrf(request, dbs)
        store = store_factory()
        ledger = _load(store, record_id)
        branch = _branch(ledger, record_id, body.branch_id)
        if branch.cycle_id is not None and branch.cycle_id != cycle_id:
            raise HTTPException(404, "Fieldwork branch is outside this cycle")
        if (record_id, cycle_id) not in ledger.cycles:
            raise HTTPException(404, "Fieldwork cycle not found")
        manifest = _manifest(
            body,
            _version_manifest(
                version_metadata,
                record,
                scope_graph_version=_scope_version(ledger, cycle_id, branch.branch_id),
            ),
        )
        layer = (
            EpistemicLayer.COUNTERFACTUAL
            if branch.mode is BranchMode.COUNTERFACTUAL
            else EpistemicLayer.SYNTHESIS
        )
        authz = _authorize_write(
            authorization_context,
            dbs=dbs,
            auth_context=auth_result,
            record=record,
            project_id=record_id,
            cycle_id=cycle_id,
            branch_id=branch.branch_id,
            actor=actor,
            ledger=ledger,
            manifest=manifest,
            layer=layer,
        )
        try:
            ledger.validate_output_derivation(
                project_id=record_id,
                cycle_id=cycle_id,
                branch_id=branch.branch_id,
                input_event_ids=body.input_event_ids,
                manifest=manifest,
                auth=authz,
            )
        except FieldworkError as error:
            raise _domain_http(error) from error
        assigned_id = _event_id(
            record_id,
            cycle_id,
            branch.branch_id,
            "stored_output",
            actor_id,
            body.idempotency_key,
        )
        expected_payload = {
            "output_id": body.output_id,
            "content": body.content,
            "stored_output_hash": hashlib.sha256(
                canonical_json(body.content).encode("utf-8")
            ).hexdigest(),
            "input_event_ids": list(body.input_event_ids),
            "generator": body.generator.strip(),
        }
        existing = _find_event(ledger, assigned_id)
        if existing is not None:
            if (
                existing.kind is not EventKind.OUTPUT_STORED
                or existing.epistemic_layer is not layer
                or existing.payload != expected_payload
                or existing.manifest.as_dict() != manifest.as_dict()
                or existing.actor != actor
            ):
                raise HTTPException(
                    409, "Idempotency key was already used for another output"
                )
            event = existing
            created = False
        else:
            conflicting_output = next(
                (
                    event
                    for event in ledger.effective_events(branch.branch_id)
                    if event.cycle_id == cycle_id
                    and event.kind is EventKind.OUTPUT_STORED
                    and event.payload.get("output_id") == body.output_id
                ),
                None,
            )
            if conflicting_output is not None:
                raise HTTPException(
                    409, "output_id already exists in this branch history"
                )
            try:
                event = ledger.store_output(
                    project_id=record_id,
                    cycle_id=cycle_id,
                    branch_id=branch.branch_id,
                    output_id=body.output_id,
                    content=body.content,
                    input_event_ids=body.input_event_ids,
                    chronology=_chronology(body),
                    manifest=manifest,
                    generator=body.generator.strip(),
                    auth=authz,
                    actor=actor,
                    event_id=assigned_id,
                )
            except FieldworkError as error:
                raise _domain_http(error) from error
            _save_or_http(store, ledger)
            created = True
            _audit_commit(
                dbs,
                audit,
                "fieldwork.output_stored",
                actor_id=actor_id,
                record_id=record_id,
                metadata={
                    "cycle_id": cycle_id,
                    "branch_id": branch.branch_id,
                    "output_id": body.output_id,
                    "fieldwork_event_id": event.event_id,
                    "canonical_effect": event.canonical_effect,
                },
            )
        return {
            "event": _serialize_event(event),
            "output": {
                "output_id": body.output_id,
                "origin_event_id": event.event_id,
                "stored_output_hash": event.payload["stored_output_hash"],
                "generator_version": event.payload["generator"],
                "exact_replay_available": True,
            },
            "created": created,
        }

    @router.get("/cycles/{cycle_id}/outputs/{output_id}")
    def retrieve_stored_output(
        record_id: str,
        cycle_id: str,
        output_id: str,
        scale: AccessScale = Query(...),
        branch_id: str | None = Query(default=None, min_length=1, max_length=160),
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        actor, record = request_context(record_id, dbs, auth_result)
        actor_id = actor.actor_id
        ledger = _load(store_factory(), record_id)
        branch = _branch(ledger, record_id, branch_id)
        authz = _authorization(
            authorization_context,
            dbs=dbs,
            auth_context=auth_result,
            record=record,
            project_id=record_id,
            cycle_id=cycle_id,
            branch_id=branch.branch_id,
            actor_id=actor_id,
            ledger=ledger,
        )
        try:
            output = ledger.replay_output(
                project_id=record_id,
                cycle_id=cycle_id,
                branch_id=branch.branch_id,
                output_id=output_id,
                auth=authz,
                scale=scale,
                mode=ReplayMode.STORED,
            )
        except FieldworkError as error:
            raise _domain_http(error) from error
        return {
            "output": {
                "output_id": output.output_id,
                "origin_event_id": output.origin_event_id,
                "content": output.content,
                "output_hash": output.output_hash,
                "stored_output_hash": output.stored_output_hash,
                "generator_version": output.generator_version,
                "replay_mode": output.mode.value,
                "exact_replay": True,
                "regenerated": False,
                "persisted": output.persisted,
            }
        }

    return router


__all__ = [
    "BranchForkBody",
    "ConsentAuthority",
    "ConsentBody",
    "CycleCreateBody",
    "ENTRY_SPECS",
    "EntryBody",
    "ScopeGraphBody",
    "StoredOutputBody",
    "create_fieldwork_router",
]
