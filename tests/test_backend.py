#!/usr/bin/env python3
"""Key-free account, ownership, persistence, and synthesis checks."""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select

APP = pathlib.Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(APP))

from backend.app import create_app  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.evolution import (  # noqa: E402
    ProjectionAuthorization,
    TelemetrySensitivity,
)
from backend.evolution_store import (  # noqa: E402
    EvolutionStore,
    ProductTelemetryEventRow,
    TelemetryConsentDecisionRow,
)
from backend.fieldwork_store import FieldworkStore  # noqa: E402
from backend.mailer import MemoryEmailBackend, ResendEmailBackend  # noqa: E402
from backend.model_client import StubModelClient  # noqa: E402
from backend.models import (  # noqa: E402
    AuditEvent,
    CompletedStep,
    ConversationTurn,
    OrganizationMembership,
    StageState,
    User,
)
from backend.pathway_store import PathwayStore  # noqa: E402
from backend.prompts import STAGE_ORDER, STAGE_SPECS  # noqa: E402


class CapturingStubModelClient(StubModelClient):
    def __init__(self):
        self.calls: list[dict] = []

    def complete(self, system: str, messages: list[dict], *, json_mode: bool = False) -> str:
        self.calls.append(
            {"system": system, "messages": messages, "json_mode": json_mode}
        )
        return super().complete(system, messages, json_mode=json_mode)


class BackendFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(
            {
                "APP_ENV": "test",
                "TOOLKIT_SQLITE_URL": f"sqlite:///{self.tmp.name}/toolkit.db",
                "PUBLIC_APP_URL": "http://testserver",
                "EMAIL_BACKEND": "memory",
                "MODEL_BACKEND": "stub",
                "AUTH_PEPPER": "test-pepper-with-more-than-thirty-two-characters",
                "PRODUCT_TELEMETRY_ENABLED": "true",
                "TELEMETRY_COHORT": "beta",
            }
        )
        self.mailer = MemoryEmailBackend()
        self.model_client = CapturingStubModelClient()
        self.app = create_app(
            Settings.from_env(),
            email_backend=self.mailer,
            model_client=self.model_client,
        )
        self.client = TestClient(self.app, base_url="http://testserver")
        session = self.client.get("/api/auth/session")
        self.csrf = session.json()["csrf_token"]

    def tearDown(self):
        self.client.close()
        self.app.state.db_engine.dispose()
        self.tmp.cleanup()

    def headers(self):
        return {"Origin": "http://testserver", "X-CSRF-Token": self.csrf}

    @staticmethod
    def token_from_link(link: str) -> str:
        fragment = urlsplit(link).fragment
        return parse_qs(fragment.split("?", 1)[1])["token"][0]

    def register_verify_login(self, email="person@example.org"):
        response = self.client.post(
            "/api/auth/register",
            json={"email": email, "password": "long-enough-password"},
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 202)
        token = self.token_from_link(self.mailer.messages[-1].link)
        response = self.client.post(
            "/api/auth/verify", json={"token": token}, headers=self.headers()
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/api/auth/login",
            json={"email": email, "password": "long-enough-password"},
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.csrf = response.json()["csrf_token"]
        return response.json()["user"]

    def test_verified_account_required_and_csrf_enforced(self):
        session_response = self.client.get("/api/auth/session")
        csp = session_response.headers["content-security-policy"]
        directives = {
            parts[0]: parts[1:]
            for directive in csp.split(";")
            if (parts := directive.strip().split())
        }
        self.assertEqual(directives["script-src"], ["'self'"])
        self.assertEqual(directives["style-src"], ["'self'"])
        self.assertEqual(
            directives["style-src-elem"],
            [
                "'self'",
                "'sha256-pgvDUBa4IjFA2yuSJ2cqcyxmNYJMborsd0ORcRv9vw8='",
            ],
        )
        self.assertNotIn("style-src-attr", directives)
        unauthenticated = self.client.get("/api/records")
        self.assertEqual(unauthenticated.status_code, 401)
        missing_csrf = self.client.post(
            "/api/auth/register",
            json={"email": "person@example.org", "password": "long-enough-password"},
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(missing_csrf.status_code, 403)
        self.register_verify_login()
        session = self.client.get("/api/auth/session").json()
        self.assertTrue(session["authenticated"])
        self.assertTrue(session["user"]["email_verified"])

    def test_email_responses_do_not_expose_account_state(self):
        first = self.client.post(
            "/api/auth/forgot-password",
            json={"email": "missing@example.org"},
            headers=self.headers(),
        )
        second = self.client.post(
            "/api/auth/resend-verification",
            json={"email": "missing@example.org"},
            headers=self.headers(),
        )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertNotIn("token", first.text.lower())
        self.assertNotIn("token", second.text.lower())

    def test_full_review_synthesis_map_and_annotation(self):
        self.register_verify_login()
        response = self.client.post(
            "/api/records",
            json={
                "organization_name": "Community Center",
                "title": "Information guide review",
                "proposed_use": "A public website information guide",
            },
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 201, response.text)
        record_id = response.json()["record"]["id"]

        for stage in STAGE_ORDER:
            if stage == "entry":
                out_of_order = self.client.post(
                    f"/api/records/{record_id}/stages/stress/start",
                    headers=self.headers(),
                )
                self.assertEqual(out_of_order.status_code, 409)
            response = self.client.post(
                f"/api/records/{record_id}/stages/{stage}/start",
                headers=self.headers(),
            )
            self.assertEqual(response.status_code, 200, response.text)
            for answer_index in range(STAGE_SPECS[stage]["answers"]):
                message = {
                    "content": (
                        f"{stage} response {answer_index + 1}: staff will verify "
                        "the public website guide and keep participant records outside it."
                    ),
                    "idempotency_key": f"{stage}-{answer_index}-request-key",
                }
                response = self.client.post(
                    f"/api/records/{record_id}/stages/{stage}/messages",
                    json=message,
                    headers=self.headers(),
                )
                self.assertEqual(response.status_code, 200, response.text)
                if stage == "entry" and answer_index == 0:
                    replay = self.client.post(
                        f"/api/records/{record_id}/stages/{stage}/messages",
                        json=message,
                        headers=self.headers(),
                    )
                    self.assertEqual(replay.status_code, 200, replay.text)
                    self.assertTrue(replay.json()["idempotent_replay"])
                    self.assertEqual(
                        replay.json()["user_message"]["id"],
                        response.json()["user_message"]["id"],
                    )
            response = self.client.post(
                f"/api/records/{record_id}/stages/{stage}/complete",
                json={},
                headers=self.headers(),
            )
            self.assertEqual(response.status_code, 200, response.text)
            if stage != STAGE_ORDER[-1]:
                approval = self.client.post(
                    f"/api/records/{record_id}/pathway/approvals",
                    json={
                        "gate_key": f"{stage}_owner",
                        "status": "approved",
                        "rationale": f"The organization confirms the {stage} pass.",
                    },
                    headers=self.headers(),
                )
                self.assertEqual(approval.status_code, 201, approval.text)
                routed = self.client.post(
                    f"/api/records/{record_id}/pathway/transitions",
                    json={
                        "outcome": "proceed",
                        "rationale": f"Proceed from {stage} with confirmed evidence.",
                        "idempotency_key": f"full-review-{stage}-proceed",
                    },
                    headers=self.headers(),
                )
                self.assertEqual(routed.status_code, 200, routed.text)

        response = self.client.post(
            f"/api/records/{record_id}/synthesis", headers=self.headers()
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["concept_map"]["graph"]["nodes"])
        map_id = payload["concept_map"]["id"]
        node_id = payload["concept_map"]["graph"]["nodes"][0]["id"]

        response = self.client.post(
            f"/api/records/{record_id}/annotations",
            json={
                "concept_map_id": map_id,
                "target_type": "node",
                "target_id": node_id,
                "body": "Confirm this point with program staff.",
            },
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 201, response.text)
        saved = self.client.get(f"/api/records/{record_id}").json()["record"]
        self.assertEqual(len(saved["completed_steps"]), 7)
        self.assertTrue(saved["synthesis"])
        self.assertEqual(len(saved["annotations"]), 1)

    def test_stage_completion_requires_an_explicit_versioned_route_decision(self):
        self.register_verify_login()
        created = self.client.post(
            "/api/records",
            json={
                "organization_name": "Mutual aid network",
                "title": "Routing review",
                "proposed_use": "A staff knowledge assistant",
            },
            headers=self.headers(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        record = created.json()["record"]
        record_id = record["id"]
        self.assertEqual(record["pathway"]["run"]["current_node"], "entry")

        started = self.client.post(
            f"/api/records/{record_id}/stages/entry/start",
            headers=self.headers(),
        )
        self.assertEqual(started.status_code, 200, started.text)
        for answer_index in range(STAGE_SPECS["entry"]["answers"]):
            answered = self.client.post(
                f"/api/records/{record_id}/stages/entry/messages",
                json={
                    "content": f"Entry evidence {answer_index}: a named staff owner will verify it.",
                    "idempotency_key": f"entry-path-{answer_index}-request",
                },
                headers=self.headers(),
            )
            self.assertEqual(answered.status_code, 200, answered.text)

        completed = self.client.post(
            f"/api/records/{record_id}/stages/entry/complete",
            json={},
            headers=self.headers(),
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertTrue(completed.json()["route_required"])
        self.assertEqual(completed.json()["next_stage"], "entry")
        self.assertNotIn(
            "proceed",
            {
                edge["outcome"]
                for edge in completed.json()["pathway"]["available_transitions"]
            },
        )

        approval = self.client.post(
            f"/api/records/{record_id}/pathway/approvals",
            json={
                "gate_key": "entry_owner",
                "status": "approved",
                "rationale": "The organization confirms the entry record.",
            },
            headers=self.headers(),
        )
        self.assertEqual(approval.status_code, 201, approval.text)
        self.assertIn(
            "proceed",
            {
                edge["outcome"]
                for edge in approval.json()["pathway"]["available_transitions"]
            },
        )

        transition_body = {
            "outcome": "proceed",
            "rationale": "Proceed to the red line test with the confirmed record.",
            "idempotency_key": "route-entry-proceed-0001",
        }
        advanced = self.client.post(
            f"/api/records/{record_id}/pathway/transitions",
            json=transition_body,
            headers=self.headers(),
        )
        self.assertEqual(advanced.status_code, 200, advanced.text)
        self.assertEqual(advanced.json()["pathway"]["run"]["current_node"], "redline")
        replay = self.client.post(
            f"/api/records/{record_id}/pathway/transitions",
            json=transition_body,
            headers=self.headers(),
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["pathway"]["run"]["transition_count"], 1)
        self.assertEqual(
            self.client.get(f"/api/records/{record_id}").json()["record"][
                "current_stage"
            ],
            "redline",
        )

    def test_record_creation_pins_explicit_entry_roles_and_reviewer_membership(self):
        self.register_verify_login("owner@example.org")
        reviewer_record = self.client.post(
            "/api/records",
            json={
                "organization_name": "Review partnership",
                "title": "External review entry",
                "entry_role": "reviewer",
            },
            headers=self.headers(),
        )
        self.assertEqual(reviewer_record.status_code, 201, reviewer_record.text)
        reviewer_payload = reviewer_record.json()["record"]
        self.assertEqual(reviewer_payload["pathway"]["run"]["entry_role"], "reviewer")
        self.assertEqual(
            reviewer_payload["pathway"]["run"]["current_node"],
            "internal_external_review",
        )

        monitor_record = self.client.post(
            "/api/records",
            json={
                "organization_id": reviewer_payload["organization_id"],
                "title": "Monitoring entry",
                "entry_role": "monitor",
            },
            headers=self.headers(),
        )
        self.assertEqual(monitor_record.status_code, 201, monitor_record.text)
        self.assertEqual(
            monitor_record.json()["record"]["pathway"]["run"]["current_node"],
            "monitoring",
        )

        colleague = TestClient(self.app, base_url="http://testserver")
        colleague_session = colleague.get("/api/auth/session").json()
        colleague_headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": colleague_session["csrf_token"],
        }
        registered = colleague.post(
            "/api/auth/register",
            json={
                "email": "reviewer@example.org",
                "password": "long-enough-password",
            },
            headers=colleague_headers,
        )
        self.assertEqual(registered.status_code, 202, registered.text)
        verified = colleague.post(
            "/api/auth/verify",
            json={"token": self.token_from_link(self.mailer.messages[-1].link)},
            headers=colleague_headers,
        )
        self.assertEqual(verified.status_code, 200, verified.text)
        signed_in = colleague.post(
            "/api/auth/login",
            json={
                "email": "reviewer@example.org",
                "password": "long-enough-password",
            },
            headers=colleague_headers,
        )
        self.assertEqual(signed_in.status_code, 200, signed_in.text)
        colleague_headers["X-CSRF-Token"] = signed_in.json()["csrf_token"]
        invited = self.client.post(
            f"/api/organizations/{reviewer_payload['organization_id']}/members",
            json={"email": "reviewer@example.org", "role": "reviewer"},
            headers=self.headers(),
        )
        self.assertEqual(invited.status_code, 200, invited.text)
        colleague_record = colleague.post(
            "/api/records",
            json={
                "organization_id": reviewer_payload["organization_id"],
                "title": "Invited reviewer entry",
                "entry_role": "reviewer",
            },
            headers=colleague_headers,
        )
        self.assertEqual(colleague_record.status_code, 201, colleague_record.text)
        self.assertEqual(
            colleague_record.json()["record"]["pathway"]["run"]["current_node"],
            "internal_external_review",
        )
        colleague.close()

    def test_guided_stage_cycles_preserve_history_and_block_prohibited_proceed(self):
        self.register_verify_login()
        created = self.client.post(
            "/api/records",
            json={
                "organization_name": "Situated research collective",
                "title": "Iterative review",
                "proposed_use": "A staff-only guide with a named human owner",
            },
            headers=self.headers(),
        )
        record_id = created.json()["record"]["id"]

        def settle_pass(stage: str, cycle_number: int) -> None:
            with self.app.state.db_session_factory() as dbs, dbs.begin():
                state = dbs.scalar(
                    select(StageState).where(
                        StageState.record_id == record_id,
                        StageState.stage == stage,
                        StageState.cycle_number == cycle_number,
                    )
                )
                self.assertIsNotNone(state)
                state.coverage = {key: "covered" for key in state.coverage}
                state.blockers = []

        first_start = self.client.post(
            f"/api/records/{record_id}/stages/entry/start",
            headers=self.headers(),
        )
        self.assertEqual(first_start.status_code, 200, first_start.text)
        self.assertEqual(first_start.json()["cycle_number"], 1)
        first_message = self.client.post(
            f"/api/records/{record_id}/stages/entry/messages",
            json={
                "content": "Cycle one records the initial organizational account.",
                "idempotency_key": "repeatable-stage-message",
            },
            headers=self.headers(),
        )
        self.assertEqual(first_message.status_code, 200, first_message.text)
        settle_pass("entry", 1)
        first_completion = self.client.post(
            f"/api/records/{record_id}/stages/entry/complete",
            json={},
            headers=self.headers(),
        )
        self.assertEqual(first_completion.status_code, 200, first_completion.text)
        self.assertEqual(first_completion.json()["cycle_number"], 1)
        first_approval = self.client.post(
            f"/api/records/{record_id}/pathway/approvals",
            json={
                "gate_key": "entry_owner",
                "status": "approved",
                "rationale": "The owner confirms the first entry pass.",
            },
            headers=self.headers(),
        )
        self.assertEqual(first_approval.status_code, 201, first_approval.text)

        negotiate_body = {
            "outcome": "negotiate_return",
            "rationale": "A second situated pass is required before proceeding.",
            "idempotency_key": "entry-negotiate-cycle-two",
        }
        negotiated = self.client.post(
            f"/api/records/{record_id}/pathway/transitions",
            json=negotiate_body,
            headers=self.headers(),
        )
        self.assertEqual(negotiated.status_code, 200, negotiated.text)
        self.assertFalse(negotiated.json()["idempotent_replay"])
        negotiated_path = negotiated.json()["pathway"]
        self.assertEqual(negotiated_path["run"]["cycle_number"], 2)
        self.assertFalse(negotiated_path["confirmed_facts"]["stage_ready"])
        self.assertNotIn(
            "proceed",
            {
                edge["outcome"]
                for edge in negotiated_path["available_transitions"]
            },
        )
        replayed = self.client.post(
            f"/api/records/{record_id}/pathway/transitions",
            json=negotiate_body,
            headers=self.headers(),
        )
        self.assertEqual(replayed.status_code, 200, replayed.text)
        self.assertTrue(replayed.json()["idempotent_replay"])
        with self.app.state.db_session_factory() as dbs:
            transition_audits = dbs.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.event_type == "pathway.transitioned",
                    AuditEvent.entity_id == record_id,
                )
            )
        self.assertEqual(transition_audits, 1)

        second_start = self.client.post(
            f"/api/records/{record_id}/stages/entry/start",
            headers=self.headers(),
        )
        self.assertEqual(second_start.status_code, 200, second_start.text)
        self.assertEqual(second_start.json()["cycle_number"], 2)
        self.assertTrue(
            all(turn["cycle_number"] == 2 for turn in second_start.json()["messages"])
        )
        second_message = self.client.post(
            f"/api/records/{record_id}/stages/entry/messages",
            json={
                "content": "Cycle two records changed conditions after negotiation.",
                "idempotency_key": "repeatable-stage-message",
            },
            headers=self.headers(),
        )
        self.assertEqual(second_message.status_code, 200, second_message.text)
        self.assertNotEqual(
            first_message.json()["user_message"]["id"],
            second_message.json()["user_message"]["id"],
        )
        settle_pass("entry", 2)
        second_completion = self.client.post(
            f"/api/records/{record_id}/stages/entry/complete",
            json={},
            headers=self.headers(),
        )
        self.assertEqual(second_completion.status_code, 200, second_completion.text)
        second_path = second_completion.json()["pathway"]
        self.assertTrue(second_path["confirmed_facts"]["stage_ready"])
        self.assertEqual(second_path["confirmed_facts"]["stage_ready_cycle"], 2)
        self.assertNotIn(
            "proceed",
            {edge["outcome"] for edge in second_path["available_transitions"]},
        )

        detail = self.client.get(f"/api/records/{record_id}").json()["record"]
        self.assertEqual(
            [
                item["cycle_number"]
                for item in detail["completed_steps"]
                if item["stage"] == "entry"
            ],
            [1, 2],
        )
        self.assertEqual(
            [
                item["cycle_number"]
                for item in detail["stage_passes"]
                if item["stage"] == "entry"
            ],
            [1, 2],
        )
        with self.app.state.db_session_factory() as dbs:
            self.assertEqual(
                len(
                    dbs.scalars(
                        select(ConversationTurn).where(
                            ConversationTurn.record_id == record_id,
                            ConversationTurn.stage == "entry",
                        )
                    ).all()
                ),
                len(
                    [
                        turn
                        for turn in detail["turns"]
                        if turn["stage"] == "entry"
                    ]
                ),
            )
            self.assertEqual(
                len(
                    dbs.scalars(
                        select(CompletedStep).where(
                            CompletedStep.record_id == record_id,
                            CompletedStep.stage == "entry",
                        )
                    ).all()
                ),
                2,
            )

        second_approval = self.client.post(
            f"/api/records/{record_id}/pathway/approvals",
            json={
                "gate_key": "entry_owner",
                "status": "approved",
                "rationale": "The owner confirms the fresh entry pass.",
            },
            headers=self.headers(),
        )
        self.assertEqual(second_approval.status_code, 201, second_approval.text)
        self.assertIn(
            "proceed",
            {
                edge["outcome"]
                for edge in second_approval.json()["pathway"]["available_transitions"]
            },
        )
        advanced = self.client.post(
            f"/api/records/{record_id}/pathway/transitions",
            json={
                "outcome": "proceed",
                "rationale": "Proceed to the red line test after the fresh pass.",
                "idempotency_key": "entry-cycle-two-proceed",
            },
            headers=self.headers(),
        )
        self.assertEqual(advanced.status_code, 200, advanced.text)

        redline_start = self.client.post(
            f"/api/records/{record_id}/stages/redline/start",
            headers=self.headers(),
        )
        self.assertEqual(redline_start.status_code, 200, redline_start.text)
        settle_pass("redline", 2)
        with self.app.state.db_session_factory() as dbs, dbs.begin():
            redline = dbs.scalar(
                select(StageState).where(
                    StageState.record_id == record_id,
                    StageState.stage == "redline",
                    StageState.cycle_number == 2,
                )
            )
            redline.blockers = [
                {
                    "id": "prohibited-use",
                    "status": "open",
                    "title": "Prohibited use",
                    "detail": "Sensitive records cannot enter the external service.",
                }
            ]
        blocked_completion = self.client.post(
            f"/api/records/{record_id}/stages/redline/complete",
            json={},
            headers=self.headers(),
        )
        self.assertEqual(blocked_completion.status_code, 409, blocked_completion.text)
        reserved_fact = self.client.post(
            f"/api/records/{record_id}/pathway/facts",
            json={"key": "stage_ready", "value": True, "status": "confirmed"},
            headers=self.headers(),
        )
        self.assertEqual(reserved_fact.status_code, 403, reserved_fact.text)
        redline_approval = self.client.post(
            f"/api/records/{record_id}/pathway/approvals",
            json={
                "gate_key": "redline_owner",
                "status": "approved",
                "rationale": "Approval cannot erase the recorded prohibition.",
            },
            headers=self.headers(),
        )
        self.assertEqual(redline_approval.status_code, 201, redline_approval.text)
        blocked_path = redline_approval.json()["pathway"]
        self.assertFalse(blocked_path["confirmed_facts"]["stage_ready"])
        self.assertTrue(blocked_path["confirmed_facts"]["stage_blocked"])
        self.assertEqual(
            {
                edge["outcome"]
                for edge in blocked_path["available_transitions"]
            },
            {"negotiate_return", "pause", "non_ai", "walk_away"},
        )

    def test_unguided_checkpoints_reach_pilot_and_monitoring_safely(self):
        user = self.register_verify_login()
        created = self.client.post(
            "/api/records",
            json={
                "organization_name": "Checkpoint cooperative",
                "title": "Unguided pathway checkpoints",
            },
            headers=self.headers(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        record = created.json()["record"]
        record_id = record["id"]
        store = PathwayStore(self.app.state.db_session_factory)

        # Seed valid guided decisions with the same server-owned readiness
        # helper used by stage completion, keeping the endpoint test focused on
        # the synthesis and pilot checkpoint contract.
        for node in STAGE_ORDER:
            _definition, run = store.load_run(record_id)
            self.assertEqual(run.current_node, node)
            store.record_stage_completion(
                record_id,
                node=node,
                cycle_number=run.cycle_number,
                completion_id=f"checkpoint-fixture:{node}",
                actor_id=user["id"],
            )
            approval = self.client.post(
                f"/api/records/{record_id}/pathway/approvals",
                json={
                    "gate_key": f"{node}_owner",
                    "status": "approved",
                    "rationale": f"Confirm the bounded {node} fixture pass.",
                },
                headers=self.headers(),
            )
            self.assertEqual(approval.status_code, 201, approval.text)
            routed = self.client.post(
                f"/api/records/{record_id}/pathway/transitions",
                json={
                    "outcome": "proceed",
                    "rationale": f"Proceed from the confirmed {node} fixture pass.",
                    "idempotency_key": f"fixture-{node}-proceed",
                },
                headers=self.headers(),
            )
            self.assertEqual(routed.status_code, 200, routed.text)
        self.assertEqual(store.load_run(record_id)[1].current_node, "synthesis")

        base_checkpoint = {
            "node": "synthesis",
            "cycle_number": 1,
            "confirmed": True,
            "rationale": "The decision record is ready for a bounded pilot.",
            "idempotency_key": "synthesis-checkpoint-api-1",
        }
        wrong_node = self.client.post(
            f"/api/records/{record_id}/pathway/checkpoints",
            json={**base_checkpoint, "node": "pilot", "idempotency_key": "wrong-node-api"},
            headers=self.headers(),
        )
        self.assertEqual(wrong_node.status_code, 409, wrong_node.text)
        wrong_cycle = self.client.post(
            f"/api/records/{record_id}/pathway/checkpoints",
            json={
                **base_checkpoint,
                "cycle_number": 2,
                "idempotency_key": "wrong-cycle-api",
            },
            headers=self.headers(),
        )
        self.assertEqual(wrong_cycle.status_code, 409, wrong_cycle.text)
        unconfirmed = self.client.post(
            f"/api/records/{record_id}/pathway/checkpoints",
            json={
                **base_checkpoint,
                "confirmed": False,
                "idempotency_key": "unconfirmed-api",
            },
            headers=self.headers(),
        )
        self.assertEqual(unconfirmed.status_code, 422, unconfirmed.text)

        with self.app.state.db_session_factory() as dbs, dbs.begin():
            membership = dbs.scalar(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id
                    == record["organization_id"],
                    OrganizationMembership.user_id == user["id"],
                )
            )
            membership.role = "member"
        member_denied = self.client.post(
            f"/api/records/{record_id}/pathway/checkpoints",
            json=base_checkpoint,
            headers=self.headers(),
        )
        self.assertEqual(member_denied.status_code, 403, member_denied.text)
        with self.app.state.db_session_factory() as dbs, dbs.begin():
            membership = dbs.scalar(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id
                    == record["organization_id"],
                    OrganizationMembership.user_id == user["id"],
                )
            )
            membership.role = "owner"

        checkpoint = self.client.post(
            f"/api/records/{record_id}/pathway/checkpoints",
            json=base_checkpoint,
            headers=self.headers(),
        )
        self.assertEqual(checkpoint.status_code, 201, checkpoint.text)
        self.assertFalse(checkpoint.json()["idempotent_replay"])
        self.assertNotIn(
            "proceed",
            {
                edge["outcome"]
                for edge in checkpoint.json()["pathway"]["available_transitions"]
            },
        )
        checkpoint_retry = self.client.post(
            f"/api/records/{record_id}/pathway/checkpoints",
            json=base_checkpoint,
            headers=self.headers(),
        )
        self.assertEqual(checkpoint_retry.status_code, 201, checkpoint_retry.text)
        self.assertTrue(checkpoint_retry.json()["idempotent_replay"])
        self.assertEqual(
            checkpoint_retry.json()["checkpoint_id"],
            checkpoint.json()["checkpoint_id"],
        )
        checkpoint_conflict = self.client.post(
            f"/api/records/{record_id}/pathway/checkpoints",
            json={
                **base_checkpoint,
                "rationale": "A changed rationale cannot reuse the checkpoint key.",
            },
            headers=self.headers(),
        )
        self.assertEqual(checkpoint_conflict.status_code, 409, checkpoint_conflict.text)
        with self.app.state.db_session_factory() as dbs:
            checkpoint_audits = dbs.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.event_type
                    == "pathway.unguided_checkpoint_confirmed",
                    AuditEvent.entity_id == record_id,
                )
            )
        self.assertEqual(checkpoint_audits, 1)

        for node, target in (("synthesis", "pilot"), ("pilot", "monitoring")):
            if node == "pilot":
                pilot_checkpoint = self.client.post(
                    f"/api/records/{record_id}/pathway/checkpoints",
                    json={
                        "node": "pilot",
                        "cycle_number": 1,
                        "confirmed": True,
                        "rationale": "The bounded pilot plan is ready for monitoring.",
                        "idempotency_key": "pilot-checkpoint-api-1",
                    },
                    headers=self.headers(),
                )
                self.assertEqual(pilot_checkpoint.status_code, 201, pilot_checkpoint.text)
            approval = self.client.post(
                f"/api/records/{record_id}/pathway/approvals",
                json={
                    "gate_key": f"{node}_owner",
                    "status": "approved",
                    "rationale": f"The organization confirms the {node} checkpoint.",
                },
                headers=self.headers(),
            )
            self.assertEqual(approval.status_code, 201, approval.text)
            self.assertIn(
                "proceed",
                {
                    edge["outcome"]
                    for edge in approval.json()["pathway"]["available_transitions"]
                },
            )
            routed = self.client.post(
                f"/api/records/{record_id}/pathway/transitions",
                json={
                    "outcome": "proceed",
                    "rationale": f"Proceed from {node} to {target}.",
                    "idempotency_key": f"{node}-checkpoint-proceed",
                },
                headers=self.headers(),
            )
            self.assertEqual(routed.status_code, 200, routed.text)
            self.assertEqual(routed.json()["pathway"]["run"]["current_node"], target)

        no_monitoring_proceed = self.client.post(
            f"/api/records/{record_id}/pathway/checkpoints",
            json={
                "node": "monitoring",
                "cycle_number": 1,
                "confirmed": True,
                "rationale": "Monitoring has no Proceed edge in this pathway version.",
                "idempotency_key": "monitoring-no-proceed-api",
            },
            headers=self.headers(),
        )
        self.assertEqual(no_monitoring_proceed.status_code, 409, no_monitoring_proceed.text)

    def test_record_membership_drives_fieldwork_write_and_replay_scope(self):
        self.register_verify_login()
        created = self.client.post(
            "/api/records",
            json={
                "organization_name": "Neighborhood archive",
                "title": "Fieldwork review",
                "proposed_use": "A community archive search guide",
            },
            headers=self.headers(),
        )
        record_id = created.json()["record"]["id"]
        now = datetime.now(timezone.utc)
        chronology = {
            "observed_at": (now - timedelta(minutes=2)).isoformat(),
            "recorded_at": (now - timedelta(minutes=1)).isoformat(),
        }
        cycle = self.client.post(
            f"/api/records/{record_id}/fieldwork/cycles",
            json={
                "cycle_id": "encounter-cycle-1",
                "label": "First situated encounter",
                **chronology,
            },
            headers=self.headers(),
        )
        self.assertEqual(cycle.status_code, 201, cycle.text)
        graph = self.client.post(
            f"/api/records/{record_id}/fieldwork/cycles/encounter-cycle-1/scope-graphs",
            json={
                "version": 1,
                "nodes": [
                    {
                        "node_id": "site-1",
                        "kind": "site",
                        "label": "Neighborhood site",
                    }
                ],
                "idempotency_key": "scope-graph-0001",
                **chronology,
            },
            headers=self.headers(),
        )
        self.assertEqual(graph.status_code, 201, graph.text)
        observation = self.client.post(
            f"/api/records/{record_id}/fieldwork/cycles/encounter-cycle-1/observations",
            json={
                "content": "Staff paused the workflow to confirm who could see the source.",
                "allowed_scales": ["organization"],
                "scope_node_ids": ["site-1"],
                "idempotency_key": "field-note-0001",
                **chronology,
            },
            headers=self.headers(),
        )
        self.assertEqual(observation.status_code, 201, observation.text)
        self.assertEqual(
            observation.json()["event"]["actor"]["actor_id"],
            self.client.get("/api/auth/session").json()["user"]["id"],
        )

        before_sidecar = len(
            FieldworkStore(self.app.state.db_session_factory).load(record_id).events
        )
        sidecar = self.client.post(
            f"/api/records/{record_id}/sidecar/chat",
            json={
                "message": "What remains uncertain in this authorized fieldwork cycle?",
                "history": [],
                "scale": "organization",
                "cycle_id": "encounter-cycle-1",
                "branch_id": cycle.json()["cycle"]["branch_id"],
            },
            headers=self.headers(),
        )
        self.assertEqual(sidecar.status_code, 200, sidecar.text)
        for boundary in (
            "canonical_effect",
            "record_write_authority",
            "persisted",
            "exact_replay",
        ):
            self.assertFalse(sidecar.json()[boundary])
        self.assertEqual(
            len(FieldworkStore(self.app.state.db_session_factory).load(record_id).events),
            before_sidecar,
        )
        sidecar_prompt = self.model_client.calls[-1]["system"]
        self.assertIn(
            "Staff paused the workflow to confirm who could see the source.",
            sidecar_prompt,
        )
        self.assertNotIn("A community archive search guide", sidecar_prompt)

        narrower_sidecar = self.client.post(
            f"/api/records/{record_id}/sidecar/chat",
            json={
                "message": "What is visible at the individual scale?",
                "history": [],
                "scale": "individual",
                "cycle_id": "encounter-cycle-1",
                "branch_id": cycle.json()["cycle"]["branch_id"],
            },
            headers=self.headers(),
        )
        self.assertEqual(narrower_sidecar.status_code, 200, narrower_sidecar.text)
        narrower_prompt = self.model_client.calls[-1]["system"]
        self.assertNotIn(
            "Staff paused the workflow to confirm who could see the source.",
            narrower_prompt,
        )
        self.assertNotIn("A community archive search guide", narrower_prompt)

        replay = self.client.get(
            f"/api/records/{record_id}/fieldwork/cycles/encounter-cycle-1/replay",
            params={"scale": "organization"},
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["state_hash"])
        denied = self.client.get(
            f"/api/records/{record_id}/fieldwork/cycles/encounter-cycle-1/replay",
            params={"scale": "network"},
        )
        self.assertEqual(denied.status_code, 403, denied.text)

    def test_scope_graph_does_not_self_grant_ordinary_member_access(self):
        self.register_verify_login("fieldwork-scope-owner@example.org")
        created = self.client.post(
            "/api/records",
            json={
                "organization_name": "Participant scope cooperative",
                "title": "Scoped participant fieldwork",
                "proposed_use": "A bounded participant-account review",
            },
            headers=self.headers(),
        ).json()["record"]
        record_id = created["id"]
        now = datetime.now(timezone.utc)
        chronology = {
            "observed_at": (now - timedelta(minutes=3)).isoformat(),
            "recorded_at": (now - timedelta(minutes=2)).isoformat(),
        }
        cycle = self.client.post(
            f"/api/records/{record_id}/fieldwork/cycles",
            json={
                "cycle_id": "participant-scope-cycle",
                "label": "Participant-secret scope",
                **chronology,
            },
            headers=self.headers(),
        )
        self.assertEqual(cycle.status_code, 201, cycle.text)
        graph = self.client.post(
            f"/api/records/{record_id}/fieldwork/cycles/participant-scope-cycle/scope-graphs",
            json={
                "version": 1,
                "nodes": [
                    {
                        "node_id": "participant-secret",
                        "kind": "participant",
                        "label": "Confidential participant scope",
                    }
                ],
                "idempotency_key": "participant-scope-graph-0001",
                **chronology,
            },
            headers=self.headers(),
        )
        self.assertEqual(graph.status_code, 201, graph.text)
        observation = self.client.post(
            f"/api/records/{record_id}/fieldwork/cycles/participant-scope-cycle/observations",
            json={
                "content": "Participant-secret internal field note.",
                "sensitivity": "internal",
                "allowed_scales": ["organization"],
                "scope_node_ids": ["participant-secret"],
                "idempotency_key": "participant-secret-note-0001",
                **chronology,
            },
            headers=self.headers(),
        )
        self.assertEqual(observation.status_code, 201, observation.text)
        observation_id = observation.json()["event"]["event_id"]
        self.assertEqual(
            observation.json()["event"]["actor"]["actor_role"],
            "organization_owner",
        )

        colleague = TestClient(self.app, base_url="http://testserver")
        try:
            colleague_session = colleague.get("/api/auth/session").json()
            colleague_headers = {
                "Origin": "http://testserver",
                "X-CSRF-Token": colleague_session["csrf_token"],
            }
            colleague.post(
                "/api/auth/register",
                json={
                    "email": "fieldwork-scope-member@example.org",
                    "password": "long-enough-password",
                },
                headers=colleague_headers,
            )
            verify_token = self.token_from_link(self.mailer.messages[-1].link)
            colleague.post(
                "/api/auth/verify",
                json={"token": verify_token},
                headers=colleague_headers,
            )
            signed_in = colleague.post(
                "/api/auth/login",
                json={
                    "email": "fieldwork-scope-member@example.org",
                    "password": "long-enough-password",
                },
                headers=colleague_headers,
            )
            self.assertEqual(signed_in.status_code, 200, signed_in.text)
            colleague_headers["X-CSRF-Token"] = signed_in.json()["csrf_token"]

            invited = self.client.post(
                f"/api/organizations/{created['organization_id']}/members",
                json={
                    "email": "fieldwork-scope-member@example.org",
                    "role": "member",
                },
                headers=self.headers(),
            )
            self.assertEqual(invited.status_code, 200, invited.text)
            replay = colleague.get(
                f"/api/records/{record_id}/fieldwork/cycles/participant-scope-cycle/replay",
                params={"scale": "organization"},
            )
            self.assertEqual(replay.status_code, 200, replay.text)
            member_view = next(
                event
                for event in replay.json()["projection"]["events"]
                if event["event_id"] == observation_id
            )
            self.assertTrue(member_view["redacted"])
            self.assertIn("scope_node", member_view["payload"]["reasons"])

            denied_write = colleague.post(
                f"/api/records/{record_id}/fieldwork/cycles/participant-scope-cycle/observations",
                json={
                    "content": "An ordinary member cannot self-grant this scope.",
                    "sensitivity": "internal",
                    "allowed_scales": ["organization"],
                    "scope_node_ids": ["participant-secret"],
                    "idempotency_key": "member-scope-write-denied-0001",
                    **chronology,
                },
                headers=colleague_headers,
            )
            self.assertEqual(denied_write.status_code, 403, denied_write.text)

            member_write = colleague.post(
                f"/api/records/{record_id}/fieldwork/cycles/participant-scope-cycle/observations",
                json={
                    "content": "An unscoped member field note.",
                    "sensitivity": "internal",
                    "allowed_scales": ["organization"],
                    "idempotency_key": "member-unscoped-note-0001",
                    **chronology,
                },
                headers=colleague_headers,
            )
            self.assertEqual(member_write.status_code, 201, member_write.text)
            member_event_id = member_write.json()["event"]["event_id"]
            self.assertEqual(
                member_write.json()["event"]["actor"]["actor_role"],
                "organization_member",
            )

            promoted = self.client.post(
                f"/api/organizations/{created['organization_id']}/members",
                json={
                    "email": "fieldwork-scope-member@example.org",
                    "role": "reviewer",
                },
                headers=self.headers(),
            )
            self.assertEqual(promoted.status_code, 200, promoted.text)
            reviewer_replay = colleague.get(
                f"/api/records/{record_id}/fieldwork/cycles/participant-scope-cycle/replay",
                params={"scale": "organization"},
            )
            self.assertEqual(reviewer_replay.status_code, 200, reviewer_replay.text)
            reviewer_view = next(
                event
                for event in reviewer_replay.json()["projection"]["events"]
                if event["event_id"] == observation_id
            )
            self.assertFalse(reviewer_view["redacted"])
            reviewer_write = colleague.post(
                f"/api/records/{record_id}/fieldwork/cycles/participant-scope-cycle/observations",
                json={
                    "content": "A reviewer-authorized scoped field note.",
                    "sensitivity": "internal",
                    "allowed_scales": ["organization"],
                    "scope_node_ids": ["participant-secret"],
                    "idempotency_key": "reviewer-scoped-note-0001",
                    **chronology,
                },
                headers=colleague_headers,
            )
            self.assertEqual(reviewer_write.status_code, 201, reviewer_write.text)
            reviewer_event_id = reviewer_write.json()["event"]["event_id"]
            self.assertEqual(
                reviewer_write.json()["event"]["actor"]["actor_role"],
                "organization_reviewer",
            )
        finally:
            colleague.close()

        persisted = FieldworkStore(self.app.state.db_session_factory).load(record_id)
        persisted_roles = {
            event.event_id: event.actor.actor_role for event in persisted.events
        }
        self.assertEqual(persisted_roles[observation_id], "organization_owner")
        self.assertEqual(persisted_roles[member_event_id], "organization_member")
        self.assertEqual(persisted_roles[reviewer_event_id], "organization_reviewer")

        owner_replay = self.client.get(
            f"/api/records/{record_id}/fieldwork/cycles/participant-scope-cycle/replay",
            params={"scale": "organization"},
        )
        self.assertEqual(owner_replay.status_code, 200, owner_replay.text)
        owner_view = next(
            event
            for event in owner_replay.json()["projection"]["events"]
            if event["event_id"] == observation_id
        )
        self.assertFalse(owner_view["redacted"])
        self.assertEqual(
            owner_view["payload"]["content"],
            "Participant-secret internal field note.",
        )

    def test_organization_role_governs_consent_and_withdrawal_redacts_replay(self):
        self.register_verify_login("fieldwork-owner@example.org")
        created = self.client.post(
            "/api/records",
            json={
                "organization_name": "Consent-governed archive",
                "title": "Participant-account review",
                "proposed_use": "A situated participant research cycle",
            },
            headers=self.headers(),
        ).json()["record"]
        record_id = created["id"]
        now = datetime.now(timezone.utc)
        chronology = {
            "observed_at": (now - timedelta(minutes=3)).isoformat(),
            "recorded_at": (now - timedelta(minutes=2)).isoformat(),
        }
        cycle = self.client.post(
            f"/api/records/{record_id}/fieldwork/cycles",
            json={
                "cycle_id": "participant-cycle-1",
                "label": "Participant account cycle",
                **chronology,
            },
            headers=self.headers(),
        )
        self.assertEqual(cycle.status_code, 201, cycle.text)
        grant = self.client.post(
            f"/api/records/{record_id}/fieldwork/cycles/participant-cycle-1/consent/grants",
            json={
                "subject_id": "participant-pseudonym-1",
                "reason": "The organization owner recorded the participant's consent.",
                "idempotency_key": "owner-consent-grant-0001",
                **chronology,
            },
            headers=self.headers(),
        )
        self.assertEqual(grant.status_code, 201, grant.text)
        self.assertEqual(
            grant.json()["event"]["actor"]["actor_role"], "organization_owner"
        )
        self.assertEqual(
            grant.json()["event"]["manifest"]["sensitivity"], "restricted"
        )
        account = self.client.post(
            f"/api/records/{record_id}/fieldwork/cycles/participant-cycle-1/participant-accounts",
            json={
                "content": "The participant described how access should be bounded.",
                "consent_basis": "granted",
                "consent_subjects": ["participant-pseudonym-1"],
                "allowed_scales": ["organization"],
                "idempotency_key": "participant-account-consent-0001",
                **chronology,
            },
            headers=self.headers(),
        )
        self.assertEqual(account.status_code, 201, account.text)
        account_event_id = account.json()["event"]["event_id"]

        colleague = TestClient(self.app, base_url="http://testserver")
        try:
            colleague_session = colleague.get("/api/auth/session").json()
            colleague_headers = {
                "Origin": "http://testserver",
                "X-CSRF-Token": colleague_session["csrf_token"],
            }
            colleague.post(
                "/api/auth/register",
                json={
                    "email": "fieldwork-colleague@example.org",
                    "password": "long-enough-password",
                },
                headers=colleague_headers,
            )
            verify_token = self.token_from_link(self.mailer.messages[-1].link)
            colleague.post(
                "/api/auth/verify",
                json={"token": verify_token},
                headers=colleague_headers,
            )
            signed_in = colleague.post(
                "/api/auth/login",
                json={
                    "email": "fieldwork-colleague@example.org",
                    "password": "long-enough-password",
                },
                headers=colleague_headers,
            )
            self.assertEqual(signed_in.status_code, 200, signed_in.text)
            colleague_headers["X-CSRF-Token"] = signed_in.json()["csrf_token"]

            member = self.client.post(
                f"/api/organizations/{created['organization_id']}/members",
                json={"email": "fieldwork-colleague@example.org", "role": "member"},
                headers=self.headers(),
            )
            self.assertEqual(member.status_code, 200, member.text)
            denied = colleague.post(
                f"/api/records/{record_id}/fieldwork/cycles/participant-cycle-1/consent/withdrawals",
                json={
                    "subject_id": "participant-pseudonym-1",
                    "reason": "An ordinary member cannot spoof the participant's opt-out.",
                    "idempotency_key": "member-withdraw-denied-0001",
                    **chronology,
                },
                headers=colleague_headers,
            )
            self.assertEqual(denied.status_code, 403, denied.text)

            reviewer = self.client.post(
                f"/api/organizations/{created['organization_id']}/members",
                json={
                    "email": "fieldwork-colleague@example.org",
                    "role": "reviewer",
                },
                headers=self.headers(),
            )
            self.assertEqual(reviewer.status_code, 200, reviewer.text)
            withdrawal = colleague.post(
                f"/api/records/{record_id}/fieldwork/cycles/participant-cycle-1/consent/withdrawals",
                json={
                    "subject_id": "participant-pseudonym-1",
                    "reason": "The authorized reviewer recorded the participant's withdrawal.",
                    "idempotency_key": "reviewer-withdraw-0001",
                    **chronology,
                },
                headers=colleague_headers,
            )
            self.assertEqual(withdrawal.status_code, 201, withdrawal.text)
            self.assertEqual(
                withdrawal.json()["event"]["actor"]["actor_role"],
                "organization_reviewer",
            )
        finally:
            colleague.close()

        replay = self.client.get(
            f"/api/records/{record_id}/fieldwork/cycles/participant-cycle-1/replay",
            params={
                "scale": "organization",
                "as_of_event_id": account_event_id,
            },
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        replayed_account = next(
            event
            for event in replay.json()["projection"]["events"]
            if event["event_id"] == account_event_id
        )
        self.assertTrue(replayed_account["redacted"])
        self.assertIn("consent_withdrawn", replayed_account["payload"]["reasons"])

    def test_beta_signals_are_opt_in_categorical_and_withdrawable(self):
        identity = self.client.get("/api/product-evolution/identity")
        self.assertEqual(identity.status_code, 200, identity.text)
        self.assertEqual(identity.json()["display_name"], "Nonprofit AI toolkit")
        self.assertEqual(identity.json()["source"], "default")

        user = self.register_verify_login()
        status = self.client.get("/api/product-evolution/consent")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["consent"], "not_set")

        before_consent = self.client.post(
            "/api/product-evolution/signals",
            json={
                "signal": "name.preference.fieldwork_loop",
                "idempotency_key": "before-consent-1",
            },
            headers=self.headers(),
        )
        self.assertEqual(before_consent.status_code, 403, before_consent.text)
        consent = self.client.post(
            "/api/product-evolution/consent",
            json={"enabled": True},
            headers=self.headers(),
        )
        self.assertEqual(consent.status_code, 200, consent.text)

        raw_content = self.client.post(
            "/api/product-evolution/signals",
            json={
                "signal": "interface.help_requested",
                "idempotency_key": "raw-content-1",
                "message": "A participant's raw account must not enter telemetry.",
            },
            headers=self.headers(),
        )
        self.assertEqual(raw_content.status_code, 422, raw_content.text)
        identifying_dimension = self.client.post(
            "/api/product-evolution/signals",
            json={
                "signal": "pathway.negotiate_selected",
                "idempotency_key": "bad-dimension-1",
                "route": "person-12345",
            },
            headers=self.headers(),
        )
        self.assertEqual(
            identifying_dimension.status_code,
            422,
            identifying_dimension.text,
        )
        accepted = self.client.post(
            "/api/product-evolution/signals",
            json={
                "signal": "name.preference.fieldwork_loop",
                "idempotency_key": "name-choice-1",
                "helpful": True,
                "pathway_stage": "redline",
                "interface_state": "review_stage",
            },
            headers=self.headers(),
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertFalse(accepted.json()["content_collected"])
        retried = self.client.post(
            "/api/product-evolution/signals",
            json={
                "signal": "name.preference.fieldwork_loop",
                "idempotency_key": "name-choice-1",
                "helpful": True,
                "pathway_stage": "redline",
                "interface_state": "review_stage",
            },
            headers=self.headers(),
        )
        self.assertEqual(retried.status_code, 202, retried.text)
        self.assertEqual(retried.json()["event_id"], accepted.json()["event_id"])

        with self.app.state.db_session_factory() as dbs:
            rows = dbs.scalars(select(ProductTelemetryEventRow)).all()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertIsNotNone(row)
            stored_user = dbs.get(User, user["id"])
            self.assertIsNotNone(stored_user)
            self.assertNotIn("telemetry_scope_id", user)
            self.assertEqual(row.consent_scope_id, stored_user.telemetry_scope_id)
            self.assertNotIn(user["id"], row.consent_scope_id)
            self.assertEqual(row.metrics, {"helpful": True})
            self.assertEqual(row.dimensions["pathway_stage"], "redline")
            consent_rows = dbs.scalars(select(TelemetryConsentDecisionRow)).all()
            self.assertTrue(consent_rows)
            self.assertTrue(
                all(item.actor_id == stored_user.telemetry_scope_id for item in consent_rows)
            )
            self.assertTrue(
                all(user["id"] not in item.actor_id for item in consent_rows)
            )

        withdrawn = self.client.post(
            "/api/product-evolution/consent",
            json={"enabled": False},
            headers=self.headers(),
        )
        self.assertEqual(withdrawn.status_code, 200, withdrawn.text)
        projection = EvolutionStore(
            self.app.state.db_session_factory
        ).authorized_projection(
            ProjectionAuthorization(
                principal_id="worker:evolution",
                purpose="evolution",
                max_sensitivity=TelemetrySensitivity.RESTRICTED,
                allowed_cohorts=frozenset({"beta"}),
                policy_version="telemetry-projection.v1",
            )
        )
        self.assertEqual(projection.evidence.event_count, 0)

    def test_telemetry_withdrawal_survives_auth_pepper_rotation(self):
        user = self.register_verify_login("pepper-rotation@example.org")
        consent = self.client.post(
            "/api/product-evolution/consent",
            json={"enabled": True},
            headers=self.headers(),
        )
        self.assertEqual(consent.status_code, 200, consent.text)
        signal = self.client.post(
            "/api/product-evolution/signals",
            json={
                "signal": "interface.help_requested",
                "idempotency_key": "pepper-rotation-signal-1",
                "interface_state": "review_stage",
            },
            headers=self.headers(),
        )
        self.assertEqual(signal.status_code, 202, signal.text)
        with self.app.state.db_session_factory() as dbs:
            scope_before = dbs.get(User, user["id"]).telemetry_scope_id

        self.client.close()
        self.app.state.db_engine.dispose()
        os.environ["AUTH_PEPPER"] = (
            "rotated-test-pepper-with-more-than-thirty-two-characters"
        )
        self.mailer = MemoryEmailBackend()
        self.model_client = CapturingStubModelClient()
        self.app = create_app(
            Settings.from_env(),
            email_backend=self.mailer,
            model_client=self.model_client,
        )
        self.client = TestClient(self.app, base_url="http://testserver")
        self.csrf = self.client.get("/api/auth/session").json()["csrf_token"]
        login = self.client.post(
            "/api/auth/login",
            json={
                "email": "pepper-rotation@example.org",
                "password": "long-enough-password",
            },
            headers=self.headers(),
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.csrf = login.json()["csrf_token"]
        persisted_consent = self.client.get("/api/product-evolution/consent")
        self.assertEqual(persisted_consent.status_code, 200, persisted_consent.text)
        self.assertEqual(persisted_consent.json()["consent"], "granted")

        withdrawal = self.client.post(
            "/api/product-evolution/consent",
            json={"enabled": False},
            headers=self.headers(),
        )
        self.assertEqual(withdrawal.status_code, 200, withdrawal.text)
        with self.app.state.db_session_factory() as dbs:
            scope_after = dbs.get(User, user["id"]).telemetry_scope_id
            consent_rows = dbs.scalars(
                select(TelemetryConsentDecisionRow).order_by(
                    TelemetryConsentDecisionRow.decided_at
                )
            ).all()
        self.assertEqual(scope_after, scope_before)
        self.assertEqual({item.consent_scope_id for item in consent_rows}, {scope_before})
        self.assertEqual({item.actor_id for item in consent_rows}, {scope_before})

        projection = EvolutionStore(
            self.app.state.db_session_factory
        ).authorized_projection(
            ProjectionAuthorization(
                principal_id="worker:evolution",
                purpose="evolution",
                max_sensitivity=TelemetrySensitivity.RESTRICTED,
                allowed_cohorts=frozenset({"beta"}),
                policy_version="telemetry-projection.v1",
            )
        )
        self.assertEqual(projection.evidence.event_count, 0)

    def test_new_accounts_receive_distinct_hidden_telemetry_scopes(self):
        for email in ("scope-one@example.org", "scope-two@example.org"):
            registered = self.client.post(
                "/api/auth/register",
                json={"email": email, "password": "long-enough-password"},
                headers=self.headers(),
            )
            self.assertEqual(registered.status_code, 202, registered.text)
            self.assertNotIn("telemetry_scope", registered.text)

        with self.app.state.db_session_factory() as dbs:
            users = dbs.scalars(
                select(User).where(
                    User.email.in_(
                        ("scope-one@example.org", "scope-two@example.org")
                    )
                )
            ).all()
        scopes = {item.telemetry_scope_id for item in users}
        self.assertEqual(len(users), 2)
        self.assertEqual(len(scopes), 2)
        self.assertTrue(all(scope.startswith("scope.") for scope in scopes))

    def test_password_reset_revokes_existing_sessions(self):
        self.register_verify_login()
        response = self.client.post(
            "/api/auth/forgot-password",
            json={"email": "person@example.org"},
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 202)
        reset_token = self.token_from_link(self.mailer.messages[-1].link)
        response = self.client.post(
            "/api/auth/reset-password",
            json={"token": reset_token, "password": "a-new-long-password"},
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/records").status_code, 401)

    def test_record_access_follows_organization_membership(self):
        self.register_verify_login("owner@example.org")
        created = self.client.post(
            "/api/records",
            json={
                "organization_name": "Member-owned organization",
                "title": "Shared review",
            },
            headers=self.headers(),
        ).json()["record"]

        colleague = TestClient(self.app, base_url="http://testserver")
        colleague_csrf = colleague.get("/api/auth/session").json()["csrf_token"]
        colleague_headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": colleague_csrf,
        }
        colleague.post(
            "/api/auth/register",
            json={
                "email": "colleague@example.org",
                "password": "long-enough-password",
            },
            headers=colleague_headers,
        )
        verify_token = self.token_from_link(self.mailer.messages[-1].link)
        colleague.post(
            "/api/auth/verify",
            json={"token": verify_token},
            headers=colleague_headers,
        )
        signed_in = colleague.post(
            "/api/auth/login",
            json={
                "email": "colleague@example.org",
                "password": "long-enough-password",
            },
            headers=colleague_headers,
        )
        colleague_headers["X-CSRF-Token"] = signed_in.json()["csrf_token"]
        self.assertEqual(
            colleague.get(f"/api/records/{created['id']}").status_code, 404
        )

        membership = self.client.post(
            f"/api/organizations/{created['organization_id']}/members",
            json={"email": "colleague@example.org", "role": "reviewer"},
            headers=self.headers(),
        )
        self.assertEqual(membership.status_code, 200, membership.text)
        self.assertEqual(
            colleague.get(f"/api/records/{created['id']}").status_code, 200
        )
        colleague.close()

    def test_production_uses_host_prefixed_cookies_and_hides_outbox(self):
        production_db = f"sqlite:///{self.tmp.name}/production-shape.db"
        production_settings = replace(
            Settings.from_env(),
            environment="production",
            database_url=production_db,
            public_app_url="https://testserver",
            allowed_origin="https://testserver",
            cookie_secure=True,
            email_backend="resend",
            resend_api_key="test-only",
            email_from="Toolkit <accounts@example.org>",
        )
        production_mailer = MemoryEmailBackend()
        production_app = create_app(
            production_settings,
            email_backend=production_mailer,
            model_client=StubModelClient(),
        )
        production_client = TestClient(production_app, base_url="https://testserver")
        response = production_client.get("/api/auth/session")
        csrf = response.json()["csrf_token"]
        headers = {"Origin": "https://testserver", "X-CSRF-Token": csrf}
        production_client.post(
            "/api/auth/register",
            json={
                "email": "production-shape@example.org",
                "password": "long-enough-password",
            },
            headers=headers,
        )
        token = self.token_from_link(production_mailer.messages[-1].link)
        production_client.post(
            "/api/auth/verify", json={"token": token}, headers=headers
        )
        response = production_client.post(
            "/api/auth/login",
            json={
                "email": "production-shape@example.org",
                "password": "long-enough-password",
            },
            headers=headers,
        )
        cookies = response.headers.get_list("set-cookie")
        self.assertTrue(any("__Host-toolkit_session=" in item for item in cookies))
        self.assertTrue(any("__Host-toolkit_csrf=" in item for item in cookies))
        self.assertTrue(all("Secure" in item for item in cookies))
        self.assertEqual(production_client.get("/api/dev/outbox").status_code, 404)
        production_client.close()
        production_app.state.db_engine.dispose()

    def test_production_registration_stays_closed_without_email_configuration(self):
        settings = replace(
            Settings.from_env(),
            environment="production",
            database_url=f"sqlite:///{self.tmp.name}/closed-registration.db",
            public_app_url="https://testserver",
            allowed_origin="https://testserver",
            cookie_secure=True,
            email_backend="resend",
            resend_api_key="",
            email_from="",
        )
        closed_app = create_app(settings, model_client=StubModelClient())
        client = TestClient(closed_app, base_url="https://testserver")
        csrf = client.get("/api/auth/session").json()["csrf_token"]
        response = client.post(
            "/api/auth/register",
            json={
                "email": "person@example.org",
                "password": "long-enough-password",
            },
            headers={"Origin": "https://testserver", "X-CSRF-Token": csrf},
        )
        self.assertEqual(response.status_code, 503)
        client.close()
        closed_app.state.db_engine.dispose()


class EmailDelivery(unittest.TestCase):
    def test_resend_idempotency_header_is_hashed(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with patch("backend.mailer.urllib.request.urlopen", return_value=Response()) as send:
            ResendEmailBackend("api-key", "accounts@example.org").send(
                to="person@example.org",
                subject="Verify account",
                text="Use the link.",
                link="https://example.org/#verify?token=raw-secret-token",
            )
        request = send.call_args.args[0]
        headers = {key.casefold(): value for key, value in request.header_items()}
        value = headers["idempotency-key"]
        self.assertEqual(len(value), 64)
        self.assertNotIn("raw-secret-token", value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
