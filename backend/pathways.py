"""Immutable pathway graphs and deterministic, human-authorized transitions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


class PathwayError(ValueError):
    """Raised when a graph, fact, approval, or transition is invalid."""


class FactStatus(str, Enum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ApprovalStatus(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


class RunStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETE = "complete"
    WALKED_AWAY = "walked_away"
    NON_AI = "non_ai"
    RETIRED = "retired"


class RouteOutcome(str, Enum):
    PROCEED = "proceed"
    NEGOTIATE_RETURN = "negotiate_return"
    WALK_AWAY = "walk_away"
    NON_AI = "non_ai"
    PAUSE = "pause"
    RESUME = "resume"
    REVIEW = "review"
    REASSESS = "reassess"
    RETIRE = "retire"


TERMINAL_KINDS = {"terminal", "non_ai", "walk_away", "retired"}
MAX_CONDITION_DEPTH = 12
MAX_CONDITION_TERMS = 100
PROCEED_BLOCKING_FACTS = frozenset(
    {"stage_blocked", "prohibited_use", "route_blocked"}
)
UNGUIDED_CHECKPOINT_NODES = frozenset({"synthesis", "pilot", "monitoring"})


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise PathwayError("Pathway timestamps must include a UTC offset")
        return value.isoformat(timespec="microseconds")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=str)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise PathwayError(f"Pathway values must be JSON-compatible, got {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _node_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 80 or not text.replace("_", "").isalnum():
        raise PathwayError(f"Invalid pathway node id: {value!r}")
    return text


def validate_condition(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> dict:
    """Validate the closed condition language and return a normalized copy."""

    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_CONDITION_TERMS:
        raise PathwayError("Condition has too many terms")
    if depth > MAX_CONDITION_DEPTH:
        raise PathwayError("Condition is nested too deeply")
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping):
        raise PathwayError("A pathway condition must be an object")
    keys = set(value)
    if keys == {"all"} or keys == {"any"}:
        operator = next(iter(keys))
        terms = value[operator]
        if not isinstance(terms, Sequence) or isinstance(terms, (str, bytes)) or not terms:
            raise PathwayError(f"{operator} requires a non-empty condition list")
        return {
            operator: [
                validate_condition(item, depth=depth + 1, counter=counter)
                for item in terms
            ]
        }
    if keys == {"approval"}:
        approval = str(value["approval"] or "").strip()
        if not approval:
            raise PathwayError("approval requires a gate key")
        return {"approval": approval}
    if "fact" not in keys:
        raise PathwayError("A leaf condition requires fact or approval")
    fact_key = str(value["fact"] or "").strip()
    if not fact_key:
        raise PathwayError("fact requires a key")
    operators = keys - {"fact"}
    if len(operators) != 1:
        raise PathwayError("A fact condition requires exactly one operator")
    operator = next(iter(operators))
    if operator not in {"eq", "in", "exists", "gte"}:
        raise PathwayError(f"Unsupported condition operator: {operator}")
    operand = value[operator]
    if operator == "in":
        if not isinstance(operand, Sequence) or isinstance(operand, (str, bytes)) or not operand:
            raise PathwayError("in requires a non-empty list")
        operand = [_json_value(item) for item in operand]
    elif operator == "exists":
        if not isinstance(operand, bool):
            raise PathwayError("exists requires true or false")
    elif operator == "gte":
        if isinstance(operand, bool) or not isinstance(operand, (int, float)):
            raise PathwayError("gte requires a number")
    else:
        operand = _json_value(operand)
    return {"fact": fact_key, operator: operand}


def evaluate_condition(
    condition: Mapping[str, Any],
    confirmed_facts: Mapping[str, Any],
    approved_gates: frozenset[str],
) -> bool:
    normalized = validate_condition(condition)
    if not normalized:
        return True
    if "all" in normalized:
        return all(
            evaluate_condition(item, confirmed_facts, approved_gates)
            for item in normalized["all"]
        )
    if "any" in normalized:
        return any(
            evaluate_condition(item, confirmed_facts, approved_gates)
            for item in normalized["any"]
        )
    if "approval" in normalized:
        return normalized["approval"] in approved_gates
    key = normalized["fact"]
    exists = key in confirmed_facts
    if "exists" in normalized:
        return exists is normalized["exists"]
    if not exists:
        return False
    actual = confirmed_facts[key]
    if "eq" in normalized:
        return actual == normalized["eq"]
    if "in" in normalized:
        return actual in normalized["in"]
    if "gte" in normalized:
        return (
            not isinstance(actual, bool)
            and isinstance(actual, (int, float))
            and actual >= normalized["gte"]
        )
    return False


@dataclass(frozen=True)
class RecordFact:
    key: str
    value: Any
    status: FactStatus
    source_event_ids: tuple[str, ...] = ()
    confirmed_by: str = ""

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise PathwayError("Facts require a key")
        canonical_json(self.value)
        if self.status is FactStatus.CONFIRMED and not self.confirmed_by.strip():
            raise PathwayError("Confirmed facts require a confirming actor")

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": _json_value(self.value),
            "status": self.status.value,
            "source_event_ids": list(self.source_event_ids),
            "confirmed_by": self.confirmed_by,
        }


@dataclass(frozen=True)
class Approval:
    gate_key: str
    status: ApprovalStatus
    actor_id: str
    subject_checksum: str
    decided_at: datetime
    rationale: str = ""

    def __post_init__(self) -> None:
        if not self.gate_key.strip() or not self.actor_id.strip():
            raise PathwayError("Approvals require a gate key and actor")
        if len(self.subject_checksum) != 64:
            raise PathwayError("Approval subject checksum must be SHA-256")
        if len(self.rationale) > 4_000:
            raise PathwayError("Approval rationale is too long")
        _json_value(self.decided_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate_key": self.gate_key,
            "status": self.status.value,
            "actor_id": self.actor_id,
            "subject_checksum": self.subject_checksum,
            "decided_at": _json_value(self.decided_at),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class PathwayEdge:
    edge_id: str
    from_node: str
    to_node: str
    outcome: RouteOutcome
    condition_json: str = "{}"

    @classmethod
    def build(
        cls,
        *,
        edge_id: str,
        from_node: str,
        to_node: str,
        outcome: RouteOutcome | str,
        when: Mapping[str, Any] | None = None,
    ) -> "PathwayEdge":
        resolved = outcome if isinstance(outcome, RouteOutcome) else RouteOutcome(outcome)
        return cls(
            edge_id=_node_id(edge_id),
            from_node=_node_id(from_node),
            to_node=_node_id(to_node),
            outcome=resolved,
            condition_json=canonical_json(validate_condition(when or {})),
        )

    @property
    def condition(self) -> dict[str, Any]:
        return json.loads(self.condition_json)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.edge_id,
            "from": self.from_node,
            "to": self.to_node,
            "outcome": self.outcome.value,
            "when": self.condition,
        }


@dataclass(frozen=True)
class PathwayDefinition:
    family_key: str
    version: int
    entry_node: str
    nodes_json: str
    edges: tuple[PathwayEdge, ...]
    definition_checksum: str

    @classmethod
    def build(
        cls,
        *,
        family_key: str,
        version: int,
        entry_node: str,
        nodes: Mapping[str, Mapping[str, Any]],
        edges: Sequence[PathwayEdge | Mapping[str, Any]],
    ) -> "PathwayDefinition":
        family = _node_id(family_key)
        if version < 1:
            raise PathwayError("Pathway versions start at 1")
        normalized_nodes: dict[str, dict[str, Any]] = {}
        for raw_id, raw in nodes.items():
            node_id = _node_id(raw_id)
            if not isinstance(raw, Mapping):
                raise PathwayError("Pathway nodes must be objects")
            kind = str(raw.get("kind") or "review").strip()
            label = str(raw.get("label") or node_id.replace("_", " ").title()).strip()
            roles = raw.get("entry_roles", [])
            if not isinstance(roles, Sequence) or isinstance(roles, (str, bytes)):
                raise PathwayError("entry_roles must be a list")
            normalized_nodes[node_id] = {
                "kind": kind,
                "label": label,
                "entry_roles": sorted({str(role).strip() for role in roles if str(role).strip()}),
            }
        entry = _node_id(entry_node)
        if entry not in normalized_nodes:
            raise PathwayError("The pathway entry node is missing")
        normalized_edges: list[PathwayEdge] = []
        for index, raw in enumerate(edges):
            edge = raw if isinstance(raw, PathwayEdge) else PathwayEdge.build(
                edge_id=str(raw.get("id") or f"edge_{index + 1}"),
                from_node=str(raw.get("from") or ""),
                to_node=str(raw.get("to") or ""),
                outcome=str(raw.get("outcome") or ""),
                when=raw.get("when") or {},
            )
            if edge.from_node not in normalized_nodes or edge.to_node not in normalized_nodes:
                raise PathwayError("Every edge must reference defined nodes")
            normalized_edges.append(edge)
        edge_ids = [edge.edge_id for edge in normalized_edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise PathwayError("Pathway edge ids must be unique")
        serializable = {
            "family_key": family,
            "version": version,
            "entry": entry,
            "nodes": normalized_nodes,
            "edges": [edge.as_dict() for edge in normalized_edges],
        }
        return cls(
            family_key=family,
            version=version,
            entry_node=entry,
            nodes_json=canonical_json(normalized_nodes),
            edges=tuple(normalized_edges),
            definition_checksum=checksum(serializable),
        )

    @property
    def nodes(self) -> Mapping[str, Mapping[str, Any]]:
        return MappingProxyType(json.loads(self.nodes_json))

    def as_dict(self) -> dict[str, Any]:
        return {
            "family_key": self.family_key,
            "version": self.version,
            "entry": self.entry_node,
            "nodes": json.loads(self.nodes_json),
            "edges": [edge.as_dict() for edge in self.edges],
            "checksum": self.definition_checksum,
        }

    def entry_for_role(self, role: str) -> str:
        requested = role.strip()
        if not requested or requested == "author":
            return self.entry_node
        matches = [
            node_id
            for node_id, node in self.nodes.items()
            if requested in node.get("entry_roles", [])
        ]
        if len(matches) != 1:
            raise PathwayError(f"No unambiguous entry node exists for role {requested!r}")
        return matches[0]


@dataclass(frozen=True)
class TransitionDecision:
    sequence: int
    edge_id: str
    from_node: str
    to_node: str
    outcome: RouteOutcome
    actor_id: str
    rationale: str
    evidence_json: str
    evidence_checksum: str
    pathway_checksum: str
    previous_decision_hash: str
    decision_hash: str
    decided_at: datetime

    @classmethod
    def build(
        cls,
        *,
        sequence: int,
        edge: PathwayEdge,
        actor_id: str,
        rationale: str,
        evidence: Mapping[str, Any],
        pathway_checksum: str,
        previous_decision_hash: str,
        decided_at: datetime,
    ) -> "TransitionDecision":
        if sequence < 1 or not actor_id.strip() or not rationale.strip():
            raise PathwayError("Transitions require a sequence, actor, and rationale")
        evidence_json = canonical_json(evidence)
        evidence_digest = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
        body = {
            "sequence": sequence,
            "edge_id": edge.edge_id,
            "from_node": edge.from_node,
            "to_node": edge.to_node,
            "outcome": edge.outcome.value,
            "actor_id": actor_id,
            "rationale": rationale,
            "evidence": json.loads(evidence_json),
            "evidence_checksum": evidence_digest,
            "pathway_checksum": pathway_checksum,
            "previous_decision_hash": previous_decision_hash,
            "decided_at": _json_value(decided_at),
        }
        return cls(
            sequence=sequence,
            edge_id=edge.edge_id,
            from_node=edge.from_node,
            to_node=edge.to_node,
            outcome=edge.outcome,
            actor_id=actor_id,
            rationale=rationale,
            evidence_json=evidence_json,
            evidence_checksum=evidence_digest,
            pathway_checksum=pathway_checksum,
            previous_decision_hash=previous_decision_hash,
            decision_hash=checksum(body),
            decided_at=decided_at,
        )

    @property
    def evidence(self) -> dict[str, Any]:
        return json.loads(self.evidence_json)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "edge_id": self.edge_id,
            "from_node": self.from_node,
            "to_node": self.to_node,
            "outcome": self.outcome.value,
            "actor_id": self.actor_id,
            "rationale": self.rationale,
            "evidence": self.evidence,
            "evidence_checksum": self.evidence_checksum,
            "pathway_checksum": self.pathway_checksum,
            "previous_decision_hash": self.previous_decision_hash,
            "decision_hash": self.decision_hash,
            "decided_at": _json_value(self.decided_at),
        }


def confirmed_fact_map(facts: Iterable[RecordFact]) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for fact in facts:
        if fact.status is FactStatus.CONFIRMED:
            resolved[fact.key] = _json_value(fact.value)
    return resolved


def confirmed_fact_evidence(facts: Iterable[RecordFact]) -> dict[str, Any]:
    """Canonical approval subject, including value and evidence provenance."""

    return {
        fact.key: fact.as_dict()
        for fact in facts
        if fact.status is FactStatus.CONFIRMED
    }


def approved_gate_set(
    approvals: Iterable[Approval], facts: Iterable[RecordFact] | None = None
) -> frozenset[str]:
    subject_checksum = (
        checksum(confirmed_fact_evidence(facts)) if facts is not None else None
    )
    return frozenset(
        approval.gate_key
        for approval in approvals
        if approval.status is ApprovalStatus.APPROVED
        and (
            subject_checksum is None
            or approval.subject_checksum == subject_checksum
        )
    )


@dataclass(frozen=True)
class PathwayRun:
    record_id: str
    pathway_family: str
    pathway_version: int
    pathway_checksum: str
    current_node: str
    entry_role: str = "author"
    status: RunStatus = RunStatus.ACTIVE
    cycle_number: int = 1
    decisions: tuple[TransitionDecision, ...] = field(default_factory=tuple)

    @classmethod
    def start(
        cls,
        record_id: str,
        definition: PathwayDefinition,
        *,
        entry_role: str = "author",
    ) -> "PathwayRun":
        if not record_id.strip():
            raise PathwayError("A pathway run requires a record id")
        return cls(
            record_id=record_id,
            pathway_family=definition.family_key,
            pathway_version=definition.version,
            pathway_checksum=definition.definition_checksum,
            current_node=definition.entry_for_role(entry_role),
            entry_role=entry_role,
        )

    def _assert_definition(self, definition: PathwayDefinition) -> None:
        if (
            definition.family_key != self.pathway_family
            or definition.version != self.pathway_version
            or definition.definition_checksum != self.pathway_checksum
        ):
            raise PathwayError("This run is pinned to a different pathway version")

    def available_edges(
        self,
        definition: PathwayDefinition,
        facts: Iterable[RecordFact],
        approvals: Iterable[Approval],
        *,
        _allow_legacy_readiness: bool = False,
    ) -> tuple[PathwayEdge, ...]:
        self._assert_definition(definition)
        fact_items = tuple(facts)
        approval_items = tuple(approvals)
        confirmed = confirmed_fact_map(fact_items)
        gates = approved_gate_set(approval_items, fact_items)
        if self.status is RunStatus.PAUSED:
            allowed_outcomes = {RouteOutcome.RESUME}
        else:
            allowed_outcomes = set(RouteOutcome) - {RouteOutcome.RESUME}

        proceed_evidence_is_current = (
            confirmed.get("stage_ready") is True
            and confirmed.get("stage_ready_node") == self.current_node
            and not isinstance(confirmed.get("stage_ready_cycle"), bool)
            and confirmed.get("stage_ready_cycle") == self.cycle_number
            and not any(confirmed.get(key) for key in PROCEED_BLOCKING_FACTS)
        )
        if (
            _allow_legacy_readiness
            and self.pathway_version == 1
            and "stage_ready_node" not in confirmed
            and "stage_ready_cycle" not in confirmed
        ):
            proceed_evidence_is_current = (
                confirmed.get("stage_ready") is True
                and not any(confirmed.get(key) for key in PROCEED_BLOCKING_FACTS)
            )
        return tuple(
            edge
            for edge in definition.edges
            if edge.from_node == self.current_node
            and edge.outcome in allowed_outcomes
            and (
                edge.outcome is not RouteOutcome.PROCEED
                or proceed_evidence_is_current
            )
            and evaluate_condition(edge.condition, confirmed, gates)
        )

    def transition(
        self,
        definition: PathwayDefinition,
        *,
        outcome: RouteOutcome | str,
        actor_id: str,
        rationale: str,
        facts: Iterable[RecordFact],
        approvals: Iterable[Approval],
        decided_at: datetime | None = None,
        _allow_legacy_readiness: bool = False,
    ) -> "PathwayRun":
        if self.status in {RunStatus.COMPLETE, RunStatus.WALKED_AWAY, RunStatus.NON_AI, RunStatus.RETIRED}:
            raise PathwayError("A terminal pathway run cannot transition")
        resolved_outcome = outcome if isinstance(outcome, RouteOutcome) else RouteOutcome(outcome)
        if self.status is RunStatus.PAUSED and resolved_outcome is not RouteOutcome.RESUME:
            raise PathwayError("A paused run must resume before another transition")
        if self.status is not RunStatus.PAUSED and resolved_outcome is RouteOutcome.RESUME:
            raise PathwayError("Only a paused run can resume")
        fact_items = tuple(facts)
        approval_items = tuple(approvals)
        edges = [
            edge
            for edge in self.available_edges(
                definition,
                fact_items,
                approval_items,
                _allow_legacy_readiness=_allow_legacy_readiness,
            )
            if edge.outcome is resolved_outcome
        ]
        if len(edges) != 1:
            raise PathwayError(
                "The requested outcome must resolve to exactly one eligible pathway edge"
            )
        edge = edges[0]
        confirmed = confirmed_fact_map(fact_items)
        confirmed_evidence = confirmed_fact_evidence(fact_items)
        approved = sorted(approved_gate_set(approval_items, fact_items))
        evidence = {
            "confirmed_facts": confirmed,
            "confirmed_fact_evidence": confirmed_evidence,
            "approved_gates": approved,
        }
        decision = TransitionDecision.build(
            sequence=len(self.decisions) + 1,
            edge=edge,
            actor_id=actor_id,
            rationale=rationale,
            evidence=evidence,
            pathway_checksum=self.pathway_checksum,
            previous_decision_hash=(self.decisions[-1].decision_hash if self.decisions else ""),
            decided_at=decided_at or datetime.now(timezone.utc),
        )
        node_kind = str(definition.nodes[edge.to_node].get("kind") or "review")
        next_status = RunStatus.ACTIVE
        if resolved_outcome is RouteOutcome.PAUSE:
            next_status = RunStatus.PAUSED
        elif edge.to_node == "walked_away" or node_kind == "walk_away":
            next_status = RunStatus.WALKED_AWAY
        elif edge.to_node == "non_ai" or node_kind == "non_ai":
            next_status = RunStatus.NON_AI
        elif edge.to_node == "retired" or node_kind == "retired":
            next_status = RunStatus.RETIRED
        elif node_kind == "terminal":
            next_status = RunStatus.COMPLETE
        cycle_number = self.cycle_number + (
            1
            if resolved_outcome
            in {RouteOutcome.NEGOTIATE_RETURN, RouteOutcome.REASSESS}
            else 0
        )
        return replace(
            self,
            current_node=edge.to_node,
            status=next_status,
            cycle_number=cycle_number,
            decisions=(*self.decisions, decision),
        )

    def replay(self, definition: PathwayDefinition) -> "PathwayRun":
        """Rebuild the run from stored evidence snapshots and verify every hash."""

        self._assert_definition(definition)
        rebuilt = PathwayRun.start(
            self.record_id, definition, entry_role=self.entry_role
        )
        for stored in self.decisions:
            if stored.previous_decision_hash != (
                rebuilt.decisions[-1].decision_hash if rebuilt.decisions else ""
            ):
                raise PathwayError("Transition decision hash chain is broken")
            stored_fact_evidence = stored.evidence.get("confirmed_fact_evidence")
            if isinstance(stored_fact_evidence, Mapping):
                facts = [
                    RecordFact(
                        key=key,
                        value=value["value"],
                        status=FactStatus(value["status"]),
                        source_event_ids=tuple(value.get("source_event_ids", ())),
                        confirmed_by=value["confirmed_by"],
                    )
                    for key, value in stored_fact_evidence.items()
                ]
            else:
                facts = [
                    RecordFact(
                        key=key,
                        value=value,
                        status=FactStatus.CONFIRMED,
                        confirmed_by="replay",
                    )
                    for key, value in stored.evidence.get("confirmed_facts", {}).items()
                ]
            subject = checksum(confirmed_fact_evidence(facts))
            approvals = [
                Approval(
                    gate_key=gate,
                    status=ApprovalStatus.APPROVED,
                    actor_id="replay",
                    subject_checksum=subject,
                    decided_at=stored.decided_at,
                )
                for gate in stored.evidence.get("approved_gates", [])
            ]
            rebuilt = rebuilt.transition(
                definition,
                outcome=stored.outcome,
                actor_id=stored.actor_id,
                rationale=stored.rationale,
                facts=facts,
                approvals=approvals,
                decided_at=stored.decided_at,
                _allow_legacy_readiness=True,
            )
            if rebuilt.decisions[-1].as_dict() != stored.as_dict():
                raise PathwayError("Stored transition does not replay exactly")
        return rebuilt


def default_pathway() -> PathwayDefinition:
    """The first versioned graph for adoption review and later reassessment."""

    stage_labels = {
        "entry": "Strategic fit",
        "redline": "Red line test",
        "stress": "Stress test",
        "cost_benefit": "Costs and benefits",
        "hidden_curriculum": "Hidden curriculum",
        "accountability": "Accountability",
        "internal_external_review": "Internal and external review",
        "synthesis": "Decision record",
        "pilot": "Bounded pilot",
        "monitoring": "Monitoring and reassessment",
        "non_ai": "Non-AI redesign",
        "walked_away": "Walk away",
        "retired": "Retired",
    }
    nodes = {
        key: {
            "kind": (
                "non_ai" if key == "non_ai" else
                "walk_away" if key == "walked_away" else
                "retired" if key == "retired" else
                "review"
            ),
            "label": label,
            "entry_roles": (
                ["reviewer"] if key == "internal_external_review" else
                ["monitor"] if key == "monitoring" else
                []
            ),
        }
        for key, label in stage_labels.items()
    }
    edges: list[dict[str, Any]] = []
    review_nodes = [
        "entry",
        "redline",
        "stress",
        "cost_benefit",
        "hidden_curriculum",
        "accountability",
        "internal_external_review",
        "synthesis",
        "pilot",
        "monitoring",
    ]
    forward = {
        "entry": "redline",
        "redline": "stress",
        "stress": "cost_benefit",
        "cost_benefit": "hidden_curriculum",
        "hidden_curriculum": "accountability",
        "accountability": "internal_external_review",
        "internal_external_review": "synthesis",
        "synthesis": "pilot",
        "pilot": "monitoring",
    }
    for node, target in forward.items():
        edges.append(
            {
                "id": f"{node}_proceed",
                "from": node,
                "to": target,
                "outcome": "proceed",
                "when": {
                    "all": [
                        {"fact": "stage_ready", "eq": True},
                        {"fact": "stage_ready_node", "eq": node},
                        {"fact": "stage_blocked", "eq": False},
                        {"approval": f"{node}_owner"},
                    ]
                },
            }
        )
    for node in review_nodes:
        edges.extend(
            [
                {
                    "id": f"{node}_negotiate",
                    "from": node,
                    "to": node,
                    "outcome": "negotiate_return",
                    "when": {},
                },
                {
                    "id": f"{node}_pause",
                    "from": node,
                    "to": node,
                    "outcome": "pause",
                    "when": {},
                },
                {
                    "id": f"{node}_resume",
                    "from": node,
                    "to": node,
                    "outcome": "resume",
                    "when": {},
                },
                {
                    "id": f"{node}_non_ai",
                    "from": node,
                    "to": "non_ai",
                    "outcome": "non_ai",
                    "when": {},
                },
                {
                    "id": f"{node}_walk_away",
                    "from": node,
                    "to": "walked_away",
                    "outcome": "walk_away",
                    "when": {},
                },
            ]
        )
    edges.extend(
        [
            {
                "id": "monitoring_reassess",
                "from": "monitoring",
                "to": "internal_external_review",
                "outcome": "reassess",
                "when": {},
            },
            {
                "id": "monitoring_retire",
                "from": "monitoring",
                "to": "retired",
                "outcome": "retire",
                "when": {"approval": "retirement_owner"},
            },
        ]
    )
    return PathwayDefinition.build(
        family_key="adoption_review",
        version=2,
        entry_node="entry",
        nodes=nodes,
        edges=edges,
    )


__all__ = [
    "Approval",
    "ApprovalStatus",
    "FactStatus",
    "PathwayDefinition",
    "PathwayEdge",
    "PathwayError",
    "PathwayRun",
    "RecordFact",
    "RouteOutcome",
    "RunStatus",
    "TransitionDecision",
    "UNGUIDED_CHECKPOINT_NODES",
    "approved_gate_set",
    "canonical_json",
    "checksum",
    "confirmed_fact_evidence",
    "confirmed_fact_map",
    "default_pathway",
    "evaluate_condition",
    "validate_condition",
]
