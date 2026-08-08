"""Replayable, event-sourced core for governed ethnographic fieldwork.

This module is deliberately isolated from the web application and ORM.  The
event stream is the source of truth: projections, historical views, and
counterfactuals are derived without updating or deleting canonical evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence


class FieldworkError(ValueError):
    """Base error for an invalid fieldwork operation."""


class AuthorizationDenied(FieldworkError):
    """Raised when a projection or output exceeds the caller's grant."""


class AppendOnlyViolation(FieldworkError):
    """Raised when an operation would alter canonical history."""


class BranchMode(str, Enum):
    CANONICAL = "canonical"
    HISTORICAL = "historical"
    COUNTERFACTUAL = "counterfactual"


class EpistemicLayer(str, Enum):
    OBSERVATION = "observation"
    PARTICIPANT_ACCOUNT = "participant_account"
    RESEARCHER_RECORD = "researcher_record"
    REFLEXIVE_MEMO = "reflexive_memo"
    POSITIONALITY = "positionality"
    MEMBER_CHECK = "member_check"
    INTERPRETATION = "interpretation"
    SYNTHESIS = "synthesis"
    DECISION = "decision"
    INTERVENTION = "intervention"
    AFTER_EFFECT = "after_effect"
    COUNTERFACTUAL = "counterfactual"


class Sensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"
    SENSITIVE = "sensitive"


SENSITIVITY_RANK = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.INTERNAL: 1,
    Sensitivity.RESTRICTED: 2,
    Sensitivity.SENSITIVE: 3,
}


class AccessScale(str, Enum):
    """Explicit disclosure contexts; no scale silently implies another."""

    INDIVIDUAL = "individual"
    ENCOUNTER = "encounter"
    CASE = "case"
    PARTICIPANT = "participant"
    TEAM = "team"
    SITE = "site"
    PROGRAM = "program"
    ORGANIZATION = "organization"
    COHORT = "cohort"
    NETWORK = "network"
    ECOSYSTEM = "ecosystem"
    PUBLIC = "public"


class ScopeKind(str, Enum):
    """Non-flattening kinds for nodes in a situated fieldwork graph."""

    ENCOUNTER = "encounter"
    CASE = "case"
    PARTICIPANT = "participant"
    TEAM = "team"
    SITE = "site"
    PROGRAM = "program"
    ORGANIZATION = "organization"
    COHORT = "cohort"
    NETWORK = "network"
    ECOSYSTEM = "ecosystem"
    PUBLIC = "public"


class EventKind(str, Enum):
    PROJECT_CREATED = "project.created"
    CYCLE_OPENED = "cycle.opened"
    BRANCH_FORKED = "branch.forked"
    SCOPE_GRAPH_VERSIONED = "scope_graph.versioned"
    OBSERVATION_RECORDED = "observation.recorded"
    INTERPRETATION_COMMITTED = "interpretation.committed"
    DECISION_COMMITTED = "decision.committed"
    CONSENT_GRANTED = "consent.granted"
    CONSENT_WITHDRAWN = "consent.withdrawn"
    OUTPUT_STORED = "output.stored"


