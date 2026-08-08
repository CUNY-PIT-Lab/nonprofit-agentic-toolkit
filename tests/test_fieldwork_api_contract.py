#!/usr/bin/env python3
"""HTTP contract checks for governed, record-scoped fieldwork replay."""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient


APP = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from backend.database import build_database  # noqa: E402
from backend.fieldwork import (  # noqa: E402
    AccessScale,
    AuthorizationContext,
    Sensitivity,
)
from backend.fieldwork_api import (  # noqa: E402
    ENTRY_SPECS,
    ConsentAuthority,
    create_fieldwork_router,
)
from backend.fieldwork_store import FieldworkStore  # noqa: E402
from backend.models import Base  # noqa: E402


NOW = datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc)


class RequestDatabase:
    def __init__(self, audit_log: list[dict]):
        self.audit_log = audit_log
        self.commits = 0

    def commit(self):
        self.commits += 1


class FieldworkApiContractTests(unittest.TestCase):
    def setUp(self):
        engine, self.session_factory = build_database("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.audit_log: list[dict] = []
        self.record = {"id": "record-1", "title": "Situated AI review"}
        self.consent_policy = ConsentAuthority(
            principal_id="researcher-1",
            actor_role="organization_reviewer",
            can_act_for_other_subjects=True,
        )
        self.max_sensitivity = Sensitivity.SENSITIVE

        def db_dependency():
            yield RequestDatabase(self.audit_log)

        def auth_dependency():
            return (
                {"id": "researcher-1", "fieldwork_role": "researcher"},
                {"session": "test"},
            )

        def require_csrf(request: Request, _dbs):
            if request.headers.get("X-CSRF-Token") != "test-csrf":
                raise HTTPException(403, "CSRF check failed")

        def record_access(_dbs, actor_id: str, record_id: str):
            if actor_id != "researcher-1" or record_id != self.record["id"]:
                raise HTTPException(404, "Review record not found")
            return self.record

        def audit(dbs, event_type: str, **values):
            dbs.audit_log.append({"event_type": event_type, **values})

        def authorization_context(
            _dbs,
            auth_result,
            _record,
            project_id,
            cycle_id,
            branch_id,
            _ledger,
        ):
            return AuthorizationContext(
                principal_id=auth_result[0]["id"],
                project_ids=frozenset({project_id}),
                cycle_ids=frozenset({cycle_id}),
                branch_ids=frozenset({branch_id}),
                scales=frozenset({AccessScale.ENCOUNTER, AccessScale.ORGANIZATION}),
                max_sensitivity=self.max_sensitivity,
                scope_node_ids=frozenset({"encounter-1", "organization-1"}),
            )

        app = FastAPI()
        app.include_router(
            create_fieldwork_router(
                db_dependency=db_dependency,
                auth_dependency=auth_dependency,
                require_csrf=require_csrf,
                record_access=record_access,
                actor_role=lambda _dbs, auth_result, _record, _actor_id: auth_result[
                    0
                ]["fieldwork_role"],
                audit=audit,
                version_metadata=lambda _record: {
                    "app_version": "test-app",
                    "policy_version": "fieldwork-policy.test",
                    "consent_version": "consent.test",
                    "prompt_version": "prompt.test",
                    "model_version": "stored-model.test",
                },
                store_factory=lambda: FieldworkStore(self.session_factory),
                authorization_context=authorization_context,
                consent_authority=lambda *_args: self.consent_policy,
            )
        )
        self.client = TestClient(app)
        self.headers = {"X-CSRF-Token": "test-csrf"}

    @staticmethod
    def chronology(observed_at: datetime = NOW) -> dict[str, str]:
        return {
            "observed_at": observed_at.isoformat(),
            "recorded_at": (observed_at + timedelta(minutes=5)).isoformat(),
        }

    def create_cycle(self) -> dict:
        response = self.client.post(
            "/api/records/record-1/fieldwork/cycles",
            headers=self.headers,
            json={
                "cycle_id": "cycle-1",
                "label": "First encounter cycle",
                **self.chronology(),
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["cycle"]

    def append_observation(self) -> dict:
        response = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/observations",
            headers=self.headers,
            json={
                "content": "The intake team paused before selecting a route.",
                "allowed_scales": ["encounter", "organization"],
                "scope_node_ids": ["encounter-1"],
                "idempotency_key": "observation-0001",
                **self.chronology(),
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["event"]

    def test_contract_has_typed_routes_and_no_generic_event_append(self):
        paths = set(self.client.app.openapi()["paths"])
        for spec in ENTRY_SPECS:
            self.assertIn(
                f"/api/records/{{record_id}}/fieldwork/cycles/{{cycle_id}}/{spec.slug}",
                paths,
            )
        self.assertNotIn(
            "/api/records/{record_id}/fieldwork/cycles/{cycle_id}/events", paths
        )
        invalid = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/events",
            headers=self.headers,
            json={"kind": "cycle.opened"},
        )
        self.assertEqual(invalid.status_code, 404)

    def test_cycle_and_observation_are_durable_and_actor_attributed(self):
        cycle = self.create_cycle()
        event = self.append_observation()

        self.assertEqual(cycle["opened_by"], "researcher-1")
        self.assertEqual(
            event["actor"], {"actor_id": "researcher-1", "actor_role": "researcher"}
        )
        self.assertEqual(event["kind"], "observation.recorded")
        self.assertTrue(event["canonical_effect"])

        listed = self.client.get("/api/records/record-1/fieldwork/cycles").json()[
            "cycles"
        ]
        self.assertEqual(
            [(item["cycle_id"], item["label"]) for item in listed],
            [("cycle-1", "First encounter cycle")],
        )
        persisted = FieldworkStore(self.session_factory).load("record-1")
        self.assertEqual(persisted.events[-1].event_hash, event["event_hash"])
        self.assertIn(
            "fieldwork.observation_appended",
            {item["event_type"] for item in self.audit_log},
        )

        repeated = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/observations",
            headers=self.headers,
            json={
                "content": "The intake team paused before selecting a route.",
                "allowed_scales": ["encounter", "organization"],
                "scope_node_ids": ["encounter-1"],
                "idempotency_key": "observation-0001",
                **self.chronology(),
            },
        )
        self.assertEqual(repeated.status_code, 201, repeated.text)
        self.assertFalse(repeated.json()["created"])
        self.assertEqual(repeated.json()["event"]["event_id"], event["event_id"])

    def test_naive_chronology_and_client_supplied_actor_are_rejected(self):
        self.create_cycle()
        naive = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/observations",
            headers=self.headers,
            json={
                "content": "A naive timestamp must not enter the ledger.",
                "idempotency_key": "observation-naive",
                "observed_at": "2026-08-08T16:00:00",
                "recorded_at": "2026-08-08T16:05:00",
            },
        )
        self.assertEqual(naive.status_code, 422)

        forged_actor = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/observations",
            headers=self.headers,
            json={
                "content": "This actor was supplied by the client.",
                "actor_id": "someone-else",
                "idempotency_key": "observation-forged-actor",
                **self.chronology(),
            },
        )
        self.assertEqual(forged_actor.status_code, 422)

    def test_write_manifest_cannot_exceed_actor_authorization(self):
        self.create_cycle()
        response = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/observations",
            headers=self.headers,
            json={
                "content": "This tagged note needs an authorization the actor lacks.",
                "authorization_tags": ["restricted-field-team"],
                "idempotency_key": "observation-unauthorized-tag",
                **self.chronology(),
            },
        )
        self.assertEqual(response.status_code, 403)
        ledger = FieldworkStore(self.session_factory).load("record-1")
        self.assertNotIn(
            "This tagged note needs an authorization the actor lacks.",
            [str(event.payload.get("content") or "") for event in ledger.events],
        )

    def test_current_withdrawal_redacts_an_older_as_of_participant_account(self):
        self.create_cycle()
        grant = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/consent/grants",
            headers=self.headers,
            json={
                "subject_id": "participant-pseudonym-1",
                "reason": "Recorded consent for this fieldwork cycle.",
                "idempotency_key": "consent-grant-0001",
                **self.chronology(),
            },
        )
        self.assertEqual(grant.status_code, 201, grant.text)
        self.assertEqual(
            grant.json()["event"]["actor"]["actor_role"],
            "organization_reviewer",
        )
        self.assertEqual(
            grant.json()["event"]["manifest"]["sensitivity"], "restricted"
        )
        account = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/participant-accounts",
            headers=self.headers,
            json={
                "content": "The participant described where the workflow failed.",
                "consent_basis": "granted",
                "consent_subjects": ["participant-pseudonym-1"],
                "allowed_scales": ["organization"],
                "idempotency_key": "participant-account-0001",
                **self.chronology(),
            },
        )
        self.assertEqual(account.status_code, 201, account.text)
        account_event = account.json()["event"]

        visible = self.client.get(
            "/api/records/record-1/fieldwork/cycles/cycle-1/replay",
            params={
                "scale": "organization",
                "as_of_event_id": account_event["event_id"],
            },
        )
        self.assertEqual(visible.status_code, 200, visible.text)
        visible_event = next(
            item
            for item in visible.json()["projection"]["events"]
            if item["event_id"] == account_event["event_id"]
        )
        self.assertFalse(visible_event["redacted"])

        withdrawal = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/consent/withdrawals",
            headers=self.headers,
            json={
                "subject_id": "participant-pseudonym-1",
                "reason": "Participant withdrew future access.",
                "idempotency_key": "consent-withdraw-0001",
                **self.chronology(NOW + timedelta(hours=1)),
            },
        )
        self.assertEqual(withdrawal.status_code, 201, withdrawal.text)
        self.assertEqual(
            withdrawal.json()["event"]["actor"]["actor_role"],
            "organization_reviewer",
        )

        redacted = self.client.get(
            "/api/records/record-1/fieldwork/cycles/cycle-1/replay",
            params={
                "scale": "organization",
                "as_of_event_id": account_event["event_id"],
            },
        )
        self.assertEqual(redacted.status_code, 200, redacted.text)
        redacted_event = next(
            item
            for item in redacted.json()["projection"]["events"]
            if item["event_id"] == account_event["event_id"]
        )
        self.assertTrue(redacted_event["redacted"])
        self.assertIn("consent_withdrawn", redacted_event["payload"]["reasons"])

    def test_unbound_member_cannot_mutate_or_spoof_participant_consent(self):
        self.create_cycle()
        self.consent_policy = ConsentAuthority(
            principal_id="researcher-1",
            actor_role="organization_member",
        )
        denied = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/consent/grants",
            headers=self.headers,
            json={
                "subject_id": "participant-pseudonym-1",
                "reason": "An ordinary member cannot assert this consent.",
                "idempotency_key": "member-consent-denied-0001",
                **self.chronology(),
            },
        )
        self.assertEqual(denied.status_code, 403, denied.text)

        client_lowered = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/consent/grants",
            headers=self.headers,
            json={
                "subject_id": "participant-pseudonym-1",
                "reason": "The client cannot lower consent-record sensitivity.",
                "sensitivity": "public",
                "idempotency_key": "member-consent-lowered-0001",
                **self.chronology(),
            },
        )
        self.assertEqual(client_lowered.status_code, 422, client_lowered.text)
        ledger = FieldworkStore(self.session_factory).load("record-1")
        self.assertFalse(
            any(
                event.kind.value.startswith("consent.")
                for event in ledger.events
            )
        )

    def test_bound_participant_can_opt_out_only_their_authenticated_subject(self):
        self.create_cycle()
        self.consent_policy = ConsentAuthority(
            principal_id="researcher-1",
            actor_role="participant",
            bound_subject_id="participant-pseudonym-1",
        )
        self.max_sensitivity = Sensitivity.INTERNAL
        spoofed = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/consent/withdrawals",
            headers=self.headers,
            json={
                "subject_id": "participant-pseudonym-2",
                "reason": "A participant cannot withdraw for another subject.",
                "idempotency_key": "participant-spoof-denied-0001",
                **self.chronology(),
            },
        )
        self.assertEqual(spoofed.status_code, 403, spoofed.text)

        opt_out = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/consent/withdrawals",
            headers=self.headers,
            json={
                "subject_id": "participant-pseudonym-1",
                "reason": "The authenticated participant opted out of research use.",
                "idempotency_key": "participant-self-opt-out-0001",
                **self.chronology(),
            },
        )
        self.assertEqual(opt_out.status_code, 201, opt_out.text)
        event = opt_out.json()["event"]
        self.assertEqual(event["actor"]["actor_role"], "participant")
        self.assertEqual(event["payload"]["subject_id"], "participant-pseudonym-1")
        self.assertEqual(event["manifest"]["sensitivity"], "restricted")

    def test_counterfactual_fork_cannot_write_to_canonical_history(self):
        cycle = self.create_cycle()
        observation = self.append_observation()
        fork = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/branches",
            headers=self.headers,
            json={
                "mode": "counterfactual",
                "parent_branch_id": cycle["branch_id"],
                "base_event_id": observation["event_id"],
                "rationale": "Compare a later pathway without rewriting the encounter.",
                "idempotency_key": "counterfactual-fork-0001",
                **self.chronology(NOW + timedelta(hours=1)),
            },
        )
        self.assertEqual(fork.status_code, 201, fork.text)
        branch = fork.json()["branch"]
        self.assertFalse(branch["canonical_writes"])

        interpretation = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/interpretations",
            headers=self.headers,
            json={
                "content": "A later pathway would ask for a member check first.",
                "branch_id": branch["branch_id"],
                "causal_event_ids": [observation["event_id"]],
                "idempotency_key": "counterfactual-interpretation-0001",
                **self.chronology(NOW + timedelta(hours=2)),
            },
        )
        self.assertEqual(interpretation.status_code, 201, interpretation.text)
        fork_event = interpretation.json()["event"]
        self.assertFalse(fork_event["canonical_effect"])
        self.assertEqual(fork_event["epistemic_layer"], "counterfactual")
        self.assertTrue(fork_event["payload"]["simulation_only"])

        canonical = self.client.get(
            "/api/records/record-1/fieldwork/cycles/cycle-1/replay",
            params={"scale": "organization"},
        ).json()["projection"]
        self.assertNotIn(
            fork_event["event_id"], {item["event_id"] for item in canonical["events"]}
        )

    def test_scope_graph_versions_are_sequential_and_replayable_at_scale(self):
        self.create_cycle()
        graph = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/scope-graphs",
            headers=self.headers,
            json={
                "version": 1,
                "nodes": [
                    {"node_id": "encounter-1", "kind": "encounter", "label": "Intake"},
                    {
                        "node_id": "organization-1",
                        "kind": "organization",
                        "label": "Partner organization",
                    },
                ],
                "edges": [
                    {
                        "source": "encounter-1",
                        "target": "organization-1",
                        "relation": "situated_within",
                    }
                ],
                "allowed_scales": ["encounter", "organization"],
                "idempotency_key": "scope-graph-0001",
                **self.chronology(),
            },
        )
        self.assertEqual(graph.status_code, 201, graph.text)
        self.assertEqual(graph.json()["graph"]["version"], 1)

        graph_repeat = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/scope-graphs",
            headers=self.headers,
            json={
                "version": 1,
                "nodes": [
                    {"node_id": "encounter-1", "kind": "encounter", "label": "Intake"},
                    {
                        "node_id": "organization-1",
                        "kind": "organization",
                        "label": "Partner organization",
                    },
                ],
                "edges": [
                    {
                        "source": "encounter-1",
                        "target": "organization-1",
                        "relation": "situated_within",
                    }
                ],
                "allowed_scales": ["encounter", "organization"],
                "idempotency_key": "scope-graph-0001",
                **self.chronology(),
            },
        )
        self.assertEqual(graph_repeat.status_code, 201, graph_repeat.text)
        self.assertFalse(graph_repeat.json()["created"])

        skipped = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/scope-graphs",
            headers=self.headers,
            json={
                "version": 3,
                "nodes": [
                    {"node_id": "encounter-1", "kind": "encounter", "label": "Intake"}
                ],
                "idempotency_key": "scope-graph-0003",
                **self.chronology(),
            },
        )
        self.assertEqual(skipped.status_code, 409)

        replay = self.client.get(
            "/api/records/record-1/fieldwork/cycles/cycle-1/replay",
            params={"scale": "encounter"},
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["projection"]["scope_graph"]["version"], 1)
        self.assertTrue(replay.json()["replay"]["stored_evidence_exact"])
        self.assertFalse(replay.json()["replay"]["model_regenerated"])

        unauthorized_scale = self.client.get(
            "/api/records/record-1/fieldwork/cycles/cycle-1/replay",
            params={"scale": "network"},
        )
        self.assertEqual(unauthorized_scale.status_code, 403)

    def test_output_endpoint_returns_only_the_stored_exact_output(self):
        self.create_cycle()
        observation = self.append_observation()
        stored = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/outputs",
            headers=self.headers,
            json={
                "output_id": "memo-1",
                "content": "Archived synthesis, exactly as originally generated.",
                "input_event_ids": [observation["event_id"]],
                "generator": "archived-model-v1",
                "scope_node_ids": ["encounter-1"],
                "idempotency_key": "stored-output-0001",
                **self.chronology(NOW + timedelta(hours=1)),
            },
        )
        self.assertEqual(stored.status_code, 201, stored.text)
        stored_event = stored.json()["event"]
        self.assertEqual(stored_event["actor"]["actor_role"], "researcher")
        self.assertTrue(stored.json()["output"]["exact_replay_available"])

        response = self.client.get(
            "/api/records/record-1/fieldwork/cycles/cycle-1/outputs/memo-1",
            params={"scale": "organization"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        output = response.json()["output"]
        self.assertEqual(
            output["content"], "Archived synthesis, exactly as originally generated."
        )
        self.assertEqual(output["origin_event_id"], stored_event["event_id"])
        self.assertEqual(output["replay_mode"], "stored")
        self.assertTrue(output["exact_replay"])
        self.assertFalse(output["regenerated"])
        self.assertTrue(output["persisted"])
        self.assertNotIn("mode", response.request.url.params)

    def test_participant_derived_output_cannot_shed_or_outlive_consent(self):
        self.create_cycle()
        grant = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/consent/grants",
            headers=self.headers,
            json={
                "subject_id": "participant-pseudonym-1",
                "reason": "Consent recorded before the participant account.",
                "idempotency_key": "output-consent-grant-0001",
                **self.chronology(),
            },
        )
        self.assertEqual(grant.status_code, 201, grant.text)
        account = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/participant-accounts",
            headers=self.headers,
            json={
                "content": "The participant described a sensitive intake pathway.",
                "consent_basis": "granted",
                "consent_subjects": ["participant-pseudonym-1"],
                "allowed_scales": ["organization"],
                "idempotency_key": "output-participant-account-0001",
                **self.chronology(),
            },
        )
        self.assertEqual(account.status_code, 201, account.text)
        account_event_id = account.json()["event"]["event_id"]

        weak = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/outputs",
            headers=self.headers,
            json={
                "output_id": "participant-summary-weak",
                "content": "This summary improperly claims consent is not required.",
                "input_event_ids": [account_event_id],
                "generator": "archived-model-v1",
                "consent_basis": "not_required",
                "idempotency_key": "weak-output-rejected-0001",
                **self.chronology(NOW + timedelta(hours=1)),
            },
        )
        self.assertEqual(weak.status_code, 422, weak.text)
        self.assertIn("consent", weak.json()["detail"].lower())

        compliant_body = {
            "output_id": "participant-summary",
            "content": "Consent-bound participant summary.",
            "input_event_ids": [account_event_id],
            "generator": "archived-model-v1",
            "consent_basis": "granted",
            "consent_subjects": ["participant-pseudonym-1"],
            "allowed_scales": ["organization"],
            "idempotency_key": "compliant-output-0001",
            **self.chronology(NOW + timedelta(hours=1)),
        }
        compliant = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/outputs",
            headers=self.headers,
            json=compliant_body,
        )
        self.assertEqual(compliant.status_code, 201, compliant.text)
        output_event_id = compliant.json()["event"]["event_id"]

        withdrawal = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/consent/withdrawals",
            headers=self.headers,
            json={
                "subject_id": "participant-pseudonym-1",
                "reason": "The participant withdrew research use.",
                "idempotency_key": "output-consent-withdraw-0001",
                **self.chronology(NOW + timedelta(hours=2)),
            },
        )
        self.assertEqual(withdrawal.status_code, 201, withdrawal.text)

        replay = self.client.get(
            "/api/records/record-1/fieldwork/cycles/cycle-1/replay",
            params={"scale": "organization", "as_of_event_id": output_event_id},
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        projected_output = next(
            event
            for event in replay.json()["projection"]["events"]
            if event["event_id"] == output_event_id
        )
        self.assertTrue(projected_output["redacted"])
        self.assertIn(
            "derived_input_consent_withdrawn",
            projected_output["payload"]["reasons"],
        )
        self.assertEqual(replay.json()["projection"]["outputs"], [])

        exact = self.client.get(
            "/api/records/record-1/fieldwork/cycles/cycle-1/outputs/participant-summary",
            params={"scale": "organization"},
        )
        self.assertEqual(exact.status_code, 403, exact.text)
        retried_store = self.client.post(
            "/api/records/record-1/fieldwork/cycles/cycle-1/outputs",
            headers=self.headers,
            json=compliant_body,
        )
        self.assertEqual(retried_store.status_code, 403, retried_store.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
