"""Focused contract tests for replayable, governed fieldwork."""

from __future__ import annotations

import pathlib
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.fieldwork import (
    AccessScale,
    ActorRef,
    AppendOnlyViolation,
    AuthorizationContext,
    AuthorizationDenied,
    BranchMode,
    Chronology,
    EpistemicLayer,
    EvidenceManifest,
    EventKind,
    FieldworkError,
    FieldworkLedger,
    ReplayMode,
    ScopeEdge,
    ScopeGraph,
    ScopeKind,
    ScopeNode,
    Sensitivity,
    SourceRef,
    VersionManifest,
    content_hash,
)
from backend.fieldwork_store import (
    FieldworkEventRow,
    FieldworkScopeVersionRow,
    FieldworkStore,
)
from backend.models import Base


START = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
RESEARCHER = ActorRef("researcher:ada", "researcher")
PARTICIPANT = ActorRef("participant:p1", "participant")


def chronology(offset: int) -> Chronology:
    observed = START + timedelta(minutes=offset)
    return Chronology(
        observed,
        observed + timedelta(seconds=10),
        observed + timedelta(seconds=20),
    )


def manifest(
    *,
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
    scales: tuple[AccessScale, ...] = (AccessScale.TEAM,),
    graph_version: int = 0,
    consent_basis: str = "not_required",
    subjects: tuple[str, ...] = (),
    scopes: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
) -> EvidenceManifest:
    return EvidenceManifest(
        sensitivity=sensitivity,
        allowed_scales=scales,
        versions=VersionManifest(
            app_version="test",
            scope_graph_version=graph_version,
        ),
        consent_basis=consent_basis,
        consent_subjects=subjects,
        scope_node_ids=scopes,
        authorization_tags=tags,
    )


def authorization(
    branch_id: str,
    *,
    cycle_id: str = "cycle:1",
    scales: frozenset[AccessScale] = frozenset({AccessScale.TEAM}),
    scopes: frozenset[str] = frozenset({"participant:p1", "site:north"}),
    tags: frozenset[str] = frozenset(),
    max_sensitivity: Sensitivity = Sensitivity.SENSITIVE,
    layers: frozenset[EpistemicLayer] = frozenset(),
) -> AuthorizationContext:
    return AuthorizationContext(
        principal_id="researcher:ada",
        project_ids=frozenset({"project:alpha"}),
        cycle_ids=frozenset({cycle_id}),
        branch_ids=frozenset({branch_id}),
        scales=scales,
        max_sensitivity=max_sensitivity,
        scope_node_ids=scopes,
        authorization_tags=tags,
        epistemic_layers=layers,
    )