class ReplayMode(str, Enum):
    STORED = "stored"
    REGENERATE = "regenerate"


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FieldworkError("Fieldwork timestamps must include a UTC offset")
    return value.isoformat(timespec="microseconds")


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=str)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Fieldwork values must be JSON-compatible, got {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return the only JSON encoding used in event and projection hashes."""

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Chronology:
    """Three distinct moments: occurrence, inscription, and append-only commit."""

    observed_at: datetime
    recorded_at: datetime
    committed_at: datetime

    def __post_init__(self) -> None:
        for value in (self.observed_at, self.recorded_at, self.committed_at):
            _timestamp(value)
        if not self.observed_at <= self.recorded_at <= self.committed_at:
            raise FieldworkError(
                "Chronology must satisfy observed_at <= recorded_at <= committed_at"
            )

    def as_dict(self) -> dict[str, str]:
        return {
            "observed_at": _timestamp(self.observed_at),
            "recorded_at": _timestamp(self.recorded_at),
            "committed_at": _timestamp(self.committed_at),
        }


@dataclass(frozen=True)
class ActorRef:
    """Stable provenance for the human, participant, agent, or service acting."""

    actor_id: str
    actor_role: str

    def __post_init__(self) -> None:
        if not self.actor_id.strip() or not self.actor_role.strip():
            raise FieldworkError("Actors require a stable id and an explicit role")

    def as_dict(self) -> dict[str, str]:
        return {"actor_id": self.actor_id, "actor_role": self.actor_role}


SYSTEM_ACTOR = ActorRef("system", "system")


@dataclass(frozen=True)
class SourceRef:
    source_id: str
    version: str
    locator: str = ""
    content_sha256: str = ""

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.version.strip():
            raise FieldworkError("Source references require an id and version")
        if self.content_sha256 and (
            len(self.content_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.content_sha256.lower())
        ):
            raise FieldworkError("Source content hashes must be SHA-256 hex")

    def as_dict(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "version": self.version,
            "locator": self.locator,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class VersionManifest:
    schema_version: str = "fieldwork.event.v1"
    app_version: str = "local"
    policy_version: str = "fieldwork-policy.v1"
    consent_version: str = "consent.v1"
    scope_graph_version: int = 0
    prompt_version: str = "none"
    model_version: str = "none"

    def __post_init__(self) -> None:
        if self.scope_graph_version < 0:
            raise FieldworkError("Scope graph versions cannot be negative")
        for value in (
            self.schema_version,
            self.app_version,
            self.policy_version,
            self.consent_version,
            self.prompt_version,
            self.model_version,
        ):
            if not str(value).strip():
                raise FieldworkError("Version manifest values cannot be blank")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "app_version": self.app_version,
            "policy_version": self.policy_version,
            "consent_version": self.consent_version,
            "scope_graph_version": self.scope_graph_version,
            "prompt_version": self.prompt_version,
            "model_version": self.model_version,
        }


@dataclass(frozen=True)
class EvidenceManifest:
    """Consent, sensitivity, audience, and software versions for one event."""

    sensitivity: Sensitivity
    allowed_scales: tuple[AccessScale, ...]
    versions: VersionManifest = field(default_factory=VersionManifest)
    consent_basis: str = "not_required"
    consent_subjects: tuple[str, ...] = ()
    authorization_tags: tuple[str, ...] = ()
    scope_node_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.allowed_scales:
            raise FieldworkError("At least one access scale is required")
        if len(set(self.allowed_scales)) != len(self.allowed_scales):
            raise FieldworkError("Access scales must be unique")
        if self.consent_basis not in {"not_required", "granted", "pending"}:
            raise FieldworkError("Consent basis must be not_required, granted, or pending")
        if self.consent_basis != "not_required" and not self.consent_subjects:
            raise FieldworkError("Consent-bound evidence requires at least one subject reference")
        if any(not subject.strip() for subject in self.consent_subjects):
            raise FieldworkError("Consent subject references cannot be blank")
        if len(set(self.scope_node_ids)) != len(self.scope_node_ids):
            raise FieldworkError("Scope node references must be unique")
        if any(not scope_id.strip() for scope_id in self.scope_node_ids):
            raise FieldworkError("Scope node references cannot be blank")

    def as_dict(self, *, reveal_subjects: bool = True) -> dict[str, Any]:
        return {
            "sensitivity": self.sensitivity.value,
            "allowed_scales": sorted(scale.value for scale in self.allowed_scales),
            "versions": self.versions.as_dict(),
            "consent_basis": self.consent_basis,
            "consent_subjects": list(self.consent_subjects) if reveal_subjects else [],
            "authorization_tags": list(self.authorization_tags),
            "scope_node_ids": list(self.scope_node_ids) if reveal_subjects else [],
        }


@dataclass(frozen=True)
class ScopeNode:
    node_id: str
    kind: ScopeKind
    label: str
    attributes_json: str = "{}"

    def __post_init__(self) -> None:
        if not self.node_id.strip() or not self.label.strip():
            raise FieldworkError("Scope nodes require an id and label")
        if not isinstance(self.kind, ScopeKind):
            try:
                object.__setattr__(self, "kind", ScopeKind(self.kind))
            except ValueError as error:
                raise FieldworkError(f"Unknown scope kind: {self.kind}") from error
        try:
            attributes = json.loads(self.attributes_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise FieldworkError("Scope node attributes must be JSON") from error
        if not isinstance(attributes, dict):
            raise FieldworkError("Scope node attributes must be a JSON object")

    @classmethod
    def build(
        cls,
        node_id: str,
        kind: ScopeKind | str,
        label: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> "ScopeNode":
        try:
            resolved_kind = kind if isinstance(kind, ScopeKind) else ScopeKind(kind)
        except ValueError as error:
            raise FieldworkError(f"Unknown scope kind: {kind}") from error
        return cls(node_id, resolved_kind, label, canonical_json(attributes or {}))

    @property
    def attributes(self) -> dict[str, Any]:
        return json.loads(self.attributes_json)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "kind": self.kind.value,
            "label": self.label,
            "attributes": self.attributes,
        }


@dataclass(frozen=True)
class ScopeEdge:
    source: str
    target: str
    relation: str

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "target": self.target, "relation": self.relation}


@dataclass(frozen=True)
class ScopeGraph:
    version: int
    nodes: tuple[ScopeNode, ...]
    edges: tuple[ScopeEdge, ...] = ()

    def __post_init__(self) -> None:
        if self.version < 1:
            raise FieldworkError("Scope graph versions start at 1")
        node_ids = [node.node_id for node in self.nodes]
        if any(not node_id.strip() for node_id in node_ids) or len(node_ids) != len(set(node_ids)):
            raise FieldworkError("Scope graph node ids must be non-empty and unique")
        known = set(node_ids)
        for edge in self.edges:
            if edge.source not in known or edge.target not in known:
                raise FieldworkError("Scope graph edges must reference existing nodes")
        adjacency = {node_id: [] for node_id in node_ids}
        for edge in self.edges:
            adjacency[edge.source].append(edge.target)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise FieldworkError("Scope graph must be acyclic")
            if node_id in visited:
                return
            visiting.add(node_id)
            for target in adjacency[node_id]:
                visit(target)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in sorted(node_ids):
            visit(node_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "nodes": [node.as_dict() for node in sorted(self.nodes, key=lambda item: item.node_id)],
            "edges": [
                edge.as_dict()
                for edge in sorted(
                    self.edges, key=lambda item: (item.source, item.target, item.relation)
                )
            ],
        }


@dataclass(frozen=True)
class AuthorizationContext:
    principal_id: str
    project_ids: frozenset[str]
    scales: frozenset[AccessScale]
    max_sensitivity: Sensitivity
    cycle_ids: frozenset[str] = frozenset()
    branch_ids: frozenset[str] = frozenset()
    epistemic_layers: frozenset[EpistemicLayer] = frozenset()
    authorization_tags: frozenset[str] = frozenset()
    scope_node_ids: frozenset[str] = frozenset()

    def require_target(
        self, project_id: str, cycle_id: str, branch_id: str, scale: AccessScale
    ) -> None:
        if project_id not in self.project_ids:
            raise AuthorizationDenied("Principal is not authorized for this project")
        if self.cycle_ids and cycle_id not in self.cycle_ids:
            raise AuthorizationDenied("Principal is not authorized for this cycle")
        if self.branch_ids and branch_id not in self.branch_ids:
            raise AuthorizationDenied("Principal is not authorized for this branch")
        if scale not in self.scales:
            raise AuthorizationDenied("Principal is not authorized at the requested scale")

    def event_restrictions(
        self, event: "FieldworkEvent", scale: AccessScale, consent: Mapping[str, str]
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if scale not in event.manifest.allowed_scales:
            reasons.append("access_scale")
        if SENSITIVITY_RANK[event.manifest.sensitivity] > SENSITIVITY_RANK[self.max_sensitivity]:
            reasons.append("sensitivity")
        if self.epistemic_layers and event.epistemic_layer not in self.epistemic_layers:
            reasons.append("epistemic_layer")
        required_tags = set(event.manifest.authorization_tags)
        if required_tags and not required_tags.issubset(self.authorization_tags):
            reasons.append("authorization_tag")
        required_scopes = set(event.manifest.scope_node_ids)
        if required_scopes and not required_scopes.issubset(self.scope_node_ids):
            reasons.append("scope_node")
        if event.kind not in {EventKind.CONSENT_GRANTED, EventKind.CONSENT_WITHDRAWN}:
            if event.manifest.consent_basis == "pending":
                reasons.append("consent_pending")
            elif event.manifest.consent_basis == "granted" and any(
                consent.get(subject) != "granted"
                for subject in event.manifest.consent_subjects
            ):
                reasons.append("consent_withdrawn")
        return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True)
class BranchSpec:
    branch_id: str
    project_id: str
    cycle_id: str | None
    mode: BranchMode
    parent_branch_id: str | None
    base_event_id: str | None
    created_at: datetime


@dataclass(frozen=True)
class FieldworkEvent:
    event_id: str
    event_hash: str
    previous_event_hash: str
    project_id: str
    cycle_id: str | None
    branch_id: str
    branch_sequence: int
    kind: EventKind
    epistemic_layer: EpistemicLayer
    actor: ActorRef
    chronology: Chronology
    payload_json: str
    causal_event_ids: tuple[str, ...]
    source_refs: tuple[SourceRef, ...]
    manifest: EvidenceManifest
    canonical_effect: bool

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_hash": self.event_hash,
            "previous_event_hash": self.previous_event_hash,
            "project_id": self.project_id,
            "cycle_id": self.cycle_id,
            "branch_id": self.branch_id,
            "branch_sequence": self.branch_sequence,
            "kind": self.kind.value,
            "epistemic_layer": self.epistemic_layer.value,
            "actor": self.actor.as_dict(),
            "chronology": self.chronology.as_dict(),
            "payload": self.payload,
            "causal_event_ids": list(self.causal_event_ids),
            "source_refs": [source.as_dict() for source in self.source_refs],
            "manifest": self.manifest.as_dict(),
            "canonical_effect": self.canonical_effect,
        }


@dataclass(frozen=True)
class Projection:
    project_id: str
    cycle_id: str
    branch_id: str
    branch_mode: BranchMode
    state_json: str
    state_hash: str

    @property
    def state(self) -> dict[str, Any]:
        return json.loads(self.state_json)


@dataclass(frozen=True)
class OutputReplay:
    output_id: str
    origin_event_id: str
    mode: ReplayMode
    content: str
    output_hash: str
    stored_output_hash: str
    persisted: bool
    generator_version: str


class FieldworkLedger:
    """In-memory append-only ledger with deterministic, authorization-aware projections."""

    def __init__(self) -> None:
        self._events: tuple[FieldworkEvent, ...] = ()
        self._events_by_id: dict[str, FieldworkEvent] = {}
        self._branches: dict[str, BranchSpec] = {}
        self._canonical_branches: dict[str, str] = {}
        self._cycles: set[tuple[str, str]] = set()

    @property
    def events(self) -> tuple[FieldworkEvent, ...]:
        return self._events

    @property
    def branch_specs(self) -> tuple[BranchSpec, ...]:
        """Return immutable branch metadata in creation order for persistence."""

        return tuple(self._branches.values())

    @property
    def cycles(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._cycles))

    @property
    def canonical_branches(self) -> dict[str, str]:
        return dict(self._canonical_branches)

    @classmethod
    def reconstitute(
        cls,
        *,
        canonical_branches: Mapping[str, str],
        cycles: Iterable[tuple[str, str]],
        branches: Iterable[BranchSpec],
        events: Iterable[FieldworkEvent],
    ) -> "FieldworkLedger":
        """Restore a ledger from durable rows and verify every hash-chain invariant."""

        ledger = cls()
        branch_items = tuple(branches)
        event_items = tuple(events)
        ledger._canonical_branches = dict(canonical_branches)
        ledger._cycles = set(cycles)
        ledger._branches = {branch.branch_id: branch for branch in branch_items}
        ledger._events = event_items
        ledger._events_by_id = {event.event_id: event for event in event_items}
        if len(ledger._branches) != len(branch_items):
            raise AppendOnlyViolation("Persisted branch ids must be unique")
        if len(ledger._events_by_id) != len(event_items):
            raise AppendOnlyViolation("Persisted event ids must be unique")
        ledger.validate_integrity()
        return ledger

    def validate_integrity(self) -> None:
        """Fail closed if persisted metadata, chronology, or event hashes were altered."""

        for project_id, canonical_id in self._canonical_branches.items():
            branch = self._branches.get(canonical_id)
            if (
                branch is None
                or branch.project_id != project_id
                or branch.mode is not BranchMode.CANONICAL
                or branch.parent_branch_id is not None
                or branch.base_event_id is not None
            ):
                raise AppendOnlyViolation("Canonical branch metadata is inconsistent")
        for project_id, cycle_id in self._cycles:
            if project_id not in self._canonical_branches or not cycle_id.strip():
                raise AppendOnlyViolation("Persisted cycle metadata is inconsistent")
        for branch in self._branches.values():
            _timestamp(branch.created_at)
            if branch.project_id not in self._canonical_branches:
                raise AppendOnlyViolation("Branch refers to an unknown project")
            if branch.mode is BranchMode.CANONICAL:
                if self._canonical_branches.get(branch.project_id) != branch.branch_id:
                    raise AppendOnlyViolation("Only the registered branch may be canonical")
            else:
                parent = self._branches.get(branch.parent_branch_id or "")
                if (
                    parent is None
                    or parent.project_id != branch.project_id
                    or branch.cycle_id is None
                    or (branch.project_id, branch.cycle_id) not in self._cycles
                    or not branch.base_event_id
                ):
                    raise AppendOnlyViolation("Fork metadata is incomplete")

        seen_ids: set[str] = set()
        per_branch: dict[str, list[FieldworkEvent]] = {
            branch_id: [] for branch_id in self._branches
        }
        for event in self._events:
            branch = self._branches.get(event.branch_id)
            if branch is None or branch.project_id != event.project_id:
                raise AppendOnlyViolation("Event refers to an unknown branch")
            if event.event_id in seen_ids:
                raise AppendOnlyViolation("Event ids must be globally unique")
            seen_ids.add(event.event_id)
            per_branch[event.branch_id].append(event)

        for branch_id, own_events in per_branch.items():
            branch = self._branches[branch_id]
            previous_hash = (
                self._events_by_id[branch.base_event_id].event_hash
                if branch.base_event_id in self._events_by_id
                else ""
            )
            if branch.base_event_id:
                try:
                    parent_ids = {
                        event.event_id
                        for event in self.effective_events(branch.parent_branch_id or "")
                    }
                except FieldworkError as error:
                    raise AppendOnlyViolation("Fork parent history is invalid") from error
                if branch.base_event_id not in parent_ids:
                    raise AppendOnlyViolation("Fork base is absent from parent history")
            for sequence, event in enumerate(own_events, start=1):
                if event.branch_sequence != sequence:
                    raise AppendOnlyViolation("Branch event sequence is not contiguous")
                if event.previous_event_hash != previous_hash:
                    raise AppendOnlyViolation("Event hash chain has been altered")
                if event.cycle_id is not None and (
                    event.project_id,
                    event.cycle_id,
                ) not in self._cycles:
                    raise AppendOnlyViolation("Event refers to an unknown cycle")
                if event.canonical_effect is not (branch.mode is BranchMode.CANONICAL):
                    raise AppendOnlyViolation("Forks and canonical events cannot exchange effects")
                if (
                    branch.mode is BranchMode.COUNTERFACTUAL
                    and event.kind is not EventKind.BRANCH_FORKED
                    and event.epistemic_layer is not EpistemicLayer.COUNTERFACTUAL
                ):
                    raise AppendOnlyViolation("Counterfactual evidence escaped its layer")
                seed = {
                    "previous_event_hash": event.previous_event_hash,
                    "project_id": event.project_id,
                    "cycle_id": event.cycle_id,
                    "branch_id": event.branch_id,
                    "branch_sequence": event.branch_sequence,
                    "kind": event.kind.value,
                    "epistemic_layer": event.epistemic_layer.value,
                    "actor": event.actor.as_dict(),
                    "chronology": event.chronology.as_dict(),
                    "payload": event.payload,
                    "causal_event_ids": list(event.causal_event_ids),
                    "source_refs": [ref.as_dict() for ref in event.source_refs],
                    "manifest": event.manifest.as_dict(),
                    "canonical_effect": event.canonical_effect,
                }
                expected = content_hash({**seed, "event_id": event.event_id})
                if event.event_hash != expected:
                    raise AppendOnlyViolation("Persisted event content does not match its hash")
                previous_hash = event.event_hash

        for event in self._events:
            effective = self.effective_events(event.branch_id)
            event_position = next(
                index for index, item in enumerate(effective) if item.event_id == event.event_id
            )
            prior_ids = {item.event_id for item in effective[:event_position]}
            if not set(event.causal_event_ids).issubset(prior_ids):
                raise AppendOnlyViolation("Causal references do not point backward")

    def canonical_branch_id(self, project_id: str) -> str:
        try:
            return self._canonical_branches[project_id]
        except KeyError as error:
            raise FieldworkError("Unknown fieldwork project") from error

    def create_project(
        self,
        project_id: str,
        title: str,
        chronology: Chronology,
        manifest: EvidenceManifest,
        *,
        canonical_branch_id: str | None = None,
        actor: ActorRef = SYSTEM_ACTOR,
        event_id: str | None = None,
    ) -> FieldworkEvent:
        if not project_id.strip() or not title.strip():
            raise FieldworkError("Projects require an id and title")
        if project_id in self._canonical_branches:
            raise AppendOnlyViolation("A project id cannot be created twice")
        branch_id = canonical_branch_id or f"{project_id}:canonical"
        if branch_id in self._branches:
            raise AppendOnlyViolation("A branch id cannot be reused")
        self._canonical_branches[project_id] = branch_id
        self._branches[branch_id] = BranchSpec(
            branch_id=branch_id,
            project_id=project_id,
            cycle_id=None,
            mode=BranchMode.CANONICAL,
            parent_branch_id=None,
            base_event_id=None,
            created_at=chronology.committed_at,
        )
        try:
            return self.append(
                project_id=project_id,
                cycle_id=None,
                branch_id=branch_id,
                kind=EventKind.PROJECT_CREATED,
                epistemic_layer=EpistemicLayer.RESEARCHER_RECORD,
                chronology=chronology,
                payload={"title": title, "canonical_branch_id": branch_id},
                manifest=manifest,
                actor=actor,
                event_id=event_id,
            )
        except Exception:
            del self._canonical_branches[project_id]
            del self._branches[branch_id]
            raise

    def open_cycle(
        self,
        project_id: str,
        cycle_id: str,
        label: str,
        chronology: Chronology,
        manifest: EvidenceManifest,
        *,
        actor: ActorRef = SYSTEM_ACTOR,
        event_id: str | None = None,
    ) -> FieldworkEvent:
        if project_id not in self._canonical_branches:
            raise FieldworkError("Create the project before opening a cycle")
        if not cycle_id.strip() or not label.strip():
            raise FieldworkError("Cycles require an id and label")
        key = (project_id, cycle_id)
        if key in self._cycles:
            raise AppendOnlyViolation("A cycle id cannot be opened twice")
        event = self.append(
            project_id=project_id,
            cycle_id=cycle_id,
            branch_id=self.canonical_branch_id(project_id),
            kind=EventKind.CYCLE_OPENED,
            epistemic_layer=EpistemicLayer.RESEARCHER_RECORD,
            chronology=chronology,
            payload={"label": label},
            manifest=manifest,
            actor=actor,
            event_id=event_id,
            _opening_cycle=True,
        )
        self._cycles.add(key)
        return event

    def fork(
        self,
        *,
        project_id: str,
        cycle_id: str,
        branch_id: str,
        parent_branch_id: str,
        base_event_id: str,
        mode: BranchMode,
        chronology: Chronology,
        manifest: EvidenceManifest,
        rationale: str,
        actor: ActorRef = SYSTEM_ACTOR,
        event_id: str | None = None,
    ) -> FieldworkEvent:
        if mode is BranchMode.CANONICAL:
            raise FieldworkError("Forks must be historical or counterfactual")
        if branch_id in self._branches:
            raise AppendOnlyViolation("A branch id cannot be reused")
        if (project_id, cycle_id) not in self._cycles:
            raise FieldworkError("Unknown fieldwork cycle")
        parent = self._branches.get(parent_branch_id)
        if not parent or parent.project_id != project_id:
            raise FieldworkError("Fork parent does not belong to the project")
        parent_events = self.effective_events(parent_branch_id)
        if base_event_id not in {event.event_id for event in parent_events}:
            raise FieldworkError("Fork base must exist in the parent history")
        self._branches[branch_id] = BranchSpec(
            branch_id=branch_id,
            project_id=project_id,
            cycle_id=cycle_id,
            mode=mode,
            parent_branch_id=parent_branch_id,
            base_event_id=base_event_id,
            created_at=chronology.committed_at,
        )
        try:
            return self.append(
                project_id=project_id,
                cycle_id=cycle_id,
                branch_id=branch_id,
                kind=EventKind.BRANCH_FORKED,
                epistemic_layer=(
                    EpistemicLayer.COUNTERFACTUAL
                    if mode is BranchMode.COUNTERFACTUAL
                    else EpistemicLayer.INTERPRETATION
                ),
                chronology=chronology,
                payload={
                    "mode": mode.value,
                    "parent_branch_id": parent_branch_id,
                    "base_event_id": base_event_id,
                    "rationale": rationale,
                },
                causal_event_ids=(base_event_id,),
                manifest=manifest,
                actor=actor,
                canonical_effect=False,
                event_id=event_id,
            )
        except Exception:
            del self._branches[branch_id]
            raise

    def append(
        self,
        *,
        project_id: str,
        cycle_id: str | None,
        branch_id: str,
        kind: EventKind,
        epistemic_layer: EpistemicLayer,
        chronology: Chronology,
        payload: Mapping[str, Any],
        manifest: EvidenceManifest,
        actor: ActorRef = SYSTEM_ACTOR,
        causal_event_ids: Sequence[str] = (),
        source_refs: Sequence[SourceRef] = (),
        canonical_effect: bool | None = None,
        event_id: str | None = None,
        _opening_cycle: bool = False,
    ) -> FieldworkEvent:
        branch = self._branches.get(branch_id)
        if not branch or branch.project_id != project_id:
            raise FieldworkError("Unknown branch for this project")
        if kind is not EventKind.PROJECT_CREATED:
            if cycle_id is None:
                raise FieldworkError("Non-project events require a cycle")
            if not _opening_cycle and (project_id, cycle_id) not in self._cycles:
                raise FieldworkError("Unknown fieldwork cycle")
        if branch.cycle_id is not None and branch.cycle_id != cycle_id:
            raise FieldworkError("Fork events must stay in their original cycle")
        if branch.mode is BranchMode.CANONICAL:
            effect = True if canonical_effect is None else canonical_effect
            if not effect:
                raise FieldworkError("Canonical events must declare canonical effect")
        else:
            effect = False if canonical_effect is None else canonical_effect
            if effect:
                raise AppendOnlyViolation("Forks cannot write to canonical history")
            if (
                branch.mode is BranchMode.COUNTERFACTUAL
                and kind is not EventKind.BRANCH_FORKED
                and epistemic_layer is not EpistemicLayer.COUNTERFACTUAL
            ):
                raise FieldworkError(
                    "Counterfactual branch events must remain in the counterfactual layer"
                )
        causal_ids = tuple(causal_event_ids)
        if len(causal_ids) != len(set(causal_ids)):
            raise FieldworkError("Causal references must be unique")
        available = {event.event_id for event in self.effective_events(branch_id)}
        missing = [cause for cause in causal_ids if cause not in available]
        if missing:
            raise FieldworkError("Causal references must point backward in branch history")
        refs = tuple(source_refs)
        if len({ref.source_id for ref in refs}) != len(refs):
            raise FieldworkError("Source references must be unique per event")
        payload_json = canonical_json(payload)
        own_events = [event for event in self._events if event.branch_id == branch_id]
        branch_sequence = len(own_events) + 1
        if own_events:
            previous_hash = own_events[-1].event_hash
        elif branch.base_event_id:
            previous_hash = self._events_by_id[branch.base_event_id].event_hash
        else:
            previous_hash = ""
        seed = {
            "previous_event_hash": previous_hash,
            "project_id": project_id,
            "cycle_id": cycle_id,
            "branch_id": branch_id,
            "branch_sequence": branch_sequence,
            "kind": kind.value,
            "epistemic_layer": epistemic_layer.value,
            "actor": actor.as_dict(),
            "chronology": chronology.as_dict(),
            "payload": json.loads(payload_json),
            "causal_event_ids": list(causal_ids),
            "source_refs": [ref.as_dict() for ref in refs],
            "manifest": manifest.as_dict(),
            "canonical_effect": effect,
        }
        assigned_id = event_id or f"fw_{content_hash(seed)[:32]}"
        if not assigned_id.strip() or assigned_id in self._events_by_id:
            raise AppendOnlyViolation("Event ids must be non-empty and unique")
        event_digest = content_hash({**seed, "event_id": assigned_id})
        event = FieldworkEvent(
            event_id=assigned_id,
            event_hash=event_digest,
            previous_event_hash=previous_hash,
            project_id=project_id,
            cycle_id=cycle_id,
            branch_id=branch_id,
            branch_sequence=branch_sequence,
            kind=kind,
            epistemic_layer=epistemic_layer,
            actor=actor,
            chronology=chronology,
            payload_json=payload_json,
            causal_event_ids=causal_ids,
            source_refs=refs,
            manifest=manifest,
            canonical_effect=effect,
        )
        self._events = (*self._events, event)
        self._events_by_id[event.event_id] = event
        return event

    def effective_events(self, branch_id: str) -> tuple[FieldworkEvent, ...]:
        branch = self._branches.get(branch_id)
        if not branch:
            raise FieldworkError("Unknown fieldwork branch")
        own = tuple(event for event in self._events if event.branch_id == branch_id)
        if branch.mode is BranchMode.CANONICAL:
            return own
        if not branch.parent_branch_id or not branch.base_event_id:
            raise FieldworkError("Fork metadata is incomplete")
        parent = self.effective_events(branch.parent_branch_id)
        for index, event in enumerate(parent):
            if event.event_id == branch.base_event_id:
                return (*parent[: index + 1], *own)
        raise FieldworkError("Fork base is no longer available")

    def version_scope_graph(
        self,
        *,
        project_id: str,
        cycle_id: str,
        branch_id: str,
        graph: ScopeGraph,
        chronology: Chronology,
        manifest: EvidenceManifest,
        causal_event_ids: Sequence[str] = (),
        actor: ActorRef = SYSTEM_ACTOR,
        event_id: str | None = None,
    ) -> FieldworkEvent:
        if manifest.versions.scope_graph_version != graph.version:
            raise FieldworkError("Event and scope graph versions must match")
        return self.append(
            project_id=project_id,
            cycle_id=cycle_id,
            branch_id=branch_id,
            kind=EventKind.SCOPE_GRAPH_VERSIONED,
            epistemic_layer=EpistemicLayer.RESEARCHER_RECORD,
            chronology=chronology,
            payload={"graph": graph.as_dict()},
            causal_event_ids=causal_event_ids,
            manifest=manifest,
            actor=actor,
            event_id=event_id,
        )

    def set_consent(
        self,
        *,
        project_id: str,
        cycle_id: str,
        branch_id: str,
        subject_id: str,
        granted: bool,
        chronology: Chronology,
        manifest: EvidenceManifest,
        reason: str,
        actor: ActorRef = SYSTEM_ACTOR,
        event_id: str | None = None,
    ) -> FieldworkEvent:
        if not subject_id.strip():
            raise FieldworkError("Consent changes require a subject reference")
        return self.append(
            project_id=project_id,
            cycle_id=cycle_id,
            branch_id=branch_id,
            kind=EventKind.CONSENT_GRANTED if granted else EventKind.CONSENT_WITHDRAWN,
            epistemic_layer=(
                EpistemicLayer.COUNTERFACTUAL
                if self._branches[branch_id].mode is BranchMode.COUNTERFACTUAL
                else EpistemicLayer.RESEARCHER_RECORD
            ),
            chronology=chronology,
            payload={"subject_id": subject_id, "reason": reason},
            manifest=manifest,
            actor=actor,
            event_id=event_id,
        )

    def record_observation(
        self,
        *,
        project_id: str,
        cycle_id: str,
        branch_id: str,
        content: str,
        chronology: Chronology,
        manifest: EvidenceManifest,
        layer: EpistemicLayer = EpistemicLayer.OBSERVATION,
        causal_event_ids: Sequence[str] = (),
        source_refs: Sequence[SourceRef] = (),
        actor: ActorRef = SYSTEM_ACTOR,
        event_id: str | None = None,
    ) -> FieldworkEvent:
        if not content.strip():
            raise FieldworkError("Fieldwork observations cannot be blank")
        return self.append(
            project_id=project_id,
            cycle_id=cycle_id,
            branch_id=branch_id,
            kind=EventKind.OBSERVATION_RECORDED,
            epistemic_layer=layer,
            chronology=chronology,
            payload={"content": content},
            causal_event_ids=causal_event_ids,
            source_refs=source_refs,
            manifest=manifest,
            actor=actor,
            event_id=event_id,
        )

    def store_output(
        self,
        *,
        project_id: str,
        cycle_id: str,
        branch_id: str,
        output_id: str,
        content: str,
        input_event_ids: Sequence[str],
        chronology: Chronology,
        manifest: EvidenceManifest,
        generator: str,
        auth: AuthorizationContext,
        actor: ActorRef = SYSTEM_ACTOR,
        event_id: str | None = None,
    ) -> FieldworkEvent:
        if not output_id.strip() or not content.strip() or not generator.strip():
            raise FieldworkError("Stored outputs require an id, content, and generator")
        self.validate_output_derivation(
            project_id=project_id,
            cycle_id=cycle_id,
            branch_id=branch_id,
            input_event_ids=input_event_ids,
            manifest=manifest,
            auth=auth,
        )
        return self.append(
            project_id=project_id,
            cycle_id=cycle_id,
            branch_id=branch_id,
            kind=EventKind.OUTPUT_STORED,
            epistemic_layer=(
                EpistemicLayer.COUNTERFACTUAL
                if self._branches[branch_id].mode is BranchMode.COUNTERFACTUAL
                else EpistemicLayer.SYNTHESIS
            ),
            chronology=chronology,
            payload={
                "output_id": output_id,
                "content": content,
                "stored_output_hash": content_hash(content),
                "input_event_ids": list(input_event_ids),
                "generator": generator,
            },
            causal_event_ids=input_event_ids,
            manifest=manifest,
            actor=actor,
            event_id=event_id,
        )

    @staticmethod
    def _stored_output_input_ids(event: FieldworkEvent) -> tuple[str, ...]:
        raw = event.payload.get("input_event_ids")
        if not isinstance(raw, list) or not raw:
            raise FieldworkError("Stored output has no derivation inputs")
        if any(not isinstance(item, str) or not item.strip() for item in raw):
            raise FieldworkError("Stored output derivation inputs are invalid")
        resolved = tuple(item.strip() for item in raw)
        if len(set(resolved)) != len(resolved):
            raise FieldworkError("Stored output derivation inputs must be unique")
        if resolved != event.causal_event_ids:
            raise FieldworkError(
                "Stored output derivation inputs do not match causal provenance"
            )
        return resolved

    def _resolve_derivation_inputs(
        self,
        *,
        branch_id: str,
        input_event_ids: Sequence[str],
        parent_event: FieldworkEvent | None = None,
    ) -> tuple[FieldworkEvent, ...]:
        """Resolve a backward-only, effective, recursively nested input graph."""

        direct_ids = tuple(input_event_ids)
        if not direct_ids:
            raise FieldworkError("Stored outputs require at least one derivation input")
        if any(not isinstance(item, str) or not item.strip() for item in direct_ids):
            raise FieldworkError("Stored output derivation inputs are invalid")
        if len(set(direct_ids)) != len(direct_ids):
            raise FieldworkError("Stored output derivation inputs must be unique")
        effective = self.effective_events(branch_id)
        positions = {event.event_id: index for index, event in enumerate(effective)}
        parent_position = len(effective)
        if parent_event is not None:
            if parent_event.branch_id != branch_id:
                raise FieldworkError("Stored output is outside its derivation branch")
            try:
                parent_position = positions[parent_event.event_id]
            except KeyError as error:
                raise FieldworkError(
                    "Stored output is absent from effective branch history"
                ) from error

        resolved: list[FieldworkEvent] = []
        visited: set[str] = set()

        def visit(event_id: str, before_position: int) -> None:
            position = positions.get(event_id)
            if position is None or position >= before_position:
                raise FieldworkError(
                    "Stored output inputs must point backward in effective branch history"
                )
            source = effective[position]
            if source.event_id in visited:
                return
            visited.add(source.event_id)
            resolved.append(source)
            if source.kind is EventKind.OUTPUT_STORED:
                for nested_id in self._stored_output_input_ids(source):
                    visit(nested_id, position)

        for input_id in direct_ids:
            visit(input_id, parent_position)
        return tuple(resolved)

    @staticmethod
    def _enforce_output_manifest_dominance(
        manifest: EvidenceManifest, inputs: Sequence[FieldworkEvent]
    ) -> None:
        if not inputs:
            raise FieldworkError("Stored outputs require derivation inputs")
        maximum_sensitivity = max(
            (event.manifest.sensitivity for event in inputs),
            key=lambda value: SENSITIVITY_RANK[value],
        )
        if (
            SENSITIVITY_RANK[manifest.sensitivity]
            < SENSITIVITY_RANK[maximum_sensitivity]
        ):
            raise FieldworkError(
                "Output sensitivity cannot be lower than its derivation inputs"
            )

        allowed_intersection = set(inputs[0].manifest.allowed_scales)
        for event in inputs[1:]:
            allowed_intersection.intersection_update(event.manifest.allowed_scales)
        if not set(manifest.allowed_scales).issubset(allowed_intersection):
            raise FieldworkError(
                "Output access scales must stay within every derivation input"
            )

        required_tags = {
            tag for event in inputs for tag in event.manifest.authorization_tags
        }
        if not required_tags.issubset(manifest.authorization_tags):
            raise FieldworkError(
                "Output authorization tags must include every derivation input"
            )
        required_scopes = {
            scope for event in inputs for scope in event.manifest.scope_node_ids
        }
        if not required_scopes.issubset(manifest.scope_node_ids):
            raise FieldworkError(
                "Output scope nodes must include every derivation input"
            )
        required_subjects = {
            subject for event in inputs for subject in event.manifest.consent_subjects
        }
        if not required_subjects.issubset(manifest.consent_subjects):
            raise FieldworkError(
                "Output consent subjects must include every derivation input"
            )

        input_bases = {event.manifest.consent_basis for event in inputs}
        if "pending" in input_bases and manifest.consent_basis != "pending":
            raise FieldworkError("Outputs derived from pending consent must remain pending")
        if (
            "pending" not in input_bases
            and "granted" in input_bases
            and manifest.consent_basis not in {"granted", "pending"}
        ):
            raise FieldworkError(
                "Outputs derived from consent-bound inputs must remain consent-bound"
            )

    def _derivation_input_restrictions(
        self,
        *,
        event: FieldworkEvent,
        auth: AuthorizationContext,
        scale: AccessScale,
        consent: Mapping[str, str],
    ) -> tuple[str, ...]:
        if event.kind is not EventKind.OUTPUT_STORED:
            return ()
        try:
            inputs = self._resolve_derivation_inputs(
                branch_id=event.branch_id,
                input_event_ids=self._stored_output_input_ids(event),
                parent_event=event,
            )
        except FieldworkError:
            return ("derived_input_invalid",)

        reasons: list[str] = []
        for source in inputs:
            target_cycle = source.cycle_id or event.cycle_id
            if target_cycle is None:
                reasons.append("derived_input_target_authorization")
                continue
            try:
                auth.require_target(
                    source.project_id,
                    target_cycle,
                    event.branch_id,
                    scale,
                )
            except AuthorizationDenied:
                reasons.append("derived_input_target_authorization")
                continue
            for reason in auth.event_restrictions(source, scale, consent):
                reasons.append(f"derived_input_{reason}")
        return tuple(dict.fromkeys(reasons))

    def validate_output_derivation(
        self,
        *,
        project_id: str,
        cycle_id: str,
        branch_id: str,
        input_event_ids: Sequence[str],
        manifest: EvidenceManifest,
        auth: AuthorizationContext,
    ) -> tuple[FieldworkEvent, ...]:
        """Validate authority, current consent, and manifest non-downgrade."""

        inputs = self._resolve_derivation_inputs(
            branch_id=branch_id,
            input_event_ids=input_event_ids,
        )
        if any(event.project_id != project_id for event in inputs):
            raise FieldworkError("Output inputs must stay within their project")
        self._enforce_output_manifest_dominance(manifest, inputs)
        consent = self._current_consent(project_id, branch_id)
        for source in inputs:
            target_cycle = source.cycle_id or cycle_id
            for scale in manifest.allowed_scales:
                try:
                    auth.require_target(
                        source.project_id,
                        target_cycle,
                        branch_id,
                        scale,
                    )
                except AuthorizationDenied as error:
                    raise AuthorizationDenied(
                        "Principal is not authorized to derive from an output input"
                    ) from error
                restrictions = auth.event_restrictions(source, scale, consent)
                if restrictions:
                    raise AuthorizationDenied(
                        "Output input is unavailable under current authorization or consent"
                    )
        return inputs

    def _current_consent(self, project_id: str, branch_id: str) -> dict[str, str]:
        canonical_id = self.canonical_branch_id(project_id)
        canonical: dict[str, str] = {}
        for event in self._events:
            if event.branch_id != canonical_id or event.kind not in {
                EventKind.CONSENT_GRANTED,
                EventKind.CONSENT_WITHDRAWN,
            }:
                continue
            subject = str(event.payload.get("subject_id") or "")
            if subject:
                canonical[subject] = (
                    "granted" if event.kind is EventKind.CONSENT_GRANTED else "withdrawn"
                )
        resolved = dict(canonical)
        for event in self.effective_events(branch_id):
            if event.branch_id == canonical_id or event.kind not in {
                EventKind.CONSENT_GRANTED,
                EventKind.CONSENT_WITHDRAWN,
            }:
                continue
            subject = str(event.payload.get("subject_id") or "")
            if not subject:
                continue
            if event.kind is EventKind.CONSENT_WITHDRAWN:
                resolved[subject] = "withdrawn"
            elif canonical.get(subject) != "withdrawn":
                resolved[subject] = "granted"
        return resolved

    def _event_view(
        self,
        event: FieldworkEvent,
        auth: AuthorizationContext,
        scale: AccessScale,
        consent: Mapping[str, str],
    ) -> dict[str, Any]:
        restrictions = tuple(
            dict.fromkeys(
                (
                    *auth.event_restrictions(event, scale, consent),
                    *self._derivation_input_restrictions(
                        event=event,
                        auth=auth,
                        scale=scale,
                        consent=consent,
                    ),
                )
            )
        )
        visible = not restrictions
        return {
            "event_id": event.event_id,
            "event_hash": event.event_hash,
            "kind": event.kind.value,
            "branch_id": event.branch_id,
            "branch_sequence": event.branch_sequence,
            "epistemic_layer": event.epistemic_layer.value,
            "actor": event.actor.as_dict()
            if visible
            else {"actor_id": "redacted", "actor_role": "redacted"},
            "chronology": event.chronology.as_dict(),
            "payload": event.payload
            if visible
            else {"redacted": True, "reasons": list(restrictions)},
            "causal_event_ids": list(event.causal_event_ids),
            "source_refs": [source.as_dict() for source in event.source_refs] if visible else [],
            "manifest": event.manifest.as_dict(reveal_subjects=visible),
            "canonical_effect": event.canonical_effect,
            "redacted": not visible,
        }

    def project(
        self,
        *,
        project_id: str,
        cycle_id: str,
        branch_id: str,
        auth: AuthorizationContext,
        scale: AccessScale,
        as_of_event_id: str | None = None,
    ) -> Projection:
        branch = self._branches.get(branch_id)
        if not branch or branch.project_id != project_id:
            raise FieldworkError("Unknown branch for this project")
        if (project_id, cycle_id) not in self._cycles:
            raise FieldworkError("Unknown fieldwork cycle")
        auth.require_target(project_id, cycle_id, branch_id, scale)
        effective = list(self.effective_events(branch_id))
        if as_of_event_id:
            positions = [
                index for index, event in enumerate(effective) if event.event_id == as_of_event_id
            ]
            if not positions:
                raise FieldworkError("Projection cutoff is not in this branch history")
            effective = effective[: positions[0] + 1]
        effective = [
            event for event in effective if event.cycle_id in {None, cycle_id}
        ]
        consent = self._current_consent(project_id, branch_id)
        event_views = [self._event_view(event, auth, scale, consent) for event in effective]
        project_data: dict[str, Any] = {"id": project_id}
        cycle_data: dict[str, Any] = {"id": cycle_id}
        scope_graph: dict[str, Any] | None = None
        outputs: list[dict[str, Any]] = []
        for view in event_views:
            if view["redacted"]:
                continue
            payload = view["payload"]
            if view["kind"] == EventKind.PROJECT_CREATED.value:
                project_data.update(payload)
            elif view["kind"] == EventKind.CYCLE_OPENED.value:
                cycle_data.update(payload)
            elif view["kind"] == EventKind.SCOPE_GRAPH_VERSIONED.value:
                scope_graph = payload["graph"]
            elif view["kind"] == EventKind.OUTPUT_STORED.value:
                outputs.append(
                    {
                        "event_id": view["event_id"],
                        "output_id": payload["output_id"],
                        "stored_output_hash": payload["stored_output_hash"],
                        "generator": payload["generator"],
                    }
                )
        consent_summary = {
            status: sum(1 for value in consent.values() if value == status)
            for status in ("granted", "withdrawn")
        }
        state = {
            "project": project_data,
            "cycle": cycle_data,
            "branch": {
                "id": branch.branch_id,
                "mode": branch.mode.value,
                "parent_branch_id": branch.parent_branch_id,
                "base_event_id": branch.base_event_id,
                "canonical_writes": branch.mode is BranchMode.CANONICAL,
            },
            "scope_graph": scope_graph,
            "events": event_views,
            "outputs": outputs,
            "consent_summary": consent_summary,
            "projection_scale": scale.value,
        }
        state_json = canonical_json(state)
        return Projection(
            project_id=project_id,
            cycle_id=cycle_id,
            branch_id=branch_id,
            branch_mode=branch.mode,
            state_json=state_json,
            state_hash=hashlib.sha256(state_json.encode("utf-8")).hexdigest(),
        )

    def replay_output(
        self,
        *,
        project_id: str,
        cycle_id: str,
        branch_id: str,
        output_id: str,
        auth: AuthorizationContext,
        scale: AccessScale,
        mode: ReplayMode = ReplayMode.STORED,
        regenerate: Callable[[Mapping[str, Any]], str] | None = None,
        generator_version: str = "none",
    ) -> OutputReplay:
        auth.require_target(project_id, cycle_id, branch_id, scale)
        consent = self._current_consent(project_id, branch_id)
        candidates = [
            event
            for event in self.effective_events(branch_id)
            if event.cycle_id == cycle_id
            and event.kind is EventKind.OUTPUT_STORED
            and event.payload.get("output_id") == output_id
        ]
        if not candidates:
            raise FieldworkError("Stored output not found in this branch history")
        origin = candidates[-1]
        view = self._event_view(origin, auth, scale, consent)
        if view["redacted"]:
            raise AuthorizationDenied("Stored output is redacted under current policy")
        stored = str(origin.payload["content"])
        stored_hash = str(origin.payload["stored_output_hash"])
        if mode is ReplayMode.STORED:
            return OutputReplay(
                output_id=output_id,
                origin_event_id=origin.event_id,
                mode=mode,
                content=stored,
                output_hash=stored_hash,
                stored_output_hash=stored_hash,
                persisted=True,
                generator_version=str(origin.payload.get("generator") or "unknown"),
            )
        if regenerate is None or not generator_version.strip() or generator_version == "none":
            raise FieldworkError("Regeneration requires a callable and explicit generator version")
        input_views = []
        for event_id in origin.payload.get("input_event_ids", []):
            source = self._events_by_id.get(str(event_id))
            if source:
                input_views.append(self._event_view(source, auth, scale, consent))
        if any(item["redacted"] for item in input_views):
            raise AuthorizationDenied("Regeneration inputs are redacted under current policy")
        regenerated = regenerate(
            {
                "output_id": output_id,
                "stored_output": stored,
                "stored_output_hash": stored_hash,
                "input_events": input_views,
                "origin_event_id": origin.event_id,
                "generator_version": generator_version,
            }
        )
        if not isinstance(regenerated, str) or not regenerated.strip():
            raise FieldworkError("Regeneration must return non-empty text")
        return OutputReplay(
            output_id=output_id,
            origin_event_id=origin.event_id,
            mode=mode,
            content=regenerated,
            output_hash=content_hash(regenerated),
            stored_output_hash=stored_hash,
            persisted=False,
            generator_version=generator_version,
        )


__all__ = [
    "AccessScale",
    "ActorRef",
    "AppendOnlyViolation",
    "AuthorizationContext",
    "AuthorizationDenied",
    "BranchMode",
    "BranchSpec",
    "Chronology",
    "EpistemicLayer",
    "EventKind",
    "EvidenceManifest",
    "FieldworkError",
    "FieldworkEvent",
    "FieldworkLedger",
    "OutputReplay",
    "Projection",
    "ReplayMode",
    "ScopeEdge",
    "ScopeGraph",
    "ScopeKind",
    "ScopeNode",
    "Sensitivity",
    "SourceRef",
    "SYSTEM_ACTOR",
    "VersionManifest",
    "canonical_json",
    "content_hash",
]
