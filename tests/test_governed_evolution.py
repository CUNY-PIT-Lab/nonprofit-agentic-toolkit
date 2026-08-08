#!/usr/bin/env python3
"""Focused privacy, replay, and governance checks for product evolution."""

from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.evolution import (
    ConsentBasis,
    ConsentStatus,
    EvaluationResult,
    EvolutionCandidate,
    EvolutionError,
    EvolutionRule,
    EvolutionWorker,
    HumanActor,
    NameSuggestion,
    ProductTelemetryEvent,
    ProjectionAuthorization,
    ProposalType,
    ReviewOutcome,
    RolloutActionKind,
    RolloutPlan,
    SemanticVersionMetadata,
    TelemetryManifest,
    TelemetrySensitivity,
)
from backend.evolution_store import (
    EvolutionProposalRow,
    EvolutionReviewRow,
    EvolutionRolloutActionRow,
    EvolutionStore,
    ProductTelemetryEventRow,
)
from backend.models import Base


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def manifest(
    *,
    consent: bool = False,
    sensitivity: TelemetrySensitivity = TelemetrySensitivity.INTERNAL,
    consent_scope_id: str = "beta-scope-7",
) -> TelemetryManifest:
    return TelemetryManifest(
        consent_basis=ConsentBasis.GRANTED if consent else ConsentBasis.NOT_REQUIRED,
        consent_scope_id=consent_scope_id if consent else "",
        sensitivity=sensitivity,
        allowed_purposes=("analytics", "evolution"),
        app_version="0.8.0",
    )


def authorization(
    *, max_sensitivity: TelemetrySensitivity = TelemetrySensitivity.RESTRICTED
) -> ProjectionAuthorization:
    return ProjectionAuthorization(
        principal_id="worker:evolution",
        purpose="evolution",
        max_sensitivity=max_sensitivity,
        allowed_cohorts=frozenset({"beta"}),
        policy_version="telemetry-projection.v1",
    )


def name_rule() -> EvolutionRule:
    return EvolutionRule(
        rule_id="name.preference.threshold",
        signal_event_type="name.preference.fieldwork_loop",
        minimum_count=3,
        candidate=EvolutionCandidate(
            proposal_type=ProposalType.NAME,
            title="Consider the Fieldwork Loop name",
            rationale=(
                "A repeated, de-identified preference signal supports testing a name "
                "that describes iterative fieldwork without claiming autonomous authority."
            ),
            change_summary="Present the suggested name and aliases in a bounded beta rollout.",
            name_suggestion=NameSuggestion(
                suggested_name="Fieldwork Loop",
                aliases=("Toolkit Loop", "Reflexive Toolkit"),
                rationale="The label describes iterative, replayable inquiry across review cycles.",
            ),
        ),
        versions=SemanticVersionMetadata(
            component_key="product.identity",
            current_version="0.8.0",
            proposed_version="0.9.0",
            prompt_version="naming-signals.v1",
            model_version="none",
        ),
        rollout_plan=RolloutPlan(
            rollout_target="product.identity@0.9.0",
            rollback_target="product.identity@0.8.0",
            cohort_key="beta",
            max_percentage=20,
            evaluation_metric="name_acceptance_rate",
            guardrail_metric="name_confusion_rate",
            evaluation_window_hours=168,
        ),
        count_unit="consent_scopes",
    )


def alternate_name_rule() -> EvolutionRule:
    return EvolutionRule(
        rule_id="name.preference.reflexive_fieldwork",
        signal_event_type="name.preference.fieldwork_loop",
        minimum_count=3,
        candidate=EvolutionCandidate(
            proposal_type=ProposalType.NAME,
            title="Consider the Reflexive Fieldwork name",
            rationale=(
                "The later aggregate review supports testing a more explicit identity "
                "while preserving the earlier name as a reversible release."
            ),
            change_summary="Test a second approved identity version with an explicit rollback.",
            name_suggestion=NameSuggestion(
                suggested_name="Reflexive Fieldwork",
                aliases=("Fieldwork Loop", "Reflexive Toolkit"),
                rationale="The name makes the toolkit's reflexive fieldwork method explicit.",
            ),
        ),
        versions=SemanticVersionMetadata(
            component_key="product.identity",
            current_version="0.9.0",
            proposed_version="1.0.0",
            prompt_version="naming-signals.v2",
            model_version="none",
        ),
        rollout_plan=RolloutPlan(
            rollout_target="product.identity@1.0.0",
            rollback_target="product.identity@0.9.0",
            cohort_key="beta",
            max_percentage=20,
            evaluation_metric="name_acceptance_rate",
            guardrail_metric="name_confusion_rate",
            evaluation_window_hours=168,
        ),
        count_unit="consent_scopes",
    )