def base_ledger() -> tuple[FieldworkLedger, str, str]:
    ledger = FieldworkLedger()
    ledger.create_project(
        "project:alpha",
        "Alpha fieldwork",
        chronology(0),
        manifest(),
        actor=RESEARCHER,
        event_id="event:project",
    )
    canonical = ledger.canonical_branch_id("project:alpha")
    ledger.open_cycle(
        "project:alpha",
        "cycle:1",
        "Beta cycle",
        chronology(1),
        manifest(),
        actor=RESEARCHER,
        event_id="event:cycle",
    )
    graph = ScopeGraph(
        1,
        nodes=(
            ScopeNode.build("encounter:1", ScopeKind.ENCOUNTER, "First encounter"),
            ScopeNode.build("case:p1", ScopeKind.CASE, "Case P1"),
            ScopeNode.build("participant:p1", ScopeKind.PARTICIPANT, "Participant P1"),
            ScopeNode.build("site:north", ScopeKind.SITE, "North site"),
            ScopeNode.build("program:a", ScopeKind.PROGRAM, "Program A"),
            ScopeNode.build("organization:o", ScopeKind.ORGANIZATION, "Organization O"),
            ScopeNode.build("cohort:c", ScopeKind.COHORT, "Cohort C"),
            ScopeNode.build("network:n", ScopeKind.NETWORK, "Network N"),
            ScopeNode.build("ecosystem:e", ScopeKind.ECOSYSTEM, "Ecosystem E"),
            ScopeNode.build("public:p", ScopeKind.PUBLIC, "Public"),
        ),
        edges=(ScopeEdge("encounter:1", "case:p1", "situated_in"),),
    )
    ledger.version_scope_graph(
        project_id="project:alpha",
        cycle_id="cycle:1",
        branch_id=canonical,
        graph=graph,
        chronology=chronology(2),
        manifest=manifest(graph_version=1),
        causal_event_ids=("event:cycle",),
        actor=RESEARCHER,
        event_id="event:scope",
    )
    ledger.set_consent(
        project_id="project:alpha",
        cycle_id="cycle:1",
        branch_id=canonical,
        subject_id="participant:p1",
        granted=True,
        chronology=chronology(3),
        manifest=manifest(),
        reason="recorded opt-in",
        actor=PARTICIPANT,
        event_id="event:consent",
    )
    observation = ledger.record_observation(
        project_id="project:alpha",
        cycle_id="cycle:1",
        branch_id=canonical,
        content="Participant described the intake pathway.",
        chronology=chronology(4),
        manifest=manifest(
            sensitivity=Sensitivity.RESTRICTED,
            scales=(AccessScale.TEAM, AccessScale.SITE),
            consent_basis="granted",
            subjects=("participant:p1",),
            scopes=("participant:p1",),
        ),
        causal_event_ids=("event:consent",),
        source_refs=(SourceRef("transcript:1", "v1", "00:01:12"),),
        actor=PARTICIPANT,
        event_id="event:observation",
    )
    return ledger, canonical, observation.event_id


