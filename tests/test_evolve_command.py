#!/usr/bin/env python3
"""Contract tests for the inert product-evolution maintenance command."""

from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import func, select

from backend.config import Settings
from backend.database import build_database, run_safe_migrations
from backend.evolution import (
    ConsentBasis,
    ConsentStatus,
    HumanActor,
    ProposalType,
    ProjectionAuthorization,
    TelemetryManifest,
    TelemetrySensitivity,
)
from backend.evolution_store import (
    EvolutionEvaluationRow,
    EvolutionProposalRow,
    EvolutionReviewRow,
    EvolutionRolloutActionRow,
    EvolutionStore,
)
from backend.evolve import execute, rule_registry
from backend.pathways import default_pathway


ROOT = pathlib.Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def settings_for(database_url: str, *, enabled: bool = True) -> Settings:
    environment = {
        "APP_ENV": "development",
        "TOOLKIT_SQLITE_URL": database_url,
        "PUBLIC_APP_URL": "http://127.0.0.1:8765",
        "MODEL_BACKEND": "stub",
        "PRODUCT_TELEMETRY_ENABLED": "true" if enabled else "false",
        "TELEMETRY_COHORT": "beta",
        "TELEMETRY_MIN_CELL_SIZE": "3",
    }
    with patch.dict(os.environ, environment, clear=True):
        return Settings.from_env()


class EvolveCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{self.tmp.name}/evolve.db"
        self.engine, self.sessions = build_database(self.database_url)
        run_safe_migrations(self.engine)
        self.settings = settings_for(self.database_url)
        self.store = EvolutionStore(
            self.sessions,
            component_baselines={
                "interface.routing": "0.8.0",
                "product.identity": "0.8.0",
                "pathway.default": "1.0.0",
            },
        )

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def seed_distinct_consented_signals(self) -> None:
        signals = (
            ("name.preference.fieldwork_loop", ProposalType.NAME),
            ("interface.route_confusing", ProposalType.INTERFACE),
            ("pathway.negotiate_selected", ProposalType.PATHWAY),
        )
        for participant in range(3):
            scope = f"beta-scope-{participant}"
            self.store.append_consent_decision(
                decision_id=f"consent-{participant}",
                consent_scope_id=scope,
                status=ConsentStatus.GRANTED,
                actor_id=f"participant-{participant}",
                actor_role="participant",
                reason_code="beta-opt-in",
                decided_at=NOW + timedelta(seconds=participant),
            )
            for signal_index, (event_type, area) in enumerate(signals):
                moment = NOW + timedelta(minutes=participant * 3 + signal_index + 1)
                self.store.append_product_event(
                    event_id=f"signal-{participant}-{signal_index}",
                    event_type=event_type,
                    product_area=area,
                    cohort_key="beta",
                    metrics={"helpful": True, "elapsed_ms": 100 + participant},
                    dimensions={"pathway_stage": "redline", "client_kind": "web"},
                    manifest=TelemetryManifest(
                        consent_basis=ConsentBasis.GRANTED,
                        consent_scope_id=scope,
                        sensitivity=TelemetrySensitivity.INTERNAL,
                        allowed_purposes=("analytics", "evolution"),
                        app_version="0.8.0",
                    ),
                    occurred_at=moment,
                    committed_at=moment + timedelta(seconds=1),
                )

    def row_count(self, model) -> int:
        with self.sessions() as session:
            return int(session.scalar(select(func.count()).select_from(model)) or 0)

    def append_extra_signal(
        self,
        *,
        event_id: str,
        event_type: str,
        product_area: ProposalType,
        occurred_at: datetime,
        consent_scope_id: str = "beta-scope-0",
    ) -> None:
        self.store.append_product_event(
            event_id=event_id,
            event_type=event_type,
            product_area=product_area,
            cohort_key="beta",
            metrics={"helpful": True},
            dimensions={"client_kind": "web"},
            manifest=TelemetryManifest(
                consent_basis=ConsentBasis.GRANTED,
                consent_scope_id=consent_scope_id,
                sensitivity=TelemetrySensitivity.INTERNAL,
                allowed_purposes=("analytics", "evolution"),
                app_version="0.8.0",
            ),
            occurred_at=occurred_at,
            committed_at=occurred_at + timedelta(seconds=1),
        )

    def test_registry_counts_distinct_consent_scopes_for_each_signal(self):
        rules = rule_registry("beta")
        self.assertEqual(
            {rule.candidate.proposal_type for rule in rules},
            {ProposalType.NAME, ProposalType.INTERFACE, ProposalType.PATHWAY},
        )
        self.assertTrue(all(rule.count_unit == "consent_scopes" for rule in rules))
        self.assertTrue(all(rule.minimum_count >= 3 for rule in rules))
        pathway_rule = next(
            rule
            for rule in rules
            if rule.candidate.proposal_type is ProposalType.PATHWAY
        )
        self.assertEqual(
            pathway_rule.versions.current_version,
            f"{default_pathway().version}.0.0",
        )

    def test_command_has_no_review_application_or_deployment_actions(self):
        source = (ROOT / "backend" / "evolve.py").read_text()
        tree = ast.parse(source)
        called_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertTrue(
            called_attributes.isdisjoint(
                {"record_review", "record_rollout_action", "record_evaluation"}
            )
        )
        self.assertNotIn("railway", source.lower())

    def test_repeat_clicks_from_one_scope_do_not_trigger_a_name_proposal(self):
        scope = "one-beta-scope"
        self.store.append_consent_decision(
            decision_id="one-consent",
            consent_scope_id=scope,
            status="granted",
            actor_id="participant-one",
            actor_role="participant",
            reason_code="beta-opt-in",
            decided_at=NOW,
        )
        for index in range(6):
            moment = NOW + timedelta(minutes=index + 1)
            self.store.append_product_event(
                event_id=f"repeat-{index}",
                event_type="name.preference.fieldwork_loop",
                product_area="name",
                cohort_key="beta",
                metrics={"helpful": True},
                dimensions={"client_kind": "web"},
                manifest=TelemetryManifest(
                    consent_basis=ConsentBasis.GRANTED,
                    consent_scope_id=scope,
                    sensitivity=TelemetrySensitivity.INTERNAL,
                    allowed_purposes=("evolution",),
                ),
                occurred_at=moment,
                committed_at=moment + timedelta(seconds=1),
            )
        result = execute(self.settings, self.sessions)
        self.assertEqual(result["aggregate"]["event_count"], 6)
        self.assertEqual(result["aggregate"]["proposal_count"], 0)
        self.assertEqual(self.row_count(EvolutionProposalRow), 0)
        projection = self.store.authorized_projection(
            ProjectionAuthorization(
                principal_id="worker:test",
                purpose="evolution",
                max_sensitivity=TelemetrySensitivity.RESTRICTED,
                allowed_cohorts=frozenset({"beta"}),
                policy_version="telemetry-projection.v1",
            ),
            minimum_cell_size=3,
        )
        self.assertNotIn(
            "name.preference.fieldwork_loop",
            projection.summary["signal_evidence"],
        )
        scoped = json.dumps(projection.summary["signal_evidence"], sort_keys=True)
        for forbidden in (
            "event_hash_root",
            "window_start",
            "window_end",
            "consent_scope_count",
        ):
            self.assertNotIn(forbidden, scoped)

    def test_creation_is_deterministic_idempotent_inert_and_privacy_safe(self):
        self.seed_distinct_consented_signals()
        first = execute(self.settings, self.sessions)
        second = execute(self.settings, self.sessions)
        self.assertEqual(first, second)
        self.assertEqual(first["aggregate"]["event_count"], 9)
        self.assertEqual(first["aggregate"]["proposal_count"], 3)
        self.assertEqual(self.row_count(EvolutionProposalRow), 3)
        self.assertEqual(self.row_count(EvolutionReviewRow), 0)
        self.assertEqual(self.row_count(EvolutionRolloutActionRow), 0)
        self.assertEqual(self.row_count(EvolutionEvaluationRow), 0)

        self.assertEqual(set(first), {"aggregate", "projection", "proposals"})
        self.assertEqual(
            set(first["aggregate"]),
            {"checksum", "event_count", "minimum_cell_size", "proposal_count"},
        )
        self.assertEqual(set(first["projection"]), {"checksum", "event_count"})
        self.assertEqual(
            {item["type"] for item in first["proposals"]},
            {"name", "interface", "pathway"},
        )
        self.assertTrue(
            all(set(item) == {"id", "type", "checksum"} for item in first["proposals"])
        )
        rendered = json.dumps(first, sort_keys=True)
        for forbidden in (
            "beta-scope",
            "participant-",
            "consent-",
            "metrics",
            "dimensions",
            "content",
            "transcript",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_unrelated_signal_changes_projection_but_not_proposals(self):
        self.seed_distinct_consented_signals()
        first = execute(self.settings, self.sessions)
        first_ids = {item["id"] for item in first["proposals"]}
        self.assertEqual(len(first_ids), 3)
        self.assertEqual(self.row_count(EvolutionProposalRow), 3)

        self.append_extra_signal(
            event_id="unrelated-replay",
            event_type="fieldwork.replay_used",
            product_area=ProposalType.INTERFACE,
            occurred_at=NOW + timedelta(hours=1),
        )
        second = execute(self.settings, self.sessions)
        self.assertNotEqual(
            first["projection"]["checksum"], second["projection"]["checksum"]
        )
        self.assertEqual(
            {item["id"] for item in second["proposals"]},
            first_ids,
        )
        self.assertEqual(self.row_count(EvolutionProposalRow), 3)

    def test_rejected_target_is_reconsidered_only_after_relevant_evidence_changes(self):
        self.seed_distinct_consented_signals()
        first = execute(self.settings, self.sessions)
        old_item = next(item for item in first["proposals"] if item["type"] == "interface")
        old = self.store.load_proposal(old_item["id"])
        self.store.record_review(
            old.proposal_id,
            review_id="reject-interface-v1",
            outcome="rejected",
            actor=HumanActor("owner-1", "owner"),
            rationale="The first interface revision does not yet address the observed issue.",
            decided_at=NOW + timedelta(hours=1),
        )

        unchanged = execute(self.settings, self.sessions)
        self.assertEqual(
            next(item for item in unchanged["proposals"] if item["type"] == "interface")[
                "id"
            ],
            old.proposal_id,
        )
        self.assertEqual(self.row_count(EvolutionProposalRow), 3)

        self.append_extra_signal(
            event_id="relevant-route-confusion",
            event_type="interface.route_confusing",
            product_area=ProposalType.INTERFACE,
            occurred_at=NOW + timedelta(hours=2),
        )
        changed = execute(self.settings, self.sessions)
        new_item = next(
            item for item in changed["proposals"] if item["type"] == "interface"
        )
        new = self.store.load_proposal(new_item["id"])
        self.assertNotEqual(new.proposal_id, old.proposal_id)
        self.assertNotEqual(
            new.evidence.projection_checksum,
            old.evidence.projection_checksum,
        )
        self.assertEqual(new.rollout_plan.rollout_target, old.rollout_plan.rollout_target)
        self.assertEqual(self.row_count(EvolutionProposalRow), 4)

        repeated = execute(self.settings, self.sessions)
        self.assertEqual(
            next(item for item in repeated["proposals"] if item["type"] == "interface")[
                "id"
            ],
            new.proposal_id,
        )
        self.assertEqual(self.row_count(EvolutionProposalRow), 4)

    def test_unreviewed_and_approved_same_target_suppress_new_relevant_evidence(self):
        self.seed_distinct_consented_signals()
        first = execute(self.settings, self.sessions)
        original_item = next(
            item for item in first["proposals"] if item["type"] == "interface"
        )
        original = self.store.load_proposal(original_item["id"])

        self.append_extra_signal(
            event_id="unreviewed-relevant-signal",
            event_type="interface.route_confusing",
            product_area=ProposalType.INTERFACE,
            occurred_at=NOW + timedelta(hours=1),
        )
        while_unreviewed = execute(self.settings, self.sessions)
        self.assertEqual(
            next(
                item
                for item in while_unreviewed["proposals"]
                if item["type"] == "interface"
            )["id"],
            original.proposal_id,
        )
        self.assertEqual(self.row_count(EvolutionProposalRow), 3)

        self.store.record_review(
            original.proposal_id,
            review_id="approve-without-rollout",
            outcome="approved",
            actor=HumanActor("owner-1", "owner"),
            rationale="The owner approves this target but has not authorized its rollout.",
            decided_at=NOW + timedelta(hours=2),
        )
        self.append_extra_signal(
            event_id="approved-relevant-signal",
            event_type="interface.route_confusing",
            product_area=ProposalType.INTERFACE,
            occurred_at=NOW + timedelta(hours=3),
        )
        while_approved = execute(self.settings, self.sessions)
        self.assertEqual(
            next(
                item
                for item in while_approved["proposals"]
                if item["type"] == "interface"
            )["id"],
            original.proposal_id,
        )
        self.assertEqual(self.row_count(EvolutionProposalRow), 3)

    def test_active_version_progresses_only_after_new_relevant_evidence(self):
        self.seed_distinct_consented_signals()
        first = execute(self.settings, self.sessions)
        first_item = next(
            item for item in first["proposals"] if item["type"] == "interface"
        )
        first_proposal = self.store.load_proposal(first_item["id"])
        self.assertEqual(first_proposal.versions.current_version, "0.8.0")
        self.assertEqual(first_proposal.versions.proposed_version, "0.8.1")
        self.store.record_review(
            first_proposal.proposal_id,
            review_id="approve-interface-v1",
            outcome="approved",
            actor=HumanActor("owner-1", "owner"),
            rationale="The owner approves the bounded interface rollout and rollback target.",
            decided_at=NOW + timedelta(hours=1),
        )
        self.store.record_rollout_action(
            first_proposal.proposal_id,
            action="rollout",
            action_id="rollout-interface-v1",
            actor=HumanActor("maintainer-1", "maintainer"),
            performed_at=NOW + timedelta(hours=2),
        )
        self.assertEqual(
            self.store.active_component_version("interface.routing", "0.8.0"),
            "0.8.1",
        )

        unchanged = execute(self.settings, self.sessions)
        self.assertEqual(
            next(item for item in unchanged["proposals"] if item["type"] == "interface")[
                "id"
            ],
            first_proposal.proposal_id,
        )
        self.assertEqual(self.row_count(EvolutionProposalRow), 3)

        self.append_extra_signal(
            event_id="post-rollout-route-confusion",
            event_type="interface.route_confusing",
            product_area=ProposalType.INTERFACE,
            occurred_at=NOW + timedelta(hours=3),
        )
        progressed = execute(self.settings, self.sessions)
        second_item = next(
            item for item in progressed["proposals"] if item["type"] == "interface"
        )
        second = self.store.load_proposal(second_item["id"])
        self.assertNotEqual(second.proposal_id, first_proposal.proposal_id)
        self.assertEqual(second.versions.current_version, "0.8.1")
        self.assertEqual(second.versions.proposed_version, "0.8.2")
        self.assertEqual(second.rollout_plan.rollback_target, "interface.routing@0.8.1")
        self.assertEqual(second.rollout_plan.rollout_target, "interface.routing@0.8.2")
        self.assertEqual(self.row_count(EvolutionProposalRow), 4)

    def test_same_event_id_is_retry_safe_without_reusing_request_timestamps(self):
        scope = "retry-scope"
        self.store.append_consent_decision(
            decision_id="retry-consent",
            consent_scope_id=scope,
            status="granted",
            actor_id="participant-retry",
            actor_role="participant",
            reason_code="beta-opt-in",
            decided_at=NOW,
        )
        arguments = {
            "event_id": "retry-event",
            "event_type": "interface.route_confusing",
            "product_area": "interface",
            "cohort_key": "beta",
            "metrics": {"helpful": False},
            "dimensions": {"client_kind": "web"},
            "manifest": TelemetryManifest(
                consent_basis=ConsentBasis.GRANTED,
                consent_scope_id=scope,
                sensitivity=TelemetrySensitivity.INTERNAL,
                allowed_purposes=("evolution",),
            ),
        }
        first = self.store.append_product_event(
            **arguments,
            occurred_at=NOW + timedelta(minutes=1),
            committed_at=NOW + timedelta(minutes=1, seconds=1),
        )
        retry = self.store.append_product_event(
            **arguments,
            occurred_at=NOW + timedelta(minutes=2),
            committed_at=NOW + timedelta(minutes=2, seconds=1),
        )
        self.assertEqual(first, retry)
        self.assertEqual(self.row_count(EvolutionProposalRow), 0)

    def test_disabled_setting_stops_before_reading_or_writing(self):
        with self.assertRaisesRegex(RuntimeError, "PRODUCT_TELEMETRY_ENABLED"):
            execute(settings_for(self.database_url, enabled=False), self.sessions)
        self.assertEqual(self.row_count(EvolutionProposalRow), 0)

    def test_module_entrypoint_prints_the_same_restricted_json(self):
        self.seed_distinct_consented_signals()
        expected = execute(self.settings, self.sessions)
        environment = os.environ.copy()
        for key in ("DATABASE_URL", "RAILWAY_ENVIRONMENT_NAME", "ENVIRONMENT"):
            environment.pop(key, None)
        environment.update(
            {
                "APP_ENV": "development",
                "TOOLKIT_SQLITE_URL": self.database_url,
                "PUBLIC_APP_URL": "http://127.0.0.1:8765",
                "MODEL_BACKEND": "stub",
                "PRODUCT_TELEMETRY_ENABLED": "true",
                "TELEMETRY_COHORT": "beta",
                "TELEMETRY_MIN_CELL_SIZE": "3",
            }
        )
        completed = subprocess.run(
            [sys.executable, "-m", "backend.evolve"],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), expected)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(self.row_count(EvolutionProposalRow), 3)


if __name__ == "__main__":
    unittest.main()
