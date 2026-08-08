#!/usr/bin/env python3
"""Versioning, policy, and replay checks for cross-stage pathways."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone


APP = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from backend.pathways import (  # noqa: E402
    Approval,
    ApprovalStatus,
    FactStatus,
    PathwayDefinition,
    PathwayError,
    PathwayRun,
    RecordFact,
    RouteOutcome,
    RunStatus,
    checksum,
    confirmed_fact_evidence,
    default_pathway,
    evaluate_condition,
    validate_condition,
)
from backend.database import build_database  # noqa: E402
from backend.models import AdoptionRecord, Base, Organization, User  # noqa: E402
from backend.pathway_store import PathwayStore  # noqa: E402


NOW = datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc)


def confirmed(key: str, value=True) -> RecordFact:
    return RecordFact(key, value, FactStatus.CONFIRMED, confirmed_by="owner-1")


def stage_ready_facts(
    node: str = "entry", cycle_number: int = 1, *, blocked: bool = False
) -> list[RecordFact]:
    return [
        confirmed("stage_ready", not blocked),
        confirmed("stage_ready_node", node),
        confirmed("stage_ready_cycle", cycle_number),
        confirmed("stage_blocked", blocked),
    ]


def approved(
    gate: str, facts: dict | list[RecordFact] | None = None
) -> Approval:
    subject_facts = (
        facts
        if isinstance(facts, list)
        else [confirmed(key, value) for key, value in (facts or {}).items()]
    )
    return Approval(
        gate_key=gate,
        status=ApprovalStatus.APPROVED,
        actor_id="owner-1",
        subject_checksum=checksum(confirmed_fact_evidence(subject_facts)),
        decided_at=NOW,
    )


class PathwayTests(unittest.TestCase):
    def test_postgres_migration_keeps_decisions_and_evidence_append_only(self):
        migration = (APP / "migrations" / "005_versioned_pathways.sql").read_text()
        self.assertIn("CREATE TABLE IF NOT EXISTS pathway_versions", migration)
        self.assertIn("CREATE TABLE IF NOT EXISTS pathway_transitions", migration)
        self.assertIn("idempotency_key VARCHAR(120)", migration)
        self.assertIn("rationale TEXT NOT NULL DEFAULT ''", migration)
        self.assertIn("pathway_reject_mutation", migration)
        self.assertIn("BEFORE UPDATE OR DELETE", migration)

    def test_closed_condition_language_rejects_arbitrary_code(self):
        with self.assertRaises(PathwayError):
            validate_condition({"python": "__import__('os').system('false')"})
        with self.assertRaises(PathwayError):
            validate_condition({"fact": "score", "sql": "drop table"})

        condition = {
            "all": [
                {"fact": "stage_ready", "eq": True},
                {"fact": "review_count", "gte": 2},
                {"approval": "entry_owner"},
            ]
        }
        self.assertTrue(
            evaluate_condition(
                condition,
                {"stage_ready": True, "review_count": 2},
                frozenset({"entry_owner"}),
            )
        )

    def test_model_proposals_are_inert_until_confirmed(self):
        definition = default_pathway()
        run = PathwayRun.start("record-1", definition)
        proposed = RecordFact("stage_ready", True, FactStatus.PROPOSED)

        with self.assertRaises(PathwayError):
            run.transition(
                definition,
                outcome=RouteOutcome.PROCEED,
                actor_id="owner-1",
                rationale="The model suggested readiness, but no person confirmed it.",
                facts=[proposed],
                approvals=[approved("entry_owner")],
                decided_at=NOW,
            )

    def test_proceed_requires_confirmed_fact_and_human_approval(self):
        definition = default_pathway()
        run = PathwayRun.start("record-1", definition)
        facts = stage_ready_facts()

        with self.assertRaises(PathwayError):
            run.transition(
                definition,
                outcome="proceed",
                actor_id="owner-1",
                rationale="Missing required approval.",
                facts=facts,
                approvals=[],
                decided_at=NOW,
            )

        advanced = run.transition(
            definition,
            outcome="proceed",
            actor_id="owner-1",
            rationale="The entry record is confirmed and the owner approved continuation.",
            facts=facts,
            approvals=[approved("entry_owner", facts)],
            decided_at=NOW,
        )
        self.assertEqual(advanced.current_node, "redline")
        self.assertEqual(advanced.decisions[-1].actor_id, "owner-1")

    def test_blocked_stage_cannot_proceed_even_with_ready_fact_and_approval(self):
        definition = default_pathway()
        run = PathwayRun.start("blocked-record", definition)
        facts = [
            confirmed("stage_ready", True),
            confirmed("stage_ready_node", "entry"),
            confirmed("stage_ready_cycle", 1),
            confirmed("stage_blocked", True),
            confirmed("prohibited_use", True),
        ]
        approval = approved("entry_owner", facts)

        outcomes = {
            edge.outcome.value
            for edge in run.available_edges(definition, facts, [approval])
        }
        self.assertEqual(
            outcomes,
            {"negotiate_return", "pause", "non_ai", "walk_away"},
        )
        with self.assertRaises(PathwayError):
            run.transition(
                definition,
                outcome="proceed",
                actor_id="owner-1",
                rationale="Owner approval cannot override a prohibited route.",
                facts=facts,
                approvals=[approval],
                decided_at=NOW,
            )

    def test_prior_cycle_readiness_cannot_unlock_a_fresh_pass(self):
        definition = default_pathway()
        old_facts = stage_ready_facts(cycle_number=1)
        old_approval = approved("entry_owner", old_facts)
        run = PathwayRun.start("iterative-record", definition).transition(
            definition,
            outcome="negotiate_return",
            actor_id="owner-1",
            rationale="The entry record needs another situated pass.",
            facts=old_facts,
            approvals=[old_approval],
            decided_at=NOW,
        )

        self.assertEqual(run.cycle_number, 2)
        with self.assertRaises(PathwayError):
            run.transition(
                definition,
                outcome="proceed",
                actor_id="owner-1",
                rationale="Cycle-one evidence is stale.",
                facts=old_facts,
                approvals=[old_approval],
                decided_at=NOW,
            )

    def test_approval_is_bound_to_the_confirmed_evidence_snapshot(self):
        definition = default_pathway()
        run = PathwayRun.start("record-1", definition)
        original = stage_ready_facts()
        approval = approved("entry_owner", original)

        with self.assertRaises(PathwayError):
            run.transition(
                definition,
                outcome="proceed",
                actor_id="owner-1",
                rationale="New evidence requires a new approval.",
                facts=[*original, confirmed("material_change", True)],
                approvals=[approval],
                decided_at=NOW,
            )

        first_source = [
            RecordFact(
                fact.key,
                fact.value,
                fact.status,
                source_event_ids=("completed-step-1",),
                confirmed_by=fact.confirmed_by,
            )
            for fact in stage_ready_facts()
        ]
        changed_source = [
            RecordFact(
                fact.key,
                fact.value,
                fact.status,
                source_event_ids=("completed-step-2",),
                confirmed_by=fact.confirmed_by,
            )
            for fact in stage_ready_facts()
        ]
        source_bound_approval = Approval(
            gate_key="entry_owner",
            status=ApprovalStatus.APPROVED,
            actor_id="owner-1",
            subject_checksum=checksum(confirmed_fact_evidence(first_source)),
            decided_at=NOW,
        )
        with self.assertRaises(PathwayError):
            run.transition(
                definition,
                outcome="proceed",
                actor_id="owner-1",
                rationale="The value is unchanged, but its source changed.",
                facts=changed_source,
                approvals=[source_bound_approval],
                decided_at=NOW,
            )

    def test_negotiate_pause_resume_and_non_ai_are_first_class_routes(self):
        definition = default_pathway()
        run = PathwayRun.start("record-1", definition)
        run = run.transition(
            definition,
            outcome="negotiate_return",
            actor_id="staff-1",
            rationale="A content owner must be named before the review continues.",
            facts=[],
            approvals=[],
            decided_at=NOW,
        )
        self.assertEqual(run.current_node, "entry")
        self.assertEqual(run.cycle_number, 2)

        run = run.transition(
            definition,
            outcome="pause",
            actor_id="staff-1",
            rationale="Fieldwork is paused until the partner meeting.",
            facts=[],
            approvals=[],
            decided_at=NOW,
        )
        self.assertEqual(run.status, RunStatus.PAUSED)
        with self.assertRaises(PathwayError):
            run.transition(
                definition,
                outcome="non_ai",
                actor_id="staff-1",
                rationale="Cannot change route while paused.",
                facts=[],
                approvals=[],
                decided_at=NOW,
            )

        run = run.transition(
            definition,
            outcome="resume",
            actor_id="staff-1",
            rationale="The partner meeting supplied the missing context.",
            facts=[],
            approvals=[],
            decided_at=NOW,
        )
        run = run.transition(
            definition,
            outcome="non_ai",
            actor_id="staff-1",
            rationale="A non-AI intake redesign better serves the stated need.",
            facts=[],
            approvals=[],
            decided_at=NOW,
        )
        self.assertEqual(run.current_node, "non_ai")
        self.assertEqual(run.status, RunStatus.NON_AI)

    def test_resume_requires_pause_and_preserves_the_guided_pass(self):
        definition = default_pathway()
        active = PathwayRun.start("pause-record", definition)
        with self.assertRaises(PathwayError):
            active.transition(
                definition,
                outcome="resume",
                actor_id="staff-1",
                rationale="An active run cannot manufacture a resume event.",
                facts=[],
                approvals=[],
                decided_at=NOW,
            )

        paused = active.transition(
            definition,
            outcome="pause",
            actor_id="staff-1",
            rationale="Pause this guided pass for partner input.",
            facts=[],
            approvals=[],
            decided_at=NOW,
        )
        self.assertEqual(paused.cycle_number, 1)
        self.assertEqual(
            [edge.outcome for edge in paused.available_edges(definition, [], [])],
            [RouteOutcome.RESUME],
        )
        resumed = paused.transition(
            definition,
            outcome="resume",
            actor_id="staff-1",
            rationale="Resume the same pass after partner input.",
            facts=[],
            approvals=[],
            decided_at=NOW,
        )
        self.assertEqual(resumed.cycle_number, 1)

    def test_run_is_pinned_and_transition_history_replays_exactly(self):
        first = default_pathway()
        run = PathwayRun.start("record-1", first)
        run = run.transition(
            first,
            outcome="proceed",
            actor_id="owner-1",
            rationale="Entry is ready.",
            facts=stage_ready_facts(),
            approvals=[approved("entry_owner", stage_ready_facts())],
            decided_at=NOW,
        )
        changed = PathwayDefinition.build(
            family_key=first.family_key,
            version=first.version + 1,
            entry_node=first.entry_node,
            nodes=first.nodes,
            edges=first.edges,
        )
        with self.assertRaises(PathwayError):
            run.available_edges(changed, [], [])

        replayed = run.replay(first)
        self.assertEqual(replayed.current_node, run.current_node)
        self.assertEqual(replayed.decisions[-1].decision_hash, run.decisions[-1].decision_hash)

    def test_legacy_version_one_journal_replays_but_live_readiness_fails_closed(self):
        current = default_pathway()
        legacy_edges = []
        for edge in current.as_dict()["edges"]:
            if edge["outcome"] == "proceed":
                edge = {
                    **edge,
                    "when": {
                        "all": [
                            {"fact": "stage_ready", "eq": True},
                            {"approval": f"{edge['from']}_owner"},
                        ]
                    },
                }
            legacy_edges.append(edge)
        legacy = PathwayDefinition.build(
            family_key=current.family_key,
            version=1,
            entry_node=current.entry_node,
            nodes=current.nodes,
            edges=legacy_edges,
        )
        old_facts = [confirmed("stage_ready")]
        old_approval = approved("entry_owner", old_facts)
        live = PathwayRun.start("legacy-record", legacy)
        with self.assertRaises(PathwayError):
            live.transition(
                legacy,
                outcome="proceed",
                actor_id="owner-1",
                rationale="Legacy unbound readiness is not live authorization.",
                facts=old_facts,
                approvals=[old_approval],
                decided_at=NOW,
            )

        historical = live.transition(
            legacy,
            outcome="proceed",
            actor_id="owner-1",
            rationale="This decision predates cycle-bound readiness.",
            facts=old_facts,
            approvals=[old_approval],
            decided_at=NOW,
            _allow_legacy_readiness=True,
        )
        self.assertEqual(historical.replay(legacy), historical)

    def test_reviewer_and_monitor_have_distinct_entry_points(self):
        definition = default_pathway()
        reviewer = PathwayRun.start("review-1", definition, entry_role="reviewer")
        monitor = PathwayRun.start("monitor-1", definition, entry_role="monitor")
        self.assertEqual(reviewer.current_node, "internal_external_review")
        self.assertEqual(monitor.current_node, "monitoring")

    def test_reviewer_walk_away_and_monitor_reassessment_are_replayable_routes(self):
        definition = default_pathway()
        reviewer = PathwayRun.start(
            "reviewer-route", definition, entry_role="reviewer"
        ).transition(
            definition,
            outcome="walk_away",
            actor_id="reviewer-1",
            rationale="The review found a red line that cannot be mitigated.",
            facts=[],
            approvals=[],
            decided_at=NOW,
        )
        self.assertEqual(reviewer.status, RunStatus.WALKED_AWAY)
        self.assertEqual(reviewer.replay(definition), reviewer)

        monitor = PathwayRun.start(
            "monitor-route", definition, entry_role="monitor"
        ).transition(
            definition,
            outcome="reassess",
            actor_id="monitor-1",
            rationale="Observed after-effects require another review cycle.",
            facts=[],
            approvals=[],
            decided_at=NOW,
        )
        self.assertEqual(monitor.current_node, "internal_external_review")
        self.assertEqual(monitor.cycle_number, 2)
        self.assertEqual(monitor.replay(definition), monitor)

        retired = PathwayRun.start(
            "retire-route", definition, entry_role="monitor"
        ).transition(
            definition,
            outcome="retire",
            actor_id="owner-1",
            rationale="The organization has completed the approved retirement plan.",
            facts=[],
            approvals=[approved("retirement_owner")],
            decided_at=NOW,
        )
        self.assertEqual(retired.status, RunStatus.RETIRED)


class PathwayStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine, self.sessions = build_database(
            f"sqlite:///{self.tmp.name}/pathways.db"
        )
        Base.metadata.create_all(self.engine)
        with self.sessions() as session, session.begin():
            user = User(
                id="user-1",
                email="owner@example.org",
                password_hash="unused",
                email_verified_at=NOW,
            )
            session.add(user)
            session.flush()
            organization = Organization(
                id="org-1", name="Partner organization", created_by_id=user.id
            )
            session.add(organization)
            session.flush()
            record = AdoptionRecord(
                id="record-1",
                organization_id=organization.id,
                title="Replayable review",
                created_by_id=user.id,
            )
            session.add(record)
        self.store = PathwayStore(self.sessions)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_pinned_run_persists_and_replays_after_transition(self):
        definition = default_pathway()
        self.store.save_definition(definition, actor_id="admin-1", now=NOW)
        self.store.start_run(
            "record-1", definition, entry_role="author", now=NOW
        )
        for fact in stage_ready_facts():
            self.store.append_fact(
                "record-1", fact, proposed_by="owner-1", now=NOW
            )
        self.store.append_approval(
            "record-1", approved("entry_owner", stage_ready_facts())
        )

        advanced = self.store.transition(
            "record-1",
            outcome="proceed",
            actor_id="owner-1",
            rationale="The confirmed entry record is ready for the red line test.",
            decided_at=NOW,
        )
        loaded_definition, loaded = self.store.load_run("record-1")

        self.assertEqual(advanced.current_node, "redline")
        self.assertEqual(loaded.current_node, "redline")
        self.assertEqual(loaded.replay(loaded_definition).decisions, loaded.decisions)

    def test_later_rejection_supersedes_an_earlier_approval(self):
        definition = default_pathway()
        self.store.save_definition(definition, actor_id="admin-1", now=NOW)
        self.store.start_run("record-1", definition, entry_role="author", now=NOW)
        fact_id = self.store.append_fact(
            "record-1", confirmed("stage_ready"), proposed_by="owner-1", now=NOW
        )
        first = self.store.append_approval("record-1", approved("entry_owner"))
        self.store.append_approval(
            "record-1",
            Approval(
                gate_key="entry_owner",
                status=ApprovalStatus.CHANGES_REQUESTED,
                actor_id="owner-1",
                subject_checksum=checksum({"fact_id": fact_id}),
                decided_at=NOW,
            ),
            supersedes_id=first,
        )

        with self.assertRaises(PathwayError):
            self.store.transition(
                "record-1",
                outcome="proceed",
                actor_id="owner-1",
                rationale="The later approval state requires changes.",
                decided_at=NOW,
            )

    def test_unguided_checkpoint_is_node_bound_and_idempotent(self):
        definition = PathwayDefinition.build(
            family_key="checkpoint_test",
            version=1,
            entry_node="synthesis",
            nodes={
                "synthesis": {"kind": "review", "label": "Synthesis"},
                "pilot": {"kind": "review", "label": "Pilot"},
            },
            edges=[
                {
                    "id": "synthesis_proceed",
                    "from": "synthesis",
                    "to": "pilot",
                    "outcome": "proceed",
                    "when": {
                        "all": [
                            {"fact": "stage_ready", "eq": True},
                            {"fact": "stage_ready_node", "eq": "synthesis"},
                            {"fact": "stage_blocked", "eq": False},
                            {"approval": "synthesis_owner"},
                        ]
                    },
                }
            ],
        )
        self.store.save_definition(definition, actor_id="owner-1", now=NOW)
        self.store.start_run(
            "record-1", definition, entry_role="author", now=NOW
        )

        checkpoint_id, created = self.store.confirm_unguided_checkpoint(
            "record-1",
            node="synthesis",
            cycle_number=1,
            actor_id="owner-1",
            rationale="The decision record is ready for a bounded pilot.",
            idempotency_key="synthesis-checkpoint-1",
            now=NOW,
        )
        retried_id, retried_created = self.store.confirm_unguided_checkpoint(
            "record-1",
            node="synthesis",
            cycle_number=1,
            actor_id="owner-1",
            rationale="The decision record is ready for a bounded pilot.",
            idempotency_key="synthesis-checkpoint-1",
            now=NOW,
        )
        self.assertTrue(created)
        self.assertFalse(retried_created)
        self.assertEqual(retried_id, checkpoint_id)
        state = self.store.state("record-1")
        self.assertEqual(state["confirmed_facts"]["stage_ready_node"], "synthesis")
        self.assertEqual(state["confirmed_facts"]["stage_ready_cycle"], 1)

        with self.assertRaises(PathwayError):
            self.store.confirm_unguided_checkpoint(
                "record-1",
                node="pilot",
                cycle_number=1,
                actor_id="owner-1",
                rationale="Wrong node must fail closed.",
                idempotency_key="wrong-node-checkpoint",
                now=NOW,
            )
        with self.assertRaises(PathwayError):
            self.store.confirm_unguided_checkpoint(
                "record-1",
                node="synthesis",
                cycle_number=2,
                actor_id="owner-1",
                rationale="Wrong cycle must fail closed.",
                idempotency_key="wrong-cycle-checkpoint",
                now=NOW,
            )
        with self.assertRaises(PathwayError):
            self.store.confirm_unguided_checkpoint(
                "record-1",
                node="synthesis",
                cycle_number=1,
                actor_id="owner-1",
                rationale="Changing the request under the same key must fail.",
                idempotency_key="synthesis-checkpoint-1",
                now=NOW,
            )


if __name__ == "__main__":
    unittest.main()
