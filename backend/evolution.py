"""Governed, privacy-bounded product evolution.

This module intentionally knows nothing about HTTP, fieldwork transcripts, or
deployment providers. Product events contain only numeric measures and short
categorical dimensions. A worker can inspect only a de-identified aggregate,
and its output is an inert proposal until a human reviews it and a separate
rollout action is recorded.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


class EvolutionError(ValueError):
    """Raised when telemetry or an evolution artifact breaks its contract."""


class ProposalType(str, Enum):
    PATHWAY = "pathway"
    PROMPT = "prompt"
    INTERFACE = "interface"
    NAME = "name"


class TelemetrySensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


SENSITIVITY_RANK = {
    TelemetrySensitivity.PUBLIC: 0,
    TelemetrySensitivity.INTERNAL: 1,
    TelemetrySensitivity.RESTRICTED: 2,
}


class ConsentBasis(str, Enum):
    NOT_REQUIRED = "not_required"
    GRANTED = "granted"


class ConsentStatus(str, Enum):
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"


class ReviewOutcome(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class RolloutActionKind(str, Enum):
    ROLLOUT = "rollout"
    ROLLBACK = "rollback"


class EvaluationOutcome(str, Enum):
    MET = "met"
    NOT_MET = "not_met"
    INCONCLUSIVE = "inconclusive"


_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,119}$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_FORBIDDEN_TELEMETRY_KEYS = (
    "transcript",
    "content",
    "message",
    "prompt_text",
    "response_text",
    "raw",
    "email",
    "phone",
    "address",
    "ip_address",
    "user_id",
    "participant",
    "person_name",
    "secret",
    "token",
)
_PURPOSES = frozenset({"analytics", "evolution", "quality"})
_HUMAN_ROLES = frozenset({"owner", "reviewer", "admin", "maintainer"})


def _aware_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EvolutionError("Timestamps must include a UTC offset")
    return value.isoformat(timespec="microseconds")


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _aware_timestamp(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise EvolutionError(f"Unsupported JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_token(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not _TOKEN.fullmatch(normalized):
        raise EvolutionError(f"{label} must be a short categorical token")
    return normalized


def _validate_safe_key(key: str) -> str:
    normalized = _require_token(key, "Telemetry keys")
    if any(fragment in normalized for fragment in _FORBIDDEN_TELEMETRY_KEYS):
        raise EvolutionError(f"Telemetry key {normalized!r} could contain raw or identifying data")
    return normalized


def _normalize_metrics(metrics: Mapping[str, int | float | bool]) -> dict[str, int | float | bool]:
    if len(metrics) > 32:
        raise EvolutionError("A product event can contain at most 32 measures")
    normalized: dict[str, int | float | bool] = {}
    for raw_key, value in metrics.items():
        key = _validate_safe_key(str(raw_key))
        if not isinstance(value, (int, float, bool)):
            raise EvolutionError("Telemetry measures must be numeric or boolean")
        if isinstance(value, float) and not math.isfinite(value):
            raise EvolutionError("Telemetry measures must be finite")
        normalized[key] = value
    return dict(sorted(normalized.items()))


def _normalize_dimensions(dimensions: Mapping[str, str]) -> dict[str, str]:
    if len(dimensions) > 24:
        raise EvolutionError("A product event can contain at most 24 dimensions")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in dimensions.items():
        key = _validate_safe_key(str(raw_key))
        normalized[key] = _require_token(str(raw_value), f"Dimension {key}")
    return dict(sorted(normalized.items()))


@dataclass(frozen=True)
class TelemetryManifest:
    """Collection boundary carried by every product event."""

    consent_basis: ConsentBasis
    sensitivity: TelemetrySensitivity
    allowed_purposes: tuple[str, ...]
    consent_scope_id: str = ""
    deidentified: bool = True
    schema_version: str = "product.telemetry.v1"
    app_version: str = "local"
    policy_version: str = "telemetry-policy.v1"

    def __post_init__(self) -> None:
        if not self.deidentified:
            raise EvolutionError("Product evolution accepts only de-identified telemetry")
        purposes = tuple(_require_token(item, "Telemetry purposes") for item in self.allowed_purposes)
        if not purposes or len(purposes) != len(set(purposes)):
            raise EvolutionError("Telemetry purposes must be non-empty and unique")
        if not set(purposes) <= _PURPOSES:
            raise EvolutionError("Unknown telemetry purpose")
        object.__setattr__(self, "allowed_purposes", purposes)
        if self.consent_basis is ConsentBasis.GRANTED:
            object.__setattr__(
                self,
                "consent_scope_id",
                _require_token(self.consent_scope_id, "Consent scope"),
            )
        elif self.consent_scope_id:
            raise EvolutionError("A not-required event cannot carry a consent subject scope")
        for value in (self.schema_version, self.app_version, self.policy_version):
            if not value.strip() or len(value) > 120:
                raise EvolutionError("Telemetry version metadata cannot be blank or oversized")

    def as_dict(self) -> dict[str, Any]:
        return {
            "consent_basis": self.consent_basis.value,
            "consent_scope_id": self.consent_scope_id,
            "sensitivity": self.sensitivity.value,
            "allowed_purposes": list(self.allowed_purposes),
            "deidentified": self.deidentified,
            "schema_version": self.schema_version,
            "app_version": self.app_version,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class ProductTelemetryEvent:
    event_id: str
    sequence: int
    event_type: str
    product_area: ProposalType
    cohort_key: str
    metrics_json: str
    dimensions_json: str
    manifest: TelemetryManifest
    occurred_at: datetime
    committed_at: datetime
    previous_event_hash: str
    event_hash: str

    @classmethod
    def build(
        cls,
        *,
        event_id: str,
        sequence: int,
        event_type: str,
        product_area: ProposalType | str,
        cohort_key: str,
        metrics: Mapping[str, int | float | bool],
        dimensions: Mapping[str, str],
        manifest: TelemetryManifest,
        occurred_at: datetime,
        committed_at: datetime,
        previous_event_hash: str = "",
    ) -> "ProductTelemetryEvent":
        if not event_id.strip() or len(event_id) > 120 or sequence < 1:
            raise EvolutionError("Product events require an id and positive sequence")
        kind = _require_token(event_type, "Event type")
        area = product_area if isinstance(product_area, ProposalType) else ProposalType(product_area)
        cohort = _require_token(cohort_key, "Cohort")
        if occurred_at > committed_at:
            raise EvolutionError("Product events cannot be committed before they occur")
        _aware_timestamp(occurred_at)
        _aware_timestamp(committed_at)
        if previous_event_hash and not re.fullmatch(r"[0-9a-f]{64}", previous_event_hash):
            raise EvolutionError("Previous event hashes must be SHA-256 hex")
        metrics_json = canonical_json(_normalize_metrics(metrics))
        dimensions_json = canonical_json(_normalize_dimensions(dimensions))
        body = {
            "event_id": event_id,
            "sequence": sequence,
            "event_type": kind,
            "product_area": area.value,
            "cohort_key": cohort,
            "metrics": json.loads(metrics_json),
            "dimensions": json.loads(dimensions_json),
            "manifest": manifest.as_dict(),
            "occurred_at": occurred_at,
            "committed_at": committed_at,
            "previous_event_hash": previous_event_hash,
        }
        return cls(
            event_id=event_id,
            sequence=sequence,
            event_type=kind,
            product_area=area,
            cohort_key=cohort,
            metrics_json=metrics_json,
            dimensions_json=dimensions_json,
            manifest=manifest,
            occurred_at=occurred_at,
            committed_at=committed_at,
            previous_event_hash=previous_event_hash,
            event_hash=checksum(body),
        )

    @property
    def metrics(self) -> dict[str, int | float | bool]:
        return json.loads(self.metrics_json)

    @property
    def dimensions(self) -> dict[str, str]:
        return json.loads(self.dimensions_json)

    def verify(self, previous_hash: str) -> None:
        rebuilt = ProductTelemetryEvent.build(
            event_id=self.event_id,
            sequence=self.sequence,
            event_type=self.event_type,
            product_area=self.product_area,
            cohort_key=self.cohort_key,
            metrics=self.metrics,
            dimensions=self.dimensions,
            manifest=self.manifest,
            occurred_at=self.occurred_at,
            committed_at=self.committed_at,
            previous_event_hash=previous_hash,
        )
        if previous_hash != self.previous_event_hash or rebuilt.event_hash != self.event_hash:
            raise EvolutionError("Product telemetry hash chain is invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "product_area": self.product_area.value,
            "cohort_key": self.cohort_key,
            "metrics": self.metrics,
            "dimensions": self.dimensions,
            "manifest": self.manifest.as_dict(),
            "occurred_at": _aware_timestamp(self.occurred_at),
            "committed_at": _aware_timestamp(self.committed_at),
            "previous_event_hash": self.previous_event_hash,
            "event_hash": self.event_hash,
        }


@dataclass(frozen=True)
class TelemetryConsentDecision:
    decision_id: str
    consent_scope_id: str
    status: ConsentStatus
    actor_id: str
    actor_role: str
    reason_code: str
    decided_at: datetime
    decision_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.decision_id.strip() or not self.actor_id.strip() or not self.actor_role.strip():
            raise EvolutionError("Consent decisions require ids and actor provenance")
        object.__setattr__(self, "consent_scope_id", _require_token(self.consent_scope_id, "Consent scope"))
        object.__setattr__(self, "reason_code", _require_token(self.reason_code, "Consent reason"))
        _aware_timestamp(self.decided_at)
        object.__setattr__(
            self,
            "decision_hash",
            checksum(
                {
                    "decision_id": self.decision_id,
                    "consent_scope_id": self.consent_scope_id,
                    "status": self.status.value,
                    "actor_id": self.actor_id,
                    "actor_role": self.actor_role,
                    "reason_code": self.reason_code,
                    "decided_at": self.decided_at,
                }
            ),
        )


@dataclass(frozen=True)
class ProjectionAuthorization:
    principal_id: str
    purpose: str
    max_sensitivity: TelemetrySensitivity
    allowed_cohorts: frozenset[str]
    policy_version: str
    allow_all_cohorts: bool = False

    def __post_init__(self) -> None:
        if not self.principal_id.strip() or not self.policy_version.strip():
            raise EvolutionError("Projection authorization requires a principal and policy version")
        object.__setattr__(self, "purpose", _require_token(self.purpose, "Projection purpose"))
        if self.purpose not in _PURPOSES:
            raise EvolutionError("Unknown projection purpose")
        normalized = frozenset(_require_token(item, "Authorized cohorts") for item in self.allowed_cohorts)
        object.__setattr__(self, "allowed_cohorts", normalized)
        if not self.allow_all_cohorts and not normalized:
            raise EvolutionError("Projection authorization must name at least one cohort")


@dataclass(frozen=True)
class AggregateEvidenceManifest:
    aggregation_version: str
    authorization_policy_version: str
    purpose: str
    event_count: int
    event_hash_root: str
    cohort_keys: tuple[str, ...]
    window_start: str
    window_end: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "aggregation_version": self.aggregation_version,
            "authorization_policy_version": self.authorization_policy_version,
            "purpose": self.purpose,
            "event_count": self.event_count,
            "event_hash_root": self.event_hash_root,
            "cohort_keys": list(self.cohort_keys),
            "window_start": self.window_start,
            "window_end": self.window_end,
        }


@dataclass(frozen=True)
class DeidentifiedProjection:
    summary_json: str
    evidence: AggregateEvidenceManifest
    projection_checksum: str
    deidentified: bool = True

    @property
    def summary(self) -> dict[str, Any]:
        return json.loads(self.summary_json)

    def count_for(self, event_type: str, *, count_unit: str = "events") -> int | None:
        count_key = {
            "events": "event_counts",
            "consent_scopes": "consent_scope_counts",
        }.get(count_unit)
        if count_key is None:
            raise EvolutionError("Unknown aggregate count unit")
        cell = self.summary.get(count_key, {}).get(event_type)
        if not cell or cell.get("suppressed"):
            return None
        return int(cell["count"])

    def signal_evidence_for(self, event_type: str) -> dict[str, Any]:
        """Return checksum-verified evidence scoped to one categorical signal."""

        normalized = _require_token(event_type, "Signal event")
        stored = self.summary.get("signal_evidence", {}).get(normalized)
        if not isinstance(stored, dict) or "checksum" not in stored:
            raise EvolutionError("Signal-scoped projection evidence is unavailable")
        body = {key: value for key, value in stored.items() if key != "checksum"}
        if stored["checksum"] != checksum(body):
            raise EvolutionError("Signal-scoped projection evidence is invalid")
        return dict(stored)

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "evidence": self.evidence.as_dict(),
            "projection_checksum": self.projection_checksum,
            "deidentified": True,
        }


def _decimal_text(value: Decimal) -> str:
    result = format(value.normalize(), "f")
    return "0" if result in {"-0", ""} else result


def aggregate_telemetry(
    events: Iterable[ProductTelemetryEvent],
    *,
    authorization: ProjectionAuthorization,
    consent_status: Mapping[str, ConsentStatus],
    minimum_cell_size: int = 3,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> DeidentifiedProjection:
    """Build a deterministic, small-cell-suppressed projection."""

    if minimum_cell_size < 2:
        raise EvolutionError("Small-cell suppression must be at least two")
    ordered = sorted(events, key=lambda item: item.sequence)
    prior = ""
    for expected, event in enumerate(ordered, start=1):
        if event.sequence != expected:
            raise EvolutionError("Product telemetry sequence is not contiguous")
        event.verify(prior)
        prior = event.event_hash
    selected: list[ProductTelemetryEvent] = []
    for event in ordered:
        manifest = event.manifest
        if not manifest.deidentified or authorization.purpose not in manifest.allowed_purposes:
            continue
        if SENSITIVITY_RANK[manifest.sensitivity] > SENSITIVITY_RANK[authorization.max_sensitivity]:
            continue
        if not authorization.allow_all_cohorts and event.cohort_key not in authorization.allowed_cohorts:
            continue
        if window_start and event.occurred_at < window_start:
            continue
        if window_end and event.occurred_at > window_end:
            continue
        if (
            manifest.consent_basis is ConsentBasis.GRANTED
            and consent_status.get(manifest.consent_scope_id) is not ConsentStatus.GRANTED
        ):
            continue
        selected.append(event)

    counts: dict[str, int] = {}
    consent_scopes: dict[str, set[str]] = {}
    dimensions: dict[str, dict[str, int]] = {}
    measures: dict[str, list[Decimal]] = {}
    signal_events: dict[str, list[ProductTelemetryEvent]] = {}
    for event in selected:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
        signal_events.setdefault(event.event_type, []).append(event)
        if event.manifest.consent_basis is ConsentBasis.GRANTED:
            consent_scopes.setdefault(event.event_type, set()).add(
                event.manifest.consent_scope_id
            )
        for key, value in event.dimensions.items():
            bucket = dimensions.setdefault(key, {})
            bucket[value] = bucket.get(value, 0) + 1
        for key, value in event.metrics.items():
            if isinstance(value, bool):
                decimal_value = Decimal(1 if value else 0)
            else:
                decimal_value = Decimal(str(value))
            measures.setdefault(key, []).append(decimal_value)

    def cell(value: int) -> dict[str, Any]:
        return (
            {"count": value, "suppressed": False}
            if value >= minimum_cell_size
            else {"count": None, "suppressed": True}
        )

    measure_summary: dict[str, Any] = {}
    for key, values in sorted(measures.items()):
        if len(values) < minimum_cell_size:
            measure_summary[key] = {"count": None, "suppressed": True}
            continue
        total = sum(values, Decimal(0))
        measure_summary[key] = {
            "count": len(values),
            "min": _decimal_text(min(values)),
            "max": _decimal_text(max(values)),
            "sum": _decimal_text(total),
            "mean": _decimal_text(total / Decimal(len(values))),
            "suppressed": False,
        }

    signal_evidence: dict[str, dict[str, Any]] = {}
    for event_type, relevant in sorted(signal_events.items()):
        relevant_scopes = {
            item.manifest.consent_scope_id
            for item in relevant
            if item.manifest.consent_basis is ConsentBasis.GRANTED
        }
        if (
            len(relevant) < minimum_cell_size
            or len(relevant_scopes) < minimum_cell_size
        ):
            # Do not publish a root, chronology, or exact count for a signal
            # whose event or distinct-consent-scope cell is suppressed.
            continue
        body = {
            "aggregation_version": "telemetry.signal-aggregate.v1",
            "authorization_policy_version": authorization.policy_version,
            "purpose": authorization.purpose,
            "event_type": event_type,
            "event_count": len(relevant),
            "consent_scope_count": len(relevant_scopes),
            "event_hash_root": checksum([item.event_hash for item in relevant]),
            "cohort_keys": sorted({item.cohort_key for item in relevant}),
            "window_start": _aware_timestamp(min(item.occurred_at for item in relevant)),
            "window_end": _aware_timestamp(max(item.occurred_at for item in relevant)),
        }
        signal_evidence[event_type] = {**body, "checksum": checksum(body)}

    summary = {
        "event_counts": {key: cell(value) for key, value in sorted(counts.items())},
        "consent_scope_counts": {
            key: cell(len(values)) for key, values in sorted(consent_scopes.items())
        },
        "dimension_counts": {
            key: {value: cell(count) for value, count in sorted(bucket.items())}
            for key, bucket in sorted(dimensions.items())
        },
        "measure_summaries": measure_summary,
        "signal_evidence": signal_evidence,
        "minimum_cell_size": minimum_cell_size,
    }
    event_hash_root = checksum([item.event_hash for item in selected])
    start = window_start or (min((item.occurred_at for item in selected), default=None))
    end = window_end or (max((item.occurred_at for item in selected), default=None))
    evidence = AggregateEvidenceManifest(
        aggregation_version="telemetry.aggregate.v1",
        authorization_policy_version=authorization.policy_version,
        purpose=authorization.purpose,
        event_count=len(selected),
        event_hash_root=event_hash_root,
        cohort_keys=tuple(sorted({item.cohort_key for item in selected})),
        window_start=_aware_timestamp(start) if start else "empty",
        window_end=_aware_timestamp(end) if end else "empty",
    )
    summary_json = canonical_json(summary)
    projection_checksum = checksum({"summary": summary, "evidence": evidence.as_dict()})
    return DeidentifiedProjection(summary_json, evidence, projection_checksum)


def _parse_semver(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if not match:
        raise EvolutionError("Versions must use semantic versioning")
    return tuple(int(match.group(index)) for index in (1, 2, 3))


@dataclass(frozen=True)
class SemanticVersionMetadata:
    component_key: str
    current_version: str
    proposed_version: str
    schema_version: str = "evolution.proposal.v1"
    worker_version: str = "evolution-worker.v1"
    prompt_version: str = "none"
    model_version: str = "none"

    def __post_init__(self) -> None:
        object.__setattr__(self, "component_key", _require_token(self.component_key, "Component"))
        current = _parse_semver(self.current_version)
        proposed = _parse_semver(self.proposed_version)
        if proposed <= current:
            raise EvolutionError("A proposal version must advance the current semantic version")
        for value in (self.schema_version, self.worker_version, self.prompt_version, self.model_version):
            if not value.strip() or len(value) > 120:
                raise EvolutionError("Evolution version metadata cannot be blank or oversized")

    def as_dict(self) -> dict[str, str]:
        return {
            "component_key": self.component_key,
            "current_version": self.current_version,
            "proposed_version": self.proposed_version,
            "schema_version": self.schema_version,
            "worker_version": self.worker_version,
            "prompt_version": self.prompt_version,
            "model_version": self.model_version,
        }


@dataclass(frozen=True)
class RolloutPlan:
    rollout_target: str
    rollback_target: str
    cohort_key: str
    max_percentage: int
    evaluation_metric: str
    guardrail_metric: str
    evaluation_window_hours: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.rollout_target, "Rollout target"),
            (self.rollback_target, "Rollback target"),
        ):
            if not value.strip() or len(value) > 160 or "\n" in value:
                raise EvolutionError(f"{label} is invalid")
        if self.rollout_target == self.rollback_target:
            raise EvolutionError("Rollout and rollback targets must differ")
        object.__setattr__(self, "cohort_key", _require_token(self.cohort_key, "Rollout cohort"))
        object.__setattr__(self, "evaluation_metric", _validate_safe_key(self.evaluation_metric))
        object.__setattr__(self, "guardrail_metric", _validate_safe_key(self.guardrail_metric))
        if not 1 <= self.max_percentage <= 100:
            raise EvolutionError("Rollout percentage must be between 1 and 100")
        if not 1 <= self.evaluation_window_hours <= 24 * 180:
            raise EvolutionError("Evaluation window is outside the supported range")

    def as_dict(self) -> dict[str, Any]:
        return {
            "rollout_target": self.rollout_target,
            "rollback_target": self.rollback_target,
            "cohort_key": self.cohort_key,
            "max_percentage": self.max_percentage,
            "evaluation_metric": self.evaluation_metric,
            "guardrail_metric": self.guardrail_metric,
            "evaluation_window_hours": self.evaluation_window_hours,
        }


@dataclass(frozen=True)
class NameSuggestion:
    suggested_name: str
    aliases: tuple[str, ...]
    rationale: str

    def __post_init__(self) -> None:
        names = (self.suggested_name, *self.aliases)
        if not 2 <= len(self.suggested_name.strip()) <= 80:
            raise EvolutionError("Suggested product names must be 2-80 characters")
        if len(self.aliases) > 5 or len({item.casefold() for item in names}) != len(names):
            raise EvolutionError("Name aliases must be unique and limited to five")
        if any(not 2 <= len(item.strip()) <= 80 or "\n" in item for item in names):
            raise EvolutionError("Names and aliases must be short labels")
        if not 8 <= len(self.rationale.strip()) <= 500:
            raise EvolutionError("A name suggestion requires a bounded rationale")

    def as_dict(self) -> dict[str, Any]:
        return {
            "suggested_name": self.suggested_name.strip(),
            "aliases": [item.strip() for item in self.aliases],
            "rationale": self.rationale.strip(),
        }


@dataclass(frozen=True)
class EvolutionCandidate:
    proposal_type: ProposalType
    title: str
    rationale: str
    change_summary: str
    name_suggestion: NameSuggestion | None = None

    def __post_init__(self) -> None:
        if not 4 <= len(self.title.strip()) <= 160:
            raise EvolutionError("Proposal titles must be 4-160 characters")
        if not 12 <= len(self.rationale.strip()) <= 1200:
            raise EvolutionError("Proposals require a bounded rationale")
        if not 8 <= len(self.change_summary.strip()) <= 1000:
            raise EvolutionError("Proposals require a bounded change summary")
        if (self.proposal_type is ProposalType.NAME) != (self.name_suggestion is not None):
            raise EvolutionError("Only name proposals carry a self-name suggestion")


@dataclass(frozen=True)
class EvolutionEvidenceManifest:
    projection_checksum: str
    event_hash_root: str
    event_count: int
    aggregation_version: str
    authorization_policy_version: str
    rule_id: str
    signal_event_type: str
    observed_count: int
    minimum_count: int
    count_unit: str = "events"
    evidence_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.projection_checksum):
            raise EvolutionError("Projection checksums must be SHA-256 hex")
        object.__setattr__(self, "rule_id", _require_token(self.rule_id, "Evolution rule"))
        object.__setattr__(self, "signal_event_type", _require_token(self.signal_event_type, "Signal event"))
        if self.count_unit not in {"events", "consent_scopes"}:
            raise EvolutionError("Unknown evolution evidence count unit")
        object.__setattr__(self, "evidence_checksum", checksum(self.as_dict(include_checksum=False)))

    def as_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        value = {
            "projection_checksum": self.projection_checksum,
            "event_hash_root": self.event_hash_root,
            "event_count": self.event_count,
            "aggregation_version": self.aggregation_version,
            "authorization_policy_version": self.authorization_policy_version,
            "rule_id": self.rule_id,
            "signal_event_type": self.signal_event_type,
            "observed_count": self.observed_count,
            "minimum_count": self.minimum_count,
            "count_unit": self.count_unit,
        }
        if include_checksum:
            value["evidence_checksum"] = self.evidence_checksum
        return value


@dataclass(frozen=True)
class EvolutionProposal:
    proposal_id: str
    proposal_type: ProposalType
    title: str
    rationale: str
    change_summary: str
    versions: SemanticVersionMetadata
    rollout_plan: RolloutPlan
    evidence: EvolutionEvidenceManifest
    created_at: datetime
    name_suggestion: NameSuggestion | None
    proposal_checksum: str

    @classmethod
    def build(
        cls,
        *,
        candidate: EvolutionCandidate,
        versions: SemanticVersionMetadata,
        rollout_plan: RolloutPlan,
        evidence: EvolutionEvidenceManifest,
        created_at: datetime,
    ) -> "EvolutionProposal":
        _aware_timestamp(created_at)
        content = {
            "proposal_type": candidate.proposal_type.value,
            "title": candidate.title.strip(),
            "rationale": candidate.rationale.strip(),
            "change_summary": candidate.change_summary.strip(),
            "versions": versions.as_dict(),
            "rollout_plan": rollout_plan.as_dict(),
            "evidence": evidence.as_dict(),
            "name_suggestion": candidate.name_suggestion.as_dict() if candidate.name_suggestion else None,
            "created_at": created_at,
        }
        digest = checksum(content)
        return cls(
            proposal_id=f"evo-{digest[:24]}",
            proposal_type=candidate.proposal_type,
            title=candidate.title.strip(),
            rationale=candidate.rationale.strip(),
            change_summary=candidate.change_summary.strip(),
            versions=versions,
            rollout_plan=rollout_plan,
            evidence=evidence,
            created_at=created_at,
            name_suggestion=candidate.name_suggestion,
            proposal_checksum=digest,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "proposal_type": self.proposal_type.value,
            "title": self.title,
            "rationale": self.rationale,
            "change_summary": self.change_summary,
            "versions": self.versions.as_dict(),
            "rollout_plan": self.rollout_plan.as_dict(),
            "evidence": self.evidence.as_dict(),
            "created_at": _aware_timestamp(self.created_at),
            "name_suggestion": self.name_suggestion.as_dict() if self.name_suggestion else None,
            "proposal_checksum": self.proposal_checksum,
        }


@dataclass(frozen=True)
class EvolutionRule:
    rule_id: str
    signal_event_type: str
    minimum_count: int
    candidate: EvolutionCandidate
    versions: SemanticVersionMetadata
    rollout_plan: RolloutPlan
    count_unit: str = "events"

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", _require_token(self.rule_id, "Evolution rule"))
        object.__setattr__(self, "signal_event_type", _require_token(self.signal_event_type, "Signal event"))
        if self.minimum_count < 2:
            raise EvolutionError("Evolution rules cannot trigger on a single event")
        if self.count_unit not in {"events", "consent_scopes"}:
            raise EvolutionError("Evolution rules must count events or distinct consent scopes")


class EvolutionWorker:
    """A bounded proposal engine; it cannot review, apply, or deploy changes."""

    def evaluate(
        self,
        projection: DeidentifiedProjection,
        rules: Sequence[EvolutionRule],
    ) -> tuple[EvolutionProposal, ...]:
        if not isinstance(projection, DeidentifiedProjection) or not projection.deidentified:
            raise EvolutionError("Evolution workers accept only de-identified projections")
        proposals: list[EvolutionProposal] = []
        for rule in sorted(rules, key=lambda item: item.rule_id):
            observed = projection.count_for(
                rule.signal_event_type,
                count_unit=rule.count_unit,
            )
            if observed is None or observed < rule.minimum_count:
                continue
            scoped = projection.signal_evidence_for(rule.signal_event_type)
            scoped_observed = (
                scoped["event_count"]
                if rule.count_unit == "events"
                else scoped["consent_scope_count"]
            )
            if int(scoped_observed) != observed:
                raise EvolutionError("Signal-scoped aggregate count is inconsistent")
            evidence = EvolutionEvidenceManifest(
                projection_checksum=scoped["checksum"],
                event_hash_root=scoped["event_hash_root"],
                event_count=int(scoped["event_count"]),
                aggregation_version=scoped["aggregation_version"],
                authorization_policy_version=scoped[
                    "authorization_policy_version"
                ],
                rule_id=rule.rule_id,
                signal_event_type=rule.signal_event_type,
                observed_count=observed,
                minimum_count=rule.minimum_count,
                count_unit=rule.count_unit,
            )
            created_at = datetime.fromisoformat(scoped["window_end"])
            proposals.append(
                EvolutionProposal.build(
                    candidate=rule.candidate,
                    versions=rule.versions,
                    rollout_plan=rule.rollout_plan,
                    evidence=evidence,
                    created_at=created_at,
                )
            )
        return tuple(proposals)


@dataclass(frozen=True)
class HumanActor:
    actor_id: str
    role: str

    def __post_init__(self) -> None:
        if not self.actor_id.strip() or self.role not in _HUMAN_ROLES:
            raise EvolutionError("Evolution decisions require an authorized human role")


@dataclass(frozen=True)
class ProposalReview:
    review_id: str
    proposal_id: str
    proposal_checksum: str
    outcome: ReviewOutcome
    actor: HumanActor
    rationale: str
    decided_at: datetime
    review_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.review_id.strip() or not 8 <= len(self.rationale.strip()) <= 1000:
            raise EvolutionError("Reviews require an id and rationale")
        _aware_timestamp(self.decided_at)
        object.__setattr__(self, "review_checksum", checksum(self.as_dict(include_checksum=False)))

    def as_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        value = {
            "review_id": self.review_id,
            "proposal_id": self.proposal_id,
            "proposal_checksum": self.proposal_checksum,
            "outcome": self.outcome.value,
            "actor_id": self.actor.actor_id,
            "actor_role": self.actor.role,
            "rationale": self.rationale.strip(),
            "decided_at": _aware_timestamp(self.decided_at),
        }
        if include_checksum:
            value["review_checksum"] = self.review_checksum
        return value


@dataclass(frozen=True)
class RolloutAction:
    action_id: str
    proposal_id: str
    proposal_checksum: str
    review_checksum: str
    action: RolloutActionKind
    target: str
    actor: HumanActor
    performed_at: datetime
    action_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.action_id.strip() or not self.target.strip() or len(self.target) > 160:
            raise EvolutionError("Rollout actions require an id and bounded target")
        _aware_timestamp(self.performed_at)
        object.__setattr__(self, "action_checksum", checksum(self.as_dict(include_checksum=False)))

    def as_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        value = {
            "action_id": self.action_id,
            "proposal_id": self.proposal_id,
            "proposal_checksum": self.proposal_checksum,
            "review_checksum": self.review_checksum,
            "action": self.action.value,
            "target": self.target,
            "actor_id": self.actor.actor_id,
            "actor_role": self.actor.role,
            "performed_at": _aware_timestamp(self.performed_at),
        }
        if include_checksum:
            value["action_checksum"] = self.action_checksum
        return value


@dataclass(frozen=True)
class EvaluationResult:
    evaluation_id: str
    proposal_id: str
    rollout_action_id: str
    outcome: EvaluationOutcome
    metrics_json: str
    evaluator: HumanActor
    rationale: str
    evidence_projection_checksum: str
    recorded_at: datetime
    evaluation_checksum: str = field(init=False)

    @classmethod
    def build(
        cls,
        *,
        evaluation_id: str,
        proposal_id: str,
        rollout_action_id: str,
        outcome: EvaluationOutcome | str,
        metrics: Mapping[str, int | float | bool],
        evaluator: HumanActor,
        rationale: str,
        evidence_projection_checksum: str,
        recorded_at: datetime,
    ) -> "EvaluationResult":
        return cls(
            evaluation_id=evaluation_id,
            proposal_id=proposal_id,
            rollout_action_id=rollout_action_id,
            outcome=outcome if isinstance(outcome, EvaluationOutcome) else EvaluationOutcome(outcome),
            metrics_json=canonical_json(_normalize_metrics(metrics)),
            evaluator=evaluator,
            rationale=rationale,
            evidence_projection_checksum=evidence_projection_checksum,
            recorded_at=recorded_at,
        )

    def __post_init__(self) -> None:
        if not self.evaluation_id.strip() or not self.rollout_action_id.strip():
            raise EvolutionError("Evaluation results require stable ids")
        if not 8 <= len(self.rationale.strip()) <= 1000:
            raise EvolutionError("Evaluation results require a bounded rationale")
        if not re.fullmatch(r"[0-9a-f]{64}", self.evidence_projection_checksum):
            raise EvolutionError("Evaluations require a projection checksum")
        _aware_timestamp(self.recorded_at)
        object.__setattr__(self, "evaluation_checksum", checksum(self.as_dict(include_checksum=False)))

    @property
    def metrics(self) -> dict[str, int | float | bool]:
        return json.loads(self.metrics_json)

    def as_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        value = {
            "evaluation_id": self.evaluation_id,
            "proposal_id": self.proposal_id,
            "rollout_action_id": self.rollout_action_id,
            "outcome": self.outcome.value,
            "metrics": self.metrics,
            "evaluator_id": self.evaluator.actor_id,
            "evaluator_role": self.evaluator.role,
            "rationale": self.rationale.strip(),
            "evidence_projection_checksum": self.evidence_projection_checksum,
            "recorded_at": _aware_timestamp(self.recorded_at),
        }
        if include_checksum:
            value["evaluation_checksum"] = self.evaluation_checksum
        return value
