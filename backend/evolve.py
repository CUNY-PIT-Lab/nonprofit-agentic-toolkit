"""Create inert evolution proposals from authorized aggregate telemetry.

Run with ``python -m backend.evolve``. This maintenance command can aggregate
and propose. It has no authority to review, apply, rename, or deploy changes.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from sqlalchemy.orm import Session

from .config import Settings
from .database import build_database
from .evolution import (
    EvolutionCandidate,
    EvolutionRule,
    EvolutionWorker,
    NameSuggestion,
    ProjectionAuthorization,
    ProposalType,
    RolloutPlan,
    SemanticVersionMetadata,
    TelemetrySensitivity,
    checksum,
)
from .evolution_store import EvolutionStore
from .pathways import default_pathway


COMPONENT_DEFAULT_VERSIONS = {
    "interface.routing": "0.8.0",
    "product.identity": "0.8.0",
    "pathway.default": f"{default_pathway().version}.0.0",
}


def _bump_version(current: str, part: str) -> str:
    core = current.split("+", 1)[0].split("-", 1)[0]
    try:
        major, minor, patch = (int(item) for item in core.split("."))
    except (TypeError, ValueError) as exc:
        raise ValueError("Active component version is not semantic") from exc
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError("Unknown semantic version increment")


def rule_registry(
    cohort_key: str,
    current_versions: dict[str, str] | None = None,
) -> tuple[EvolutionRule, ...]:
    """Return the small, reviewed rule set shipped with this release."""

    versions = {**COMPONENT_DEFAULT_VERSIONS, **(current_versions or {})}
    interface_current = versions["interface.routing"]
    identity_current = versions["product.identity"]
    pathway_current = versions["pathway.default"]
    return (
        EvolutionRule(
            rule_id="interface.route_confusion.threshold",
            signal_event_type="interface.route_confusing",
            minimum_count=3,
            candidate=EvolutionCandidate(
                proposal_type=ProposalType.INTERFACE,
                title="Clarify route choices at the decision boundary",
                rationale=(
                    "Repeated aggregate route-confusion signals justify a bounded test "
                    "of clearer route labels and consequences."
                ),
                change_summary=(
                    "Test revised route explanations while preserving Proceed, "
                    "Negotiate and return, Walk Away, and the non-AI pathway."
                ),
            ),
            versions=SemanticVersionMetadata(
                component_key="interface.routing",
                current_version=interface_current,
                proposed_version=_bump_version(interface_current, "patch"),
                prompt_version="none",
                model_version="none",
            ),
            rollout_plan=RolloutPlan(
                rollout_target=(
                    "interface.routing@"
                    + _bump_version(interface_current, "patch")
                ),
                rollback_target=f"interface.routing@{interface_current}",
                cohort_key=cohort_key,
                max_percentage=20,
                evaluation_metric="route_completion_rate",
                guardrail_metric="route_confusion_rate",
                evaluation_window_hours=168,
            ),
            count_unit="consent_scopes",
        ),
        EvolutionRule(
            rule_id="name.fieldwork_loop.threshold",
            signal_event_type="name.preference.fieldwork_loop",
            minimum_count=3,
            candidate=EvolutionCandidate(
                proposal_type=ProposalType.NAME,
                title="Consider the Fieldwork Loop name",
                rationale=(
                    "A repeated, de-identified preference signal supports testing a name "
                    "that describes iterative fieldwork without claiming autonomous authority."
                ),
                change_summary=(
                    "Present the suggested name and aliases in a bounded beta rollout; "
                    "the software does not rename itself from this proposal."
                ),
                name_suggestion=NameSuggestion(
                    suggested_name="Fieldwork Loop",
                    aliases=("Toolkit Loop", "Reflexive Toolkit"),
                    rationale=(
                        "The label describes iterative, replayable inquiry across review cycles."
                    ),
                ),
            ),
            versions=SemanticVersionMetadata(
                component_key="product.identity",
                current_version=identity_current,
                proposed_version=_bump_version(identity_current, "minor"),
                prompt_version="naming-signals.v1",
                model_version="none",
            ),
            rollout_plan=RolloutPlan(
                rollout_target=(
                    "product.identity@" + _bump_version(identity_current, "minor")
                ),
                rollback_target=f"product.identity@{identity_current}",
                cohort_key=cohort_key,
                max_percentage=20,
                evaluation_metric="name_acceptance_rate",
                guardrail_metric="name_confusion_rate",
                evaluation_window_hours=168,
            ),
            count_unit="consent_scopes",
        ),
        EvolutionRule(
            rule_id="pathway.negotiate.threshold",
            signal_event_type="pathway.negotiate_selected",
            minimum_count=3,
            candidate=EvolutionCandidate(
                proposal_type=ProposalType.PATHWAY,
                title="Review repeated negotiate-and-return selections",
                rationale=(
                    "Repeated aggregate selections may indicate a useful return path or "
                    "an unclear readiness gate that warrants human review."
                ),
                change_summary=(
                    "Compare the pinned pathway with a proposed revision that makes the "
                    "return target and missing owner or evidence explicit."
                ),
            ),
            versions=SemanticVersionMetadata(
                component_key="pathway.default",
                current_version=pathway_current,
                proposed_version=_bump_version(pathway_current, "minor"),
                prompt_version="none",
                model_version="none",
            ),
            rollout_plan=RolloutPlan(
                rollout_target=(
                    "pathway.default@" + _bump_version(pathway_current, "minor")
                ),
                rollback_target=f"pathway.default@{pathway_current}",
                cohort_key=cohort_key,
                max_percentage=10,
                evaluation_metric="return_completion_rate",
                guardrail_metric="repeat_negotiate_rate",
                evaluation_window_hours=336,
            ),
            count_unit="consent_scopes",
        ),
    )


def execute(
    settings: Settings,
    session_factory: Callable[[], Session],
) -> dict[str, Any]:
    """Aggregate authorized signals and idempotently save proposal records."""

    if not settings.telemetry_enabled:
        raise RuntimeError("PRODUCT_TELEMETRY_ENABLED is required")
    store = EvolutionStore(session_factory)
    projection = store.authorized_projection(
        ProjectionAuthorization(
            principal_id="worker:evolution-maintenance",
            purpose="evolution",
            max_sensitivity=TelemetrySensitivity.RESTRICTED,
            allowed_cohorts=frozenset({settings.telemetry_cohort}),
            policy_version="telemetry-projection.v1",
        ),
        minimum_cell_size=settings.telemetry_min_cell_size,
    )
    current_versions = {
        component: store.active_component_version(component, default)
        for component, default in COMPONENT_DEFAULT_VERSIONS.items()
    }
    proposals = EvolutionWorker().evaluate(
        projection,
        rule_registry(settings.telemetry_cohort, current_versions),
    )
    saved = tuple(store.save_proposal(proposal) for proposal in proposals)
    proposal_items = [
        {
            "id": proposal.proposal_id,
            "type": proposal.proposal_type.value,
            "checksum": proposal.proposal_checksum,
        }
        for proposal in sorted(saved, key=lambda item: item.proposal_id)
    ]
    return {
        "aggregate": {
            "checksum": checksum(projection.summary),
            "event_count": projection.evidence.event_count,
            "minimum_cell_size": settings.telemetry_min_cell_size,
            "proposal_count": len(proposal_items),
        },
        "projection": {
            "checksum": projection.projection_checksum,
            "event_count": projection.evidence.event_count,
        },
        "proposals": proposal_items,
    }


def main() -> None:
    try:
        settings = Settings.from_env()
        if not settings.telemetry_enabled:
            raise RuntimeError("PRODUCT_TELEMETRY_ENABLED is required")
        engine, session_factory = build_database(settings.database_url)
        try:
            result = execute(settings, session_factory)
        finally:
            engine.dispose()
    except RuntimeError as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        raise SystemExit(2) from error
    except Exception as error:
        print(json.dumps({"error": "evolution command failed"}), file=sys.stderr)
        raise SystemExit(2) from error
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