class FieldworkReplayTests(unittest.TestCase):
    def test_multiple_cycles_replay_independently_without_collapsing_prior_state(self):
        ledger, canonical, first_observation = base_ledger()
        first_before = ledger.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            auth=authorization(canonical),
            scale=AccessScale.TEAM,
        )
        ledger.open_cycle(
            "project:alpha",
            "cycle:2",
            "Reassessment cycle",
            chronology(20),
            manifest(),
            actor=RESEARCHER,
            event_id="event:cycle:2",
        )
        ledger.record_observation(
            project_id="project:alpha",
            cycle_id="cycle:2",
            branch_id=canonical,
            content="The later reassessment recorded a changed organizational condition.",
            chronology=chronology(21),
            manifest=manifest(),
            actor=RESEARCHER,
            event_id="event:observation:2",
        )
        second = ledger.project(
            project_id="project:alpha",
            cycle_id="cycle:2",
            branch_id=canonical,
            auth=authorization(canonical, cycle_id="cycle:2"),
            scale=AccessScale.TEAM,
        )
        first_after = ledger.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            auth=authorization(canonical),
            scale=AccessScale.TEAM,
        )

        self.assertEqual(first_before.state_hash, first_after.state_hash)
        first_ids = {item["event_id"] for item in first_after.state["events"]}
        second_ids = {item["event_id"] for item in second.state["events"]}
        self.assertIn(first_observation, first_ids)
        self.assertNotIn(first_observation, second_ids)
        self.assertIn("event:observation:2", second_ids)
        self.assertNotIn("event:observation:2", first_ids)

    def test_deterministic_projection_preserves_chronology_provenance_and_scope(self):
        ledger, canonical, observation_id = base_ledger()
        auth = authorization(canonical)
        first = ledger.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            auth=auth,
            scale=AccessScale.TEAM,
        )
        second = ledger.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            auth=auth,
            scale=AccessScale.TEAM,
        )
        self.assertEqual(first.state_hash, second.state_hash)
        self.assertEqual(first.state_json, second.state_json)
        event = next(item for item in first.state["events"] if item["event_id"] == observation_id)
        self.assertEqual(event["actor"], PARTICIPANT.as_dict())
        self.assertEqual(event["causal_event_ids"], ["event:consent"])
        self.assertEqual(event["source_refs"][0]["source_id"], "transcript:1")
        self.assertLessEqual(
            event["chronology"]["observed_at"], event["chronology"]["committed_at"]
        )
        kinds = {node["kind"] for node in first.state["scope_graph"]["nodes"]}
        self.assertTrue({"case", "participant", "cohort", "network", "ecosystem"} <= kinds)

    def test_forks_are_isolated_and_current_withdrawal_redacts_history(self):
        ledger, canonical, observation_id = base_ledger()
        ledger.fork(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id="branch:history",
            parent_branch_id=canonical,
            base_event_id=observation_id,
            mode=BranchMode.HISTORICAL,
            chronology=chronology(5),
            manifest=manifest(),
            rationale="Inspect state at the first encounter",
            actor=RESEARCHER,
            event_id="event:historical-fork",
        )
        canonical_before = ledger.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            auth=authorization(canonical),
            scale=AccessScale.TEAM,
        ).state_hash
        ledger.record_observation(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id="branch:history",
            content="Reflexive note in the historical branch only.",
            chronology=chronology(6),
            manifest=manifest(),
            layer=EpistemicLayer.REFLEXIVE_MEMO,
            actor=RESEARCHER,
            event_id="event:historical-memo",
        )
        canonical_after = ledger.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            auth=authorization(canonical),
            scale=AccessScale.TEAM,
        ).state_hash
        self.assertEqual(canonical_before, canonical_after)
        with self.assertRaises(AppendOnlyViolation):
            ledger.append(
                project_id="project:alpha",
                cycle_id="cycle:1",
                branch_id="branch:history",
                kind=ledger.events[-1].kind,
                epistemic_layer=EpistemicLayer.INTERPRETATION,
                chronology=chronology(7),
                payload={"content": "illegal"},
                manifest=manifest(),
                canonical_effect=True,
            )
        ledger.set_consent(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            subject_id="participant:p1",
            granted=False,
            chronology=chronology(8),
            manifest=manifest(),
            reason="participant withdrew",
            actor=PARTICIPANT,
            event_id="event:withdrawal",
        )
        historical = ledger.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id="branch:history",
            auth=authorization("branch:history"),
            scale=AccessScale.TEAM,
            as_of_event_id="event:historical-memo",
        )
        old_observation = next(
            item for item in historical.state["events"] if item["event_id"] == observation_id
        )
        self.assertTrue(old_observation["redacted"])
        self.assertIn("consent_withdrawn", old_observation["payload"]["reasons"])

    def test_counterfactual_and_cross_scope_authorization_remain_explicit(self):
        ledger, canonical, observation_id = base_ledger()
        ledger.fork(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id="branch:what-if",
            parent_branch_id=canonical,
            base_event_id=observation_id,
            mode=BranchMode.COUNTERFACTUAL,
            chronology=chronology(5),
            manifest=manifest(),
            rationale="Explore an alternate service pathway",
            actor=RESEARCHER,
            event_id="event:counterfactual-fork",
        )
        with self.assertRaises(FieldworkError):
            ledger.record_observation(
                project_id="project:alpha",
                cycle_id="cycle:1",
                branch_id="branch:what-if",
                content="Unmarked alternate claim",
                chronology=chronology(6),
                manifest=manifest(),
            )
        ledger.record_observation(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id="branch:what-if",
            content="If intake moved to the site, this could change access.",
            chronology=chronology(7),
            manifest=manifest(),
            layer=EpistemicLayer.COUNTERFACTUAL,
            actor=RESEARCHER,
            event_id="event:what-if",
        )
        site_only = ledger.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            auth=authorization(
                canonical,
                scales=frozenset({AccessScale.SITE}),
                scopes=frozenset({"site:north"}),
            ),
            scale=AccessScale.SITE,
        )
        observation = next(
            item
            for item in site_only.state["events"]
            if item["event_id"] == observation_id
        )
        self.assertTrue(observation["redacted"])
        self.assertIn("scope_node", observation["payload"]["reasons"])

    def test_stored_replay_is_distinct_from_unpersisted_regeneration(self):
        ledger, canonical, observation_id = base_ledger()
        ledger.store_output(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            output_id="output:1",
            content="Stored synthesis",
            input_event_ids=(observation_id,),
            chronology=chronology(5),
            manifest=manifest(
                sensitivity=Sensitivity.RESTRICTED,
                consent_basis="granted",
                subjects=("participant:p1",),
                scopes=("participant:p1",),
            ),
            generator="sidecar:v1",
            auth=authorization(canonical),
            actor=ActorRef("agent:sidecar", "informational_ai_sidecar"),
            event_id="event:output",
        )
        count = len(ledger.events)
        stored = ledger.replay_output(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            output_id="output:1",
            auth=authorization(canonical),
            scale=AccessScale.TEAM,
            mode=ReplayMode.STORED,
        )
        regenerated = ledger.replay_output(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            output_id="output:1",
            auth=authorization(canonical),
            scale=AccessScale.TEAM,
            mode=ReplayMode.REGENERATE,
            regenerate=lambda context: context["stored_output"] + " regenerated",
            generator_version="sidecar:v2",
        )
        self.assertTrue(stored.persisted)
        self.assertFalse(regenerated.persisted)
        self.assertNotEqual(stored.output_hash, regenerated.output_hash)
        self.assertEqual(len(ledger.events), count)

    def test_output_manifest_cannot_downgrade_any_input_restriction(self):
        ledger, canonical, observation_id = base_ledger()
        auth = authorization(
            canonical,
            scales=frozenset({AccessScale.TEAM, AccessScale.ORGANIZATION}),
        )
        common = {
            "project_id": "project:alpha",
            "cycle_id": "cycle:1",
            "branch_id": canonical,
            "output_id": "output:weak",
            "content": "A derived synthesis",
            "input_event_ids": (observation_id,),
            "chronology": chronology(5),
            "generator": "test:v1",
            "auth": auth,
            "actor": RESEARCHER,
        }
        cases = (
            (
                "sensitivity",
                manifest(
                    consent_basis="granted",
                    subjects=("participant:p1",),
                    scopes=("participant:p1",),
                ),
                "sensitivity",
            ),
            (
                "scale",
                manifest(
                    sensitivity=Sensitivity.RESTRICTED,
                    scales=(AccessScale.ORGANIZATION,),
                    consent_basis="granted",
                    subjects=("participant:p1",),
                    scopes=("participant:p1",),
                ),
                "access scales",
            ),
            (
                "scope",
                manifest(
                    sensitivity=Sensitivity.RESTRICTED,
                    consent_basis="granted",
                    subjects=("participant:p1",),
                ),
                "scope nodes",
            ),
            (
                "subjects",
                manifest(
                    sensitivity=Sensitivity.RESTRICTED,
                    scopes=("participant:p1",),
                ),
                "consent subjects",
            ),
            (
                "basis",
                manifest(
                    sensitivity=Sensitivity.RESTRICTED,
                    consent_basis="not_required",
                    subjects=("participant:p1",),
                    scopes=("participant:p1",),
                ),
                "consent-bound",
            ),
        )
        for label, output_manifest, expected in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(FieldworkError, expected):
                    ledger.store_output(
                        **common,
                        manifest=output_manifest,
                        event_id=f"event:weak:{label}",
                    )

        tagged = ledger.record_observation(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            content="Restricted research-team context.",
            chronology=chronology(6),
            manifest=manifest(
                sensitivity=Sensitivity.RESTRICTED,
                consent_basis="granted",
                subjects=("participant:p1",),
                scopes=("participant:p1",),
                tags=("research_team",),
            ),
            actor=RESEARCHER,
            event_id="event:tagged-input",
        )
        with self.assertRaisesRegex(FieldworkError, "authorization tags"):
            ledger.store_output(
                **{
                    **common,
                    "output_id": "output:missing-tag",
                    "input_event_ids": (tagged.event_id,),
                    "chronology": chronology(7),
                    "auth": authorization(canonical, tags=frozenset({"research_team"})),
                },
                manifest=manifest(
                    sensitivity=Sensitivity.RESTRICTED,
                    consent_basis="granted",
                    subjects=("participant:p1",),
                    scopes=("participant:p1",),
                ),
                event_id="event:missing-tag",
            )

    def test_output_dependencies_remain_consent_bound_through_nested_replay(self):
        ledger, canonical, observation_id = base_ledger()
        auth = authorization(canonical)
        restricted_manifest = manifest(
            sensitivity=Sensitivity.RESTRICTED,
            consent_basis="granted",
            subjects=("participant:p1",),
            scopes=("participant:p1",),
        )

        # Reconstituted legacy data may contain a weak output manifest.  Read
        # policy must follow its stored inputs instead of trusting that manifest.
        legacy = ledger.append(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            kind=EventKind.OUTPUT_STORED,
            epistemic_layer=EpistemicLayer.SYNTHESIS,
            chronology=chronology(5),
            payload={
                "output_id": "output:legacy",
                "content": "Legacy weak synthesis",
                "stored_output_hash": content_hash("Legacy weak synthesis"),
                "input_event_ids": [observation_id],
                "generator": "legacy:v1",
            },
            causal_event_ids=(observation_id,),
            manifest=manifest(),
            actor=RESEARCHER,
            event_id="event:legacy-output",
        )
        compliant = ledger.store_output(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            output_id="output:compliant",
            content="Consent-bound synthesis",
            input_event_ids=(observation_id,),
            chronology=chronology(6),
            manifest=restricted_manifest,
            generator="model:v1",
            auth=auth,
            actor=RESEARCHER,
            event_id="event:compliant-output",
        )
        nested = ledger.store_output(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            output_id="output:nested",
            content="Nested consent-bound synthesis",
            input_event_ids=(compliant.event_id,),
            chronology=chronology(7),
            manifest=restricted_manifest,
            generator="model:v2",
            auth=auth,
            actor=RESEARCHER,
            event_id="event:nested-output",
        )
        self.assertEqual(
            ledger.replay_output(
                project_id="project:alpha",
                cycle_id="cycle:1",
                branch_id=canonical,
                output_id="output:legacy",
                auth=auth,
                scale=AccessScale.TEAM,
            ).origin_event_id,
            legacy.event_id,
        )

        ledger.set_consent(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            subject_id="participant:p1",
            granted=False,
            chronology=chronology(8),
            manifest=manifest(),
            reason="participant withdrew research consent",
            actor=PARTICIPANT,
            event_id="event:output-withdrawal",
        )
        projection = ledger.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            auth=auth,
            scale=AccessScale.TEAM,
        ).state
        output_views = {
            event["event_id"]: event
            for event in projection["events"]
            if event["kind"] == EventKind.OUTPUT_STORED.value
        }
        self.assertEqual(projection["outputs"], [])
        for event_id in (legacy.event_id, compliant.event_id, nested.event_id):
            self.assertTrue(output_views[event_id]["redacted"])
            self.assertIn(
                "derived_input_consent_withdrawn",
                output_views[event_id]["payload"]["reasons"],
            )
        for output_id in ("output:legacy", "output:compliant", "output:nested"):
            with self.assertRaises(AuthorizationDenied):
                ledger.replay_output(
                    project_id="project:alpha",
                    cycle_id="cycle:1",
                    branch_id=canonical,
                    output_id=output_id,
                    auth=auth,
                    scale=AccessScale.TEAM,
                )
        with self.assertRaises(AuthorizationDenied):
            ledger.replay_output(
                project_id="project:alpha",
                cycle_id="cycle:1",
                branch_id=canonical,
                output_id="output:nested",
                auth=auth,
                scale=AccessScale.TEAM,
                mode=ReplayMode.REGENERATE,
                regenerate=lambda _context: "must not run",
                generator_version="model:v3",
            )
        with self.assertRaises(AuthorizationDenied):
            ledger.store_output(
                project_id="project:alpha",
                cycle_id="cycle:1",
                branch_id=canonical,
                output_id="output:after-withdrawal",
                content="Must not persist",
                input_event_ids=(observation_id,),
                chronology=chronology(9),
                manifest=restricted_manifest,
                generator="model:v3",
                auth=auth,
                actor=RESEARCHER,
            )

    def test_legacy_output_read_policy_recursively_enforces_every_input_dimension(self):
        ledger, canonical, observation_id = base_ledger()

        def append_legacy(
            *,
            event_id: str,
            output_id: str,
            input_id: str,
            cycle_id: str = "cycle:1",
            offset: int,
            output_manifest: EvidenceManifest | None = None,
        ):
            content = f"Legacy output {output_id}"
            return ledger.append(
                project_id="project:alpha",
                cycle_id=cycle_id,
                branch_id=canonical,
                kind=EventKind.OUTPUT_STORED,
                epistemic_layer=EpistemicLayer.SYNTHESIS,
                chronology=chronology(offset),
                payload={
                    "output_id": output_id,
                    "content": content,
                    "stored_output_hash": content_hash(content),
                    "input_event_ids": [input_id],
                    "generator": "legacy:v1",
                },
                causal_event_ids=(input_id,),
                manifest=output_manifest or manifest(),
                actor=RESEARCHER,
                event_id=event_id,
            )

        legacy = append_legacy(
            event_id="event:legacy-wide-output",
            output_id="output:legacy-wide",
            input_id=observation_id,
            offset=5,
            output_manifest=manifest(
                scales=(AccessScale.TEAM, AccessScale.ORGANIZATION)
            ),
        )

        cases = (
            (
                "sensitivity",
                authorization(canonical, max_sensitivity=Sensitivity.INTERNAL),
                AccessScale.TEAM,
                "derived_input_sensitivity",
            ),
            (
                "scale",
                authorization(
                    canonical,
                    scales=frozenset({AccessScale.ORGANIZATION}),
                ),
                AccessScale.ORGANIZATION,
                "derived_input_access_scale",
            ),
            (
                "scope",
                authorization(canonical, scopes=frozenset()),
                AccessScale.TEAM,
                "derived_input_scope_node",
            ),
            (
                "epistemic_layer",
                authorization(
                    canonical,
                    layers=frozenset({EpistemicLayer.SYNTHESIS}),
                ),
                AccessScale.TEAM,
                "derived_input_epistemic_layer",
            ),
        )
        for label, auth, scale, expected_reason in cases:
            with self.subTest(label=label):
                projection = ledger.project(
                    project_id="project:alpha",
                    cycle_id="cycle:1",
                    branch_id=canonical,
                    auth=auth,
                    scale=scale,
                ).state
                view = next(
                    event
                    for event in projection["events"]
                    if event["event_id"] == legacy.event_id
                )
                self.assertTrue(view["redacted"])
                self.assertIn(expected_reason, view["payload"]["reasons"])
                with self.assertRaises(AuthorizationDenied):
                    ledger.replay_output(
                        project_id="project:alpha",
                        cycle_id="cycle:1",
                        branch_id=canonical,
                        output_id="output:legacy-wide",
                        auth=auth,
                        scale=scale,
                    )

        tagged = ledger.record_observation(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            content="Input requiring the research-team authorization tag.",
            chronology=chronology(6),
            manifest=manifest(tags=("research_team",)),
            actor=RESEARCHER,
            event_id="event:legacy-tagged-input",
        )
        tagged_output = append_legacy(
            event_id="event:legacy-tag-output",
            output_id="output:legacy-tag",
            input_id=tagged.event_id,
            offset=7,
        )
        tag_projection = ledger.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            auth=authorization(canonical),
            scale=AccessScale.TEAM,
        ).state
        tag_view = next(
            event
            for event in tag_projection["events"]
            if event["event_id"] == tagged_output.event_id
        )
        self.assertIn(
            "derived_input_authorization_tag", tag_view["payload"]["reasons"]
        )

        ledger.open_cycle(
            "project:alpha",
            "cycle:2",
            "Unauthorized source cycle",
            chronology(8),
            manifest(),
            actor=RESEARCHER,
            event_id="event:legacy-cycle-2",
        )
        other_cycle = ledger.record_observation(
            project_id="project:alpha",
            cycle_id="cycle:2",
            branch_id=canonical,
            content="Input from a cycle outside the replay grant.",
            chronology=chronology(9),
            manifest=manifest(),
            actor=RESEARCHER,
            event_id="event:legacy-other-cycle-input",
        )
        cross_cycle = append_legacy(
            event_id="event:legacy-cross-cycle-output",
            output_id="output:legacy-cross-cycle",
            input_id=other_cycle.event_id,
            offset=10,
        )
        cycle_projection = ledger.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            auth=authorization(canonical),
            scale=AccessScale.TEAM,
        ).state
        cycle_view = next(
            event
            for event in cycle_projection["events"]
            if event["event_id"] == cross_cycle.event_id
        )
        self.assertIn(
            "derived_input_target_authorization",
            cycle_view["payload"]["reasons"],
        )

    def test_output_storage_rejects_pending_unauthorized_cycle_and_noneffective_inputs(self):
        ledger, canonical, observation_id = base_ledger()
        restricted_manifest = manifest(
            sensitivity=Sensitivity.RESTRICTED,
            consent_basis="pending",
            subjects=("participant:p1",),
            scopes=("participant:p1",),
        )
        pending = ledger.record_observation(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            content="Account awaiting consent confirmation.",
            chronology=chronology(5),
            manifest=restricted_manifest,
            actor=RESEARCHER,
            event_id="event:pending-input",
        )
        with self.assertRaises(AuthorizationDenied):
            ledger.store_output(
                project_id="project:alpha",
                cycle_id="cycle:1",
                branch_id=canonical,
                output_id="output:pending",
                content="Must not persist while consent is pending",
                input_event_ids=(pending.event_id,),
                chronology=chronology(6),
                manifest=restricted_manifest,
                generator="model:v1",
                auth=authorization(canonical),
                actor=RESEARCHER,
            )

        ledger.open_cycle(
            "project:alpha",
            "cycle:2",
            "Second cycle",
            chronology(7),
            manifest(),
            actor=RESEARCHER,
            event_id="event:cycle:unauthorized",
        )
        other_cycle = ledger.record_observation(
            project_id="project:alpha",
            cycle_id="cycle:2",
            branch_id=canonical,
            content="Evidence from another cycle.",
            chronology=chronology(8),
            manifest=manifest(),
            actor=RESEARCHER,
            event_id="event:other-cycle-input",
        )
        with self.assertRaises(AuthorizationDenied):
            ledger.store_output(
                project_id="project:alpha",
                cycle_id="cycle:1",
                branch_id=canonical,
                output_id="output:cross-cycle",
                content="Must not cross an unauthorized cycle",
                input_event_ids=(other_cycle.event_id,),
                chronology=chronology(9),
                manifest=manifest(),
                generator="model:v1",
                auth=authorization(canonical),
                actor=RESEARCHER,
            )

        ledger.fork(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id="branch:derived",
            parent_branch_id=canonical,
            base_event_id=observation_id,
            mode=BranchMode.HISTORICAL,
            chronology=chronology(10),
            manifest=manifest(),
            rationale="Test branch-scoped derivation authority",
            actor=RESEARCHER,
            event_id="event:derived-fork",
        )
        branch_manifest = manifest(
            sensitivity=Sensitivity.RESTRICTED,
            consent_basis="granted",
            subjects=("participant:p1",),
            scopes=("participant:p1",),
        )
        inherited = ledger.store_output(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id="branch:derived",
            output_id="output:inherited-base",
            content="Authorized derivation from inherited base evidence",
            input_event_ids=(observation_id,),
            chronology=chronology(11),
            manifest=branch_manifest,
            generator="model:v1",
            auth=authorization("branch:derived"),
            actor=RESEARCHER,
            event_id="event:inherited-output",
        )
        self.assertFalse(inherited.canonical_effect)

        # This event exists in canonical history after the fork cutoff, but is
        # not part of the fork's effective history and therefore cannot be used.
        with self.assertRaises(FieldworkError):
            ledger.store_output(
                project_id="project:alpha",
                cycle_id="cycle:1",
                branch_id="branch:derived",
                output_id="output:outside-effective-history",
                content="Must not derive from evidence outside the fork",
                input_event_ids=(other_cycle.event_id,),
                chronology=chronology(12),
                manifest=manifest(),
                generator="model:v1",
                auth=authorization("branch:derived"),
                actor=RESEARCHER,
            )

        ledger.set_consent(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            subject_id="participant:p1",
            granted=False,
            chronology=chronology(13),
            manifest=manifest(),
            reason="participant withdrew after the forked derivation",
            actor=PARTICIPANT,
            event_id="event:fork-output-withdrawal",
        )
        branch_projection = ledger.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id="branch:derived",
            auth=authorization("branch:derived"),
            scale=AccessScale.TEAM,
        ).state
        inherited_view = next(
            event
            for event in branch_projection["events"]
            if event["event_id"] == inherited.event_id
        )
        self.assertTrue(inherited_view["redacted"])
        self.assertIn(
            "derived_input_consent_withdrawn",
            inherited_view["payload"]["reasons"],
        )

    def test_sqlalchemy_store_survives_reload_and_rejects_mutation(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        store = FieldworkStore(factory)
        ledger, canonical, observation_id = base_ledger()
        expected_hash = ledger.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            auth=authorization(canonical),
            scale=AccessScale.TEAM,
        ).state_hash
        store.save(ledger)
        reloaded = store.load("project:alpha")
        actual_hash = reloaded.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            auth=authorization(canonical),
            scale=AccessScale.TEAM,
        ).state_hash
        self.assertEqual(expected_hash, actual_hash)
        self.assertEqual(reloaded.events[-1].actor, PARTICIPANT)
        reloaded.set_consent(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            subject_id="participant:p1",
            granted=False,
            chronology=chronology(9),
            manifest=manifest(),
            reason="withdrawn after restart",
            actor=PARTICIPANT,
            event_id="event:persisted-withdrawal",
        )
        store.save(reloaded)
        after_restart = store.load("project:alpha")
        withdrawn = after_restart.project(
            project_id="project:alpha",
            cycle_id="cycle:1",
            branch_id=canonical,
            auth=authorization(canonical),
            scale=AccessScale.TEAM,
        )
        observation = next(
            item
            for item in withdrawn.state["events"]
            if item["event_id"] == observation_id
        )
        self.assertTrue(observation["redacted"])
        self.assertEqual(observation["actor"]["actor_id"], "redacted")
        self.assertEqual(observation["manifest"]["scope_node_ids"], [])
        with factory() as session:
            self.assertEqual(session.query(FieldworkScopeVersionRow).count(), 1)
            row = session.get(FieldworkEventRow, observation_id)
            row.actor_role = "tampered"
            with self.assertRaises(AppendOnlyViolation):
                session.commit()
            session.rollback()
        with factory() as session:
            row = session.get(FieldworkEventRow, observation_id)
            session.delete(row)
            with self.assertRaises(AppendOnlyViolation):
                session.commit()
            session.rollback()
        # A direct corruption can bypass mapper hooks in this SQLite test;
        # reconstitution must still fail the actor-inclusive content hash.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE fieldwork_events SET actor_role = 'tampered' "
                    "WHERE event_id = :event_id"
                ),
                {"event_id": observation_id},
            )
        with self.assertRaises(AppendOnlyViolation):
            store.load("project:alpha")

    def test_postgres_migration_encodes_append_only_and_provenance_contract(self):
        sql = (
            pathlib.Path(__file__).parents[1]
            / "migrations"
            / "004_fieldwork_replay.sql"
        ).read_text()
        for required in (
            "fieldwork_reject_mutation",
            "BEFORE UPDATE OR DELETE",
            "fieldwork_validate_event_branch",
            "fork events cannot write canonical effects",
            "fieldwork_current_consent",
            "actor_id",
            "actor_role",
            "reflexive_memo",
            "after_effect",
        ):
            self.assertIn(required, sql)


if __name__ == "__main__":
    unittest.main()