def stale_name_rule() -> EvolutionRule:
    return EvolutionRule(
        rule_id="name.preference.stale_baseline",
        signal_event_type="name.preference.fieldwork_loop",
        minimum_count=3,
        candidate=EvolutionCandidate(
            proposal_type=ProposalType.NAME,
            title="Stale identity proposal",
            rationale=(
                "This fixture deliberately carries an outdated semantic baseline "
                "so rollout validation can reject it before append."
            ),
            change_summary="Attempt an identity release from an untrusted old baseline.",
            name_suggestion=NameSuggestion(
                suggested_name="Stale Fieldwork",
                aliases=("Old Toolkit",),
                rationale="This fixture must never become the active application identity.",
            ),
        ),
        versions=SemanticVersionMetadata(
            component_key="product.identity",
            current_version="0.7.0",
            proposed_version="0.9.0",
            prompt_version="naming-signals.stale",
            model_version="none",
        ),
        rollout_plan=RolloutPlan(
            rollout_target="product.identity@0.9.0",
            rollback_target="product.identity@0.7.0",
            cohort_key="beta",
            max_percentage=10,
            evaluation_metric="name_acceptance_rate",
            guardrail_metric="name_confusion_rate",
            evaluation_window_hours=24,
        ),
        count_unit="consent_scopes",
    )


def competing_name_rule() -> EvolutionRule:
    return EvolutionRule(
        rule_id="name.preference.concurrent_alternative",
        signal_event_type="name.preference.fieldwork_loop",
        minimum_count=3,
        candidate=EvolutionCandidate(
            proposal_type=ProposalType.NAME,
            title="Consider the Fieldwork Cycles name",
            rationale=(
                "A concurrent release fixture verifies that two proposals cannot both "
                "advance one component from the same governed baseline."
            ),
            change_summary="Test a competing identity target from the same baseline.",
            name_suggestion=NameSuggestion(
                suggested_name="Fieldwork Cycles",
                aliases=("Fieldwork Toolkit",),
                rationale="The fixture represents a distinct but reversible identity target.",
            ),
        ),
        versions=SemanticVersionMetadata(
            component_key="product.identity",
            current_version="0.8.0",
            proposed_version="1.1.0",
            prompt_version="naming-signals.concurrent",
            model_version="none",
        ),
        rollout_plan=RolloutPlan(
            rollout_target="product.identity@1.1.0",
            rollback_target="product.identity@0.8.0",
            cohort_key="beta",
            max_percentage=10,
            evaluation_metric="name_acceptance_rate",
            guardrail_metric="name_confusion_rate",
            evaluation_window_hours=24,
        ),
        count_unit="consent_scopes",
    )


class GovernedEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{self.tmp.name}/evolution.db",
            connect_args={"check_same_thread": False},
        )
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self.store = EvolutionStore(
            self.sessions,
            component_baselines={"product.identity": "0.8.0"},
        )

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def grant(self) -> None:
        for index, scope in enumerate(
            ("beta-scope-7", "beta-scope-8", "beta-scope-9")
        ):
            self.store.append_consent_decision(
                decision_id=f"consent-grant-{index}",
                consent_scope_id=scope,
                status=ConsentStatus.GRANTED,
                actor_id=f"participant-pseudonym-{index + 7}",
                actor_role="participant",
                reason_code="beta-opt-in",
                decided_at=NOW + timedelta(seconds=index),
            )

    def append_signals(
        self,
        *,
        event_type: str = "name.preference.fieldwork_loop",
        event_manifest: TelemetryManifest | None = None,
        count: int = 3,
    ) -> None:
        for index in range(count):
            chosen = event_manifest or manifest(
                consent=True,
                consent_scope_id=("beta-scope-7", "beta-scope-8", "beta-scope-9")[
                    index % 3
                ],
            )
            self.store.append_product_event(
                event_id=f"event-{event_type}-{index}",
                event_type=event_type,
                product_area=ProposalType.NAME,
                cohort_key="beta",
                metrics={"elapsed_ms": 10 + index * 10, "completed": True},
                dimensions={"pathway_stage": "redline", "client_kind": "web"},
                manifest=chosen,
                occurred_at=NOW + timedelta(minutes=index + 1),
                committed_at=NOW + timedelta(minutes=index + 1, seconds=1),
            )

    def proposal(self):
        projection = self.store.authorized_projection(authorization())
        return EvolutionWorker().evaluate(projection, [name_rule()])[0]

    def proposal_for(self, rule: EvolutionRule):
        projection = self.store.authorized_projection(authorization())
        return EvolutionWorker().evaluate(projection, [rule])[0]

    def action_count(self) -> int:
        with self.sessions() as session:
            return len(session.scalars(select(EvolutionRolloutActionRow)).all())

    def proposal_count(self) -> int:
        with self.sessions() as session:
            return len(session.scalars(select(EvolutionProposalRow)).all())

    def review_count(self) -> int:
        with self.sessions() as session:
            return len(session.scalars(select(EvolutionReviewRow)).all())

    def approved_name_rollout(self):
        self.grant()
        self.append_signals()
        proposal = self.store.save_proposal(self.proposal())
        review = self.store.record_review(
            proposal.proposal_id,
            review_id="identity-review",
            outcome="approved",
            actor=HumanActor("owner-1", "owner"),
            rationale="The owner approves this exact identity proposal for bounded rollout.",
            decided_at=NOW + timedelta(hours=2),
        )
        action = self.store.record_rollout_action(
            proposal.proposal_id,
            action="rollout",
            action_id="identity-rollout",
            actor=HumanActor("maintainer-1", "maintainer"),
            performed_at=NOW + timedelta(days=1),
        )
        return proposal, review, action

    def test_telemetry_contract_rejects_raw_or_identifying_content(self):
        with self.assertRaises(EvolutionError):
            TelemetryManifest(
                consent_basis=ConsentBasis.NOT_REQUIRED,
                sensitivity=TelemetrySensitivity.INTERNAL,
                allowed_purposes=("evolution",),
                deidentified=False,
            )
        with self.assertRaises(EvolutionError):
            ProductTelemetryEvent.build(
                event_id="bad-raw",
                sequence=1,
                event_type="interface.help_requested",
                product_area="interface",
                cohort_key="beta",
                metrics={},
                dimensions={"transcript": "participant said something"},
                manifest=manifest(),
                occurred_at=NOW,
                committed_at=NOW,
            )
        with self.assertRaises(EvolutionError):
            ProductTelemetryEvent.build(
                event_id="bad-prose",
                sequence=1,
                event_type="interface.help_requested",
                product_area="interface",
                cohort_key="beta",
                metrics={},
                dimensions={"route": "this is an open ended answer"},
                manifest=manifest(),
                occurred_at=NOW,
                committed_at=NOW,
            )

    def test_consent_is_required_and_withdrawal_removes_prior_events_from_projection(self):
        with self.assertRaises(EvolutionError):
            self.append_signals(count=1)
        self.grant()
        self.append_signals()
        before = self.store.authorized_projection(authorization())
        self.assertEqual(before.evidence.event_count, 3)
        self.assertEqual(before.count_for("name.preference.fieldwork_loop"), 3)

        for index, scope in enumerate(
            ("beta-scope-7", "beta-scope-8", "beta-scope-9")
        ):
            self.store.append_consent_decision(
                decision_id=f"consent-withdraw-{index}",
                consent_scope_id=scope,
                status="withdrawn",
                actor_id=f"participant-pseudonym-{index + 7}",
                actor_role="participant",
                reason_code="beta-opt-out",
                decided_at=NOW + timedelta(hours=1, seconds=index),
            )
        after = self.store.authorized_projection(authorization())
        self.assertEqual(after.evidence.event_count, 0)
        self.assertIsNone(after.count_for("name.preference.fieldwork_loop"))

    def test_aggregation_is_deterministic_suppressed_and_sensitivity_bounded(self):
        self.grant()
        self.append_signals()
        self.append_signals(
            event_type="interface.restricted_error",
            event_manifest=manifest(
                consent=True, sensitivity=TelemetrySensitivity.RESTRICTED
            ),
            count=2,
        )
        first = self.store.authorized_projection(
            authorization(max_sensitivity=TelemetrySensitivity.INTERNAL)
        )
        restarted = EvolutionStore(self.sessions).authorized_projection(
            authorization(max_sensitivity=TelemetrySensitivity.INTERNAL)
        )
        self.assertEqual(first.projection_checksum, restarted.projection_checksum)
        self.assertEqual(first.summary_json, restarted.summary_json)
        self.assertEqual(first.summary["measure_summaries"]["elapsed_ms"]["mean"], "20")
        self.assertNotIn("interface.restricted_error", first.summary["event_counts"])

        restricted = self.store.authorized_projection(authorization())
        cell = restricted.summary["event_counts"]["interface.restricted_error"]
        self.assertTrue(cell["suppressed"])
        self.assertIsNone(cell["count"])

    def test_worker_consumes_only_projection_and_creates_inert_self_name_proposal(self):
        self.grant()
        self.append_signals()
        projection = self.store.authorized_projection(authorization())
        worker = EvolutionWorker()
        with self.assertRaises(EvolutionError):
            worker.evaluate([], [name_rule()])  # type: ignore[arg-type]

        first = worker.evaluate(projection, [name_rule()])
        second = worker.evaluate(projection, [name_rule()])
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        proposal = first[0]
        self.assertEqual(proposal.proposal_type, ProposalType.NAME)
        self.assertEqual(proposal.name_suggestion.suggested_name, "Fieldwork Loop")
        self.assertEqual(len(proposal.proposal_checksum), 64)
        self.assertEqual(len(proposal.evidence.evidence_checksum), 64)

    def test_human_review_is_required_and_approval_does_not_apply(self):
        self.grant()
        self.append_signals()
        proposal = self.store.save_proposal(self.proposal())
        with self.assertRaises(EvolutionError):
            HumanActor("agent:worker", "agent")
        with self.assertRaises(EvolutionError):
            self.store.record_rollout_action(
                proposal.proposal_id,
                action="rollout",
                actor=HumanActor("owner-1", "owner"),
                performed_at=NOW + timedelta(days=1),
            )

        review = self.store.record_review(
            proposal.proposal_id,
            review_id="review-1",
            outcome=ReviewOutcome.APPROVED,
            actor=HumanActor("owner-1", "owner"),
            rationale="The owner approves a limited beta test with the documented rollback.",
            decided_at=NOW + timedelta(hours=2),
        )
        self.assertEqual(review.outcome, ReviewOutcome.APPROVED)
        self.assertEqual(self.store.rollout_actions(proposal.proposal_id), ())

    def test_backdated_review_appends_nothing_and_identity_stays_resolvable(self):
        self.grant()
        self.append_signals()
        proposal = self.store.save_proposal(self.proposal())

        with self.assertRaisesRegex(EvolutionError, "cannot precede proposal creation"):
            self.store.record_review(
                proposal.proposal_id,
                review_id="backdated-identity-review",
                outcome="approved",
                actor=HumanActor("owner-1", "owner"),
                rationale="This deliberately backdated review must not enter immutable history.",
                decided_at=proposal.created_at - timedelta(microseconds=1),
            )

        self.assertEqual(self.proposal_count(), 1)
        self.assertEqual(self.review_count(), 0)
        self.assertEqual(
            self.store.active_identity("Agentic Toolkit", "0.8.0")["source"],
            "default",
        )

    def test_invalid_identity_type_appends_nothing_and_reader_stays_resolvable(self):
        self.grant()
        self.append_signals()
        rule = name_rule()
        invalid = EvolutionRule(
            rule_id="identity.invalid.interface_type",
            signal_event_type=rule.signal_event_type,
            minimum_count=rule.minimum_count,
            candidate=EvolutionCandidate(
                proposal_type=ProposalType.INTERFACE,
                title="Invalid identity interface proposal",
                rationale=(
                    "This fixture uses a non-name proposal type for the specialized "
                    "product identity component and must be rejected before append."
                ),
                change_summary="Attempt to write incompatible product identity metadata.",
            ),
            versions=rule.versions,
            rollout_plan=rule.rollout_plan,
            count_unit=rule.count_unit,
        )

        with self.assertRaisesRegex(EvolutionError, "type provenance"):
            self.store.save_proposal(self.proposal_for(invalid))

        self.assertEqual(self.proposal_count(), 0)
        self.assertEqual(
            self.store.active_identity("Agentic Toolkit", "0.8.0")["source"],
            "default",
        )

    def test_invalid_component_targets_append_nothing_and_readers_stay_resolvable(self):
        self.grant()
        self.append_signals()
        identity = name_rule()
        invalid_identity = EvolutionRule(
            rule_id="identity.invalid.rollout_target",
            signal_event_type=identity.signal_event_type,
            minimum_count=identity.minimum_count,
            candidate=identity.candidate,
            versions=identity.versions,
            rollout_plan=RolloutPlan(
                rollout_target="identity.preview@0.9.0",
                rollback_target=identity.rollout_plan.rollback_target,
                cohort_key=identity.rollout_plan.cohort_key,
                max_percentage=identity.rollout_plan.max_percentage,
                evaluation_metric=identity.rollout_plan.evaluation_metric,
                guardrail_metric=identity.rollout_plan.guardrail_metric,
                evaluation_window_hours=identity.rollout_plan.evaluation_window_hours,
            ),
            count_unit=identity.count_unit,
        )

        with self.assertRaisesRegex(EvolutionError, "target provenance"):
            self.store.save_proposal(self.proposal_for(invalid_identity))

        self.assertEqual(self.proposal_count(), 0)
        self.assertEqual(
            self.store.active_identity("Agentic Toolkit", "0.8.0")["source"],
            "default",
        )

        invalid_interface = EvolutionRule(
            rule_id="interface.invalid.rollout_target",
            signal_event_type=identity.signal_event_type,
            minimum_count=identity.minimum_count,
            candidate=EvolutionCandidate(
                proposal_type=ProposalType.INTERFACE,
                title="Invalid interface target proposal",
                rationale=(
                    "This fixture proves the component and semantic-version target "
                    "invariant applies outside the specialized identity reader too."
                ),
                change_summary="Attempt to write an incompatible interface target.",
            ),
            versions=SemanticVersionMetadata(
                component_key="interface.routing",
                current_version="0.8.0",
                proposed_version="0.8.1",
            ),
            rollout_plan=RolloutPlan(
                rollout_target="interface.preview@0.8.1",
                rollback_target="interface.routing@0.8.0",
                cohort_key="beta",
                max_percentage=20,
                evaluation_metric="route_completion_rate",
                guardrail_metric="route_confusion_rate",
                evaluation_window_hours=168,
            ),
            count_unit=identity.count_unit,
        )
        with self.assertRaisesRegex(EvolutionError, "target provenance"):
            self.store.save_proposal(self.proposal_for(invalid_interface))
        self.assertEqual(self.proposal_count(), 0)
        self.assertEqual(
            self.store.active_component_version("interface.routing", "0.8.0"),
            "0.8.0",
        )

    def test_valid_review_retry_remains_idempotent(self):
        self.grant()
        self.append_signals()
        proposal = self.store.save_proposal(self.proposal())
        arguments = {
            "review_id": "idempotent-identity-review",
            "outcome": "approved",
            "actor": HumanActor("owner-1", "owner"),
            "rationale": "The exact same valid human review may be retried safely.",
            "decided_at": NOW + timedelta(hours=2),
        }

        first = self.store.record_review(proposal.proposal_id, **arguments)
        retried = self.store.record_review(proposal.proposal_id, **arguments)

        self.assertEqual(retried.review_checksum, first.review_checksum)
        self.assertEqual(self.review_count(), 1)
        self.assertEqual(
            self.store.active_identity("Agentic Toolkit", "0.8.0")["source"],
            "default",
        )

    def test_rollout_before_review_time_appends_nothing_and_identity_stays_resolvable(self):
        self.grant()
        self.append_signals()
        proposal = self.store.save_proposal(self.proposal())
        self.store.record_review(
            proposal.proposal_id,
            review_id="future-review",
            outcome="approved",
            actor=HumanActor("owner-1", "owner"),
            rationale="The approval is valid only from its recorded decision time onward.",
            decided_at=NOW + timedelta(hours=2),
        )
        with self.assertRaisesRegex(EvolutionError, "cannot precede"):
            self.store.record_rollout_action(
                proposal.proposal_id,
                action="rollout",
                action_id="pre-review-rollout",
                actor=HumanActor("maintainer-1", "maintainer"),
                performed_at=NOW + timedelta(hours=1),
            )
        self.assertEqual(self.action_count(), 0)
        resolved = self.store.active_identity("Agentic Toolkit", "0.8.0")
        self.assertEqual(resolved["display_name"], "Agentic Toolkit")
        self.assertEqual(resolved["source"], "default")

    def test_stale_first_rollout_cannot_override_trusted_component_baseline(self):
        self.grant()
        self.append_signals()
        proposal = self.store.save_proposal(self.proposal_for(stale_name_rule()))
        self.store.record_review(
            proposal.proposal_id,
            review_id="approve-stale-baseline",
            outcome="approved",
            actor=HumanActor("owner-1", "owner"),
            rationale="Approval cannot make an outdated semantic baseline current.",
            decided_at=NOW + timedelta(hours=2),
        )
        with self.assertRaisesRegex(EvolutionError, "stale"):
            self.store.record_rollout_action(
                proposal.proposal_id,
                action="rollout",
                action_id="stale-first-rollout",
                actor=HumanActor("maintainer-1", "maintainer"),
                performed_at=NOW + timedelta(days=1),
            )
        self.assertEqual(self.action_count(), 0)
        self.assertEqual(
            self.store.active_identity("Agentic Toolkit", "0.8.0")["display_name"],
            "Agentic Toolkit",
        )

    def test_rollout_writer_requires_explicit_trusted_component_baseline(self):
        self.grant()
        self.append_signals()
        proposal = self.store.save_proposal(self.proposal())
        self.store.record_review(
            proposal.proposal_id,
            review_id="baseline-required-review",
            outcome="approved",
            actor=HumanActor("owner-1", "owner"),
            rationale="This approval does not supply the component's trusted baseline.",
            decided_at=NOW + timedelta(hours=2),
        )
        unconfigured = EvolutionStore(self.sessions)
        with self.assertRaisesRegex(EvolutionError, "trusted component baseline"):
            unconfigured.record_rollout_action(
                proposal.proposal_id,
                action="rollout",
                action_id="missing-baseline-rollout",
                actor=HumanActor("maintainer-1", "maintainer"),
                performed_at=NOW + timedelta(days=1),
            )
        self.assertEqual(self.action_count(), 0)
        self.assertEqual(
            self.store.active_identity("Agentic Toolkit", "0.8.0")["source"],
            "default",
        )

    def test_same_action_key_is_concurrent_idempotent_and_conflicts_fail_closed(self):
        self.grant()
        self.append_signals()
        proposal = self.store.save_proposal(self.proposal())
        self.store.record_review(
            proposal.proposal_id,
            review_id="concurrent-review",
            outcome="approved",
            actor=HumanActor("owner-1", "owner"),
            rationale="The owner approves one rollout action under one stable key.",
            decided_at=NOW + timedelta(hours=2),
        )

        def append(offset: int):
            return self.store.record_rollout_action(
                proposal.proposal_id,
                action="rollout",
                action_id="shared-rollout-key",
                actor=HumanActor("maintainer-1", "maintainer"),
                performed_at=NOW + timedelta(days=1, minutes=offset),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            actions = list(executor.map(append, (0, 1)))
        self.assertEqual(actions[0].action_checksum, actions[1].action_checksum)
        self.assertEqual(self.action_count(), 1)
        self.assertEqual(
            self.store.active_identity("Agentic Toolkit", "0.8.0")["display_name"],
            "Fieldwork Loop",
        )

        with self.assertRaisesRegex(EvolutionError, "idempotency key conflicts"):
            self.store.record_rollout_action(
                proposal.proposal_id,
                action="rollout",
                action_id="shared-rollout-key",
                actor=HumanActor("different-maintainer", "maintainer"),
                performed_at=NOW + timedelta(days=2),
            )
        self.assertEqual(self.action_count(), 1)
        self.assertEqual(
            self.store.active_identity("Agentic Toolkit", "0.8.0")["source"],
            "approved_rollout",
        )

    def test_component_lock_serializes_concurrent_competing_rollouts(self):
        self.grant()
        self.append_signals()
        first = self.store.save_proposal(self.proposal_for(name_rule()))
        second = self.store.save_proposal(self.proposal_for(competing_name_rule()))
        for proposal, suffix in ((first, "first"), (second, "second")):
            self.store.record_review(
                proposal.proposal_id,
                review_id=f"concurrent-component-review-{suffix}",
                outcome="approved",
                actor=HumanActor("owner-1", "owner"),
                rationale="The fixture approves a competing rollout for lock validation.",
                decided_at=NOW + timedelta(hours=2),
            )

        barrier = threading.Barrier(2)

        def attempt(proposal, suffix: str):
            barrier.wait()
            try:
                result = self.store.record_rollout_action(
                    proposal.proposal_id,
                    action="rollout",
                    action_id=f"concurrent-component-rollout-{suffix}",
                    actor=HumanActor("maintainer-1", "maintainer"),
                    performed_at=NOW + timedelta(days=1),
                )
                return "stored", result.action_checksum
            except EvolutionError as exc:
                return "rejected", str(exc)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(attempt, first, "first"),
                executor.submit(attempt, second, "second"),
            )
            results = [future.result() for future in futures]

        self.assertEqual(sorted(result[0] for result in results), ["rejected", "stored"])
        rejected = next(result[1] for result in results if result[0] == "rejected")
        self.assertIn("stale", rejected)
        self.assertEqual(self.action_count(), 1)
        resolved = self.store.active_identity("Agentic Toolkit", "0.8.0")
        self.assertIn(resolved["display_name"], {"Fieldwork Loop", "Fieldwork Cycles"})
        self.assertIn(resolved["semantic_version"], {"0.9.0", "1.1.0"})
        self.assertEqual(resolved["source"], "approved_rollout")

    def test_explicit_rollout_evaluation_and_rollback_are_separate_append_only_facts(self):
        self.grant()
        self.append_signals()
        proposal = self.store.save_proposal(self.proposal())
        self.store.record_review(
            proposal.proposal_id,
            review_id="review-1",
            outcome="approved",
            actor=HumanActor("reviewer-1", "reviewer"),
            rationale="The reviewer authorizes only the bounded rollout described in the plan.",
            decided_at=NOW + timedelta(hours=2),
        )
        rollout = self.store.record_rollout_action(
            proposal.proposal_id,
            action=RolloutActionKind.ROLLOUT,
            action_id="rollout-1",
            actor=HumanActor("maintainer-1", "maintainer"),
            performed_at=NOW + timedelta(days=1),
        )
        self.assertEqual(rollout.target, "product.identity@0.9.0")
        projection = self.store.authorized_projection(authorization())
        evaluation = EvaluationResult.build(
            evaluation_id="evaluation-1",
            proposal_id=proposal.proposal_id,
            rollout_action_id=rollout.action_id,
            outcome="not_met",
            metrics={"name_acceptance_rate": 0.25, "name_confusion_rate": 0.5},
            evaluator=HumanActor("reviewer-1", "reviewer"),
            rationale="The guardrail exceeded the agreed threshold during the beta window.",
            evidence_projection_checksum=projection.projection_checksum,
            recorded_at=NOW + timedelta(days=8),
        )
        self.store.record_evaluation(evaluation)
        rollback = self.store.record_rollout_action(
            proposal.proposal_id,
            action="rollback",
            action_id="rollback-1",
            actor=HumanActor("maintainer-1", "maintainer"),
            performed_at=NOW + timedelta(days=8, minutes=1),
        )
        self.assertEqual(rollback.target, "product.identity@0.8.0")
        self.assertEqual(
            [item.action for item in self.store.rollout_actions(proposal.proposal_id)],
            [RolloutActionKind.ROLLOUT, RolloutActionKind.ROLLBACK],
        )

    def test_non_active_rollback_appends_nothing_and_identity_stays_resolvable(self):
        self.grant()
        self.append_signals()
        first = self.store.save_proposal(self.proposal_for(name_rule()))
        second = self.store.save_proposal(self.proposal_for(alternate_name_rule()))
        self.store.record_review(
            first.proposal_id,
            review_id="review-non-active-first",
            outcome="approved",
            actor=HumanActor("owner-1", "owner"),
            rationale="The first identity release has an explicit reversible approval.",
            decided_at=NOW + timedelta(hours=2),
        )
        self.store.record_rollout_action(
            first.proposal_id,
            action="rollout",
            action_id="rollout-non-active-first",
            actor=HumanActor("maintainer-1", "maintainer"),
            performed_at=NOW + timedelta(days=1),
        )
        self.store.record_review(
            second.proposal_id,
            review_id="review-non-active-second",
            outcome="approved",
            actor=HumanActor("owner-1", "owner"),
            rationale="The second identity release has its own reversible approval.",
            decided_at=NOW + timedelta(days=1, hours=1),
        )
        self.store.record_rollout_action(
            second.proposal_id,
            action="rollout",
            action_id="rollout-non-active-second",
            actor=HumanActor("maintainer-1", "maintainer"),
            performed_at=NOW + timedelta(days=2),
        )

        with self.assertRaisesRegex(EvolutionError, "active component rollout"):
            self.store.record_rollout_action(
                first.proposal_id,
                action="rollback",
                action_id="rollback-non-active-first",
                actor=HumanActor("maintainer-1", "maintainer"),
                performed_at=NOW + timedelta(days=3),
            )

        self.assertEqual(self.action_count(), 2)
        resolved = self.store.active_identity("Agentic Toolkit", "0.8.0")
        self.assertEqual(resolved["display_name"], "Reflexive Fieldwork")
        self.assertEqual(resolved["semantic_version"], "1.0.0")
        self.assertEqual(resolved["source"], "approved_rollout")

    def test_backdated_rollback_cannot_reorder_component_history(self):
        proposal, _review, _rollout = self.approved_name_rollout()
        with self.assertRaisesRegex(EvolutionError, "chronology"):
            self.store.record_rollout_action(
                proposal.proposal_id,
                action="rollback",
                action_id="backdated-rollback",
                actor=HumanActor("maintainer-1", "maintainer"),
                performed_at=NOW + timedelta(hours=3),
            )

        self.assertEqual(self.action_count(), 1)
        resolved = self.store.active_identity("Agentic Toolkit", "0.8.0")
        self.assertEqual(resolved["display_name"], "Fieldwork Loop")
        self.assertEqual(resolved["source"], "approved_rollout")

    def test_rejection_cannot_roll_out(self):
        self.grant()
        self.append_signals()
        proposal = self.store.save_proposal(self.proposal())
        self.store.record_review(
            proposal.proposal_id,
            review_id="review-reject",
            outcome="rejected",
            actor=HumanActor("owner-1", "owner"),
            rationale="The proposed identity is too close to an existing community project.",
            decided_at=NOW + timedelta(hours=2),
        )
        with self.assertRaises(EvolutionError):
            self.store.record_rollout_action(
                proposal.proposal_id,
                action="rollout",
                actor=HumanActor("maintainer-1", "maintainer"),
                performed_at=NOW + timedelta(days=1),
            )

    def test_active_identity_stays_default_until_explicit_approved_rollout(self):
        default = {
            "display_name": "Agentic Toolkit",
            "aliases": [],
            "semantic_version": "0.8.0",
            "proposal_checksum": None,
            "action_checksums": [],
            "source": "default",
        }
        self.assertEqual(
            self.store.active_identity("Agentic Toolkit", "0.8.0"),
            default,
        )
        self.grant()
        self.append_signals()
        proposal = self.store.save_proposal(self.proposal())
        self.store.record_review(
            proposal.proposal_id,
            review_id="identity-approval-only",
            outcome="approved",
            actor=HumanActor("owner-1", "owner"),
            rationale="The proposal is approved, but no rollout has been authorized yet.",
            decided_at=NOW + timedelta(hours=2),
        )
        self.assertEqual(
            self.store.active_identity("Agentic Toolkit", "0.8.0"),
            default,
        )

    def test_active_identity_exposes_only_bounded_name_and_provenance(self):
        proposal, _review, action = self.approved_name_rollout()
        resolved = self.store.active_identity("Agentic Toolkit", "0.8.0")
        self.assertEqual(
            set(resolved),
            {
                "display_name",
                "aliases",
                "semantic_version",
                "proposal_checksum",
                "action_checksums",
                "source",
            },
        )
        self.assertEqual(resolved["display_name"], "Fieldwork Loop")
        self.assertEqual(resolved["aliases"], ["Toolkit Loop", "Reflexive Toolkit"])
        self.assertEqual(resolved["semantic_version"], "0.9.0")
        self.assertEqual(resolved["proposal_checksum"], proposal.proposal_checksum)
        self.assertEqual(resolved["action_checksums"], [action.action_checksum])
        self.assertEqual(resolved["source"], "approved_rollout")

    def test_identity_rollback_restores_prior_rollout_then_default(self):
        self.grant()
        self.append_signals()
        first = self.store.save_proposal(self.proposal_for(name_rule()))
        second = self.store.save_proposal(self.proposal_for(alternate_name_rule()))
        self.store.record_review(
            first.proposal_id,
            review_id="review-first-name",
            outcome="approved",
            actor=HumanActor("owner-1", "owner"),
            rationale="The owner approves the first reversible identity rollout.",
            decided_at=NOW + timedelta(hours=2),
        )
        first_rollout = self.store.record_rollout_action(
            first.proposal_id,
            action="rollout",
            action_id="rollout-first-name",
            actor=HumanActor("maintainer-1", "maintainer"),
            performed_at=NOW + timedelta(days=1),
        )
        self.store.record_review(
            second.proposal_id,
            review_id="review-second-name",
            outcome="approved",
            actor=HumanActor("owner-1", "owner"),
            rationale="The owner approves the second reversible identity rollout.",
            decided_at=NOW + timedelta(days=1, hours=1),
        )
        second_rollout = self.store.record_rollout_action(
            second.proposal_id,
            action="rollout",
            action_id="rollout-second-name",
            actor=HumanActor("maintainer-1", "maintainer"),
            performed_at=NOW + timedelta(days=2),
        )
        latest = self.store.active_identity("Agentic Toolkit", "0.8.0")
        self.assertEqual(latest["display_name"], "Reflexive Fieldwork")
        self.assertEqual(latest["semantic_version"], "1.0.0")

        second_rollback = self.store.record_rollout_action(
            second.proposal_id,
            action="rollback",
            action_id="rollback-second-name",
            actor=HumanActor("maintainer-1", "maintainer"),
            performed_at=NOW + timedelta(days=3),
        )
        restored = self.store.active_identity("Agentic Toolkit", "0.8.0")
        self.assertEqual(restored["display_name"], "Fieldwork Loop")
        self.assertEqual(restored["proposal_checksum"], first.proposal_checksum)
        self.assertEqual(
            restored["action_checksums"],
            [
                first_rollout.action_checksum,
                second_rollout.action_checksum,
                second_rollback.action_checksum,
            ],
        )

        first_rollback = self.store.record_rollout_action(
            first.proposal_id,
            action="rollback",
            action_id="rollback-first-name",
            actor=HumanActor("maintainer-1", "maintainer"),
            performed_at=NOW + timedelta(days=4),
        )
        final = self.store.active_identity("Agentic Toolkit", "0.8.0")
        self.assertEqual(final["display_name"], "Agentic Toolkit")
        self.assertEqual(final["semantic_version"], "0.8.0")
        self.assertIsNone(final["proposal_checksum"])
        self.assertEqual(final["source"], "default")
        self.assertEqual(final["action_checksums"][-1], first_rollback.action_checksum)

    def test_active_identity_fails_closed_on_corrupt_proposal_checksum(self):
        proposal, _review, _action = self.approved_name_rollout()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE evolution_proposals SET proposal_checksum = :checksum "
                    "WHERE proposal_id = :proposal_id"
                ),
                {"checksum": "0" * 64, "proposal_id": proposal.proposal_id},
            )
        with self.assertRaisesRegex(EvolutionError, "proposal provenance"):
            self.store.active_identity("Agentic Toolkit", "0.8.0")

    def test_active_identity_fails_closed_on_corrupt_review_checksum(self):
        _proposal, review, _action = self.approved_name_rollout()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE evolution_reviews SET review_checksum = :checksum "
                    "WHERE review_id = :review_id"
                ),
                {"checksum": "0" * 64, "review_id": review.review_id},
            )
        with self.assertRaisesRegex(EvolutionError, "review checksum"):
            self.store.active_identity("Agentic Toolkit", "0.8.0")

    def test_active_identity_fails_closed_on_corrupt_action_checksum(self):
        _proposal, _review, action = self.approved_name_rollout()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE evolution_rollout_actions SET action_checksum = :checksum "
                    "WHERE action_id = :action_id"
                ),
                {"checksum": "0" * 64, "action_id": action.action_id},
            )
        with self.assertRaisesRegex(EvolutionError, "action checksum"):
            self.store.active_identity("Agentic Toolkit", "0.8.0")

    def test_orm_rows_reject_update_and_delete(self):
        self.grant()
        self.append_signals()
        proposal = self.store.save_proposal(self.proposal())
        with self.sessions() as session:
            row = session.scalars(select(ProductTelemetryEventRow)).first()
            row.event_type = "interface.changed"
            with self.assertRaises(EvolutionError):
                session.commit()
            session.rollback()
        with self.sessions() as session:
            row = session.get(EvolutionProposalRow, proposal.proposal_id)
            session.delete(row)
            with self.assertRaises(EvolutionError):
                session.commit()

    def test_postgres_migration_covers_every_immutable_table(self):
        migration = (
            pathlib.Path(__file__).resolve().parents[1]
            / "migrations"
            / "006_governed_evolution.sql"
        ).read_text()
        self.assertIn("deidentified IS TRUE", migration)
        self.assertIn("evolution_reject_mutation", migration)
        for table in (
            "product_telemetry_events",
            "product_telemetry_consents",
            "evolution_proposals",
            "evolution_reviews",
            "evolution_rollout_actions",
            "evolution_evaluations",
        ):
            self.assertIn(f"'{table}'", migration)


if __name__ == "__main__":
    unittest.main()
