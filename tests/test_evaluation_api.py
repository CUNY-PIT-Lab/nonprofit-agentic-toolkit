#!/usr/bin/env python3
"""Authorization, replay, concurrency, and privacy checks for evaluation."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app import create_app
from backend.config import Settings
from backend.evaluation import EvaluationError
from backend.evaluation_store import ConversationEvaluationEventRow
from backend.mailer import MemoryEmailBackend
from backend.model_client import StubModelClient
from backend.models import AdoptionRecord, ConversationTurn, StageState


class EvaluationApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ,
            {
                "APP_ENV": "test",
                "TOOLKIT_SQLITE_URL": f"sqlite:///{self.tmp.name}/toolkit.db",
                "PUBLIC_APP_URL": "http://testserver",
                "EMAIL_BACKEND": "memory",
                "MODEL_BACKEND": "stub",
                "AUTH_PEPPER": "test-pepper-with-more-than-thirty-two-characters",
                "TOOLKIT_EVALUATION_ENABLED": "true",
                "TOOLKIT_EVALUATION_MIN_INACTIVE_SECONDS": "0",
            },
            clear=False,
        )
        self.environment.start()
        self.mailer = MemoryEmailBackend()
        self.app = create_app(
            Settings.from_env(),
            email_backend=self.mailer,
            model_client=StubModelClient(),
        )
        self.clients: list[TestClient] = []
        self.owner, self.owner_headers, _ = self.account("owner@example.org")
        self.reviewer, self.reviewer_headers, _ = self.account("reviewer@example.org")
        self.member, self.member_headers, _ = self.account("member@example.org")
        self.outsider, self.outsider_headers, _ = self.account("outsider@example.org")
        created = self.owner.post(
            "/api/records",
            json={
                "organization_name": "Community review collective",
                "title": "Benefits information guide",
            },
            headers=self.owner_headers,
        )
        self.assertEqual(created.status_code, 201, created.text)
        record = created.json()["record"]
        self.record_id = record["id"]
        self.organization_id = record["organization_id"]
        for email, role in (
            ("reviewer@example.org", "reviewer"),
            ("member@example.org", "member"),
        ):
            response = self.owner.post(
                f"/api/organizations/{self.organization_id}/members",
                json={"email": email, "role": role},
                headers=self.owner_headers,
            )
            self.assertEqual(response.status_code, 200, response.text)
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        with self.app.state.db_session_factory() as session, session.begin():
            state = StageState(
                record_id=self.record_id,
                stage="entry",
                cycle_number=1,
                created_at=old,
                updated_at=old,
            )
            session.add(state)
            session.flush()
            self.stage_state_id = state.id
            for ordinal, (role, content) in enumerate(
                (
                    ("assistant", "What public information should the guide use?"),
                    ("user", "Only current agency guidance and official links."),
                    ("assistant", "Who checks an uncertain answer?"),
                    ("user", "A benefits counselor checks it before publication."),
                ),
                start=1,
            ):
                session.add(
                    ConversationTurn(
                        record_id=self.record_id,
                        stage="entry",
                        cycle_number=1,
                        role=role,
                        content=content,
                        ordinal=ordinal,
                        created_at=old + timedelta(seconds=ordinal),
                    )
                )

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.app.state.db_engine.dispose()
        self.environment.stop()
        self.tmp.cleanup()

    @staticmethod
    def token_from_link(link: str) -> str:
        return parse_qs(urlsplit(link).fragment.split("?", 1)[1])["token"][0]

    def account(self, email: str):
        client = TestClient(self.app, base_url="http://testserver")
        self.clients.append(client)
        csrf = client.get("/api/auth/session").json()["csrf_token"]
        headers = {"Origin": "http://testserver", "X-CSRF-Token": csrf}
        response = client.post(
            "/api/auth/register",
            json={"email": email, "password": "long-enough-password"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 202, response.text)
        token = self.token_from_link(self.mailer.messages[-1].link)
        response = client.post(
            "/api/auth/verify", json={"token": token}, headers=headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = client.post(
            "/api/auth/login",
            json={"email": email, "password": "long-enough-password"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return client, headers, response.json()["user"]

    def detail(self, client=None):
        client = client or self.owner
        response = client.get(f"/api/evaluation/conversations/{self.stage_state_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["conversation"]

    def mutation_body(self, operation_id: str, detail=None):
        detail = detail or self.detail()
        return {
            "operation_id": operation_id,
            "expected_version": detail["evaluation_version"],
            "expected_transcript_checksum": detail["transcript_checksum"],
        }

    def test_status_page_and_metadata_only_list(self):
        status = self.outsider.get("/api/evaluation/status")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["enabled"])
        page = self.outsider.get("/evaluation")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["cache-control"], "no-store")
        anonymous = TestClient(self.app, base_url="http://testserver")
        self.clients.append(anonymous)
        self.assertEqual(
            anonymous.get("/api/evaluation/conversations").status_code, 401
        )

        response = self.owner.get("/api/evaluation/conversations")
        self.assertEqual(response.status_code, 200, response.text)
        conversations = response.json()["conversations"]
        self.assertEqual(len(conversations), 1)
        item = conversations[0]
        self.assertEqual(item["id"], self.stage_state_id)
        self.assertNotIn("turns", item)
        self.assertNotIn("note", item)
        self.assertNotIn("annotations", item)
        self.assertNotIn("content", response.text)
        detail = self.detail()
        self.assertEqual(len(detail["turns"]), 4)
        self.assertEqual(detail["turns"][0]["role"], "assistant")

    def test_role_boundary_cross_org_denial_and_reviewer_private_state(self):
        self.assertEqual(
            self.member.get("/api/evaluation/conversations").status_code, 404
        )
        self.assertEqual(
            self.member.get(
                f"/api/evaluation/conversations/{self.stage_state_id}"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.outsider.get(
                f"/api/evaluation/conversations/{self.stage_state_id}"
            ).status_code,
            404,
        )
        reviewer_detail = self.detail(self.reviewer)
        self.assertEqual(reviewer_detail["evaluation_version"], 0)

        body = {
            **self.mutation_body("owner-placement-0001"),
            "bucket_id": "success",
        }
        response = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/placement",
            json=body,
            headers=self.owner_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        owner_detail = self.detail()
        reviewer_detail = self.detail(self.reviewer)
        self.assertEqual(owner_detail["bucket_id"], "success")
        self.assertEqual(owner_detail["evaluation_version"], 1)
        self.assertIsNone(reviewer_detail["bucket_id"])
        self.assertEqual(reviewer_detail["evaluation_version"], 0)
        reviewer_response = self.reviewer.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/placement",
            json={
                **self.mutation_body("owner-placement-0001", reviewer_detail),
                "bucket_id": "handoff",
            },
            headers=self.reviewer_headers,
        )
        self.assertEqual(reviewer_response.status_code, 200, reviewer_response.text)
        self.assertNotEqual(
            reviewer_response.json()["event_id"], response.json()["event_id"]
        )
        self.assertEqual(self.detail(self.reviewer)["bucket_id"], "handoff")

    def test_csrf_idempotency_stale_writes_and_as_of_replay(self):
        initial = self.detail()
        placement = {
            **self.mutation_body("placement-operation-0001", initial),
            "bucket_id": "needs",
        }
        missing_csrf = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/placement",
            json=placement,
        )
        self.assertEqual(missing_csrf.status_code, 403)
        first = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/placement",
            json=placement,
            headers=self.owner_headers,
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["evaluation_version"], 1)
        retry = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/placement",
            json=placement,
            headers=self.owner_headers,
        )
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(retry.json()["idempotent_replay"])
        self.assertEqual(retry.json()["evaluation_version"], 1)

        conflicting_operation = {
            **placement,
            "bucket_id": "handoff",
        }
        conflict = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/placement",
            json=conflicting_operation,
            headers=self.owner_headers,
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        stale = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/note",
            json={
                **self.mutation_body("stale-note-operation", initial),
                "note": "This must not overwrite the advanced review.",
            },
            headers=self.owner_headers,
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["detail"]["current"]["evaluation_version"], 1)

        current = self.detail()
        note = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/note",
            json={
                **self.mutation_body("review-note-operation", current),
                "note": "The human handoff should be more explicit.",
            },
            headers=self.owner_headers,
        )
        self.assertEqual(note.status_code, 200, note.text)
        self.assertEqual(note.json()["evaluation_version"], 2)
        historical = self.owner.get(
            f"/api/evaluation/conversations/{self.stage_state_id}",
            params={"as_of_evaluation_version": 1},
        )
        self.assertEqual(historical.status_code, 200, historical.text)
        conversation = historical.json()["conversation"]
        self.assertEqual(conversation["evaluation_version"], 1)
        self.assertEqual(conversation["bucket_id"], "needs")
        self.assertIsNone(conversation["note"])

    def test_transcript_staleness_annotation_validation_and_ownership(self):
        initial = self.detail()
        turn_id = initial["turns"][1]["id"]
        annotation = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/annotations/{turn_id}",
            json={
                **self.mutation_body("annotation-operation-0001", initial),
                "category": "helpful",
                "note": "Names a bounded source.",
            },
            headers=self.owner_headers,
        )
        self.assertEqual(annotation.status_code, 200, annotation.text)
        self.assertEqual(annotation.json()["annotation"]["message_id"], turn_id)
        self.assertEqual(annotation.json()["annotation"]["category"], "helpful")
        invalid_category = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/annotations/{turn_id}",
            json={
                **self.mutation_body("invalid-annotation-category", self.detail()),
                "category": "misleading",
                "note": "No",
            },
            headers=self.owner_headers,
        )
        self.assertEqual(invalid_category.status_code, 422)
        wrong_turn = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/annotations/not-a-turn",
            json={
                **self.mutation_body("wrong-turn-operation-0001", self.detail()),
                "category": "unclear",
                "note": "No",
            },
            headers=self.owner_headers,
        )
        self.assertEqual(wrong_turn.status_code, 422, wrong_turn.text)

        before_change = self.detail()
        with self.app.state.db_session_factory() as session, session.begin():
            session.add(
                ConversationTurn(
                    record_id=self.record_id,
                    stage="entry",
                    cycle_number=1,
                    role="assistant",
                    content="A canonical follow-up added by the guided review.",
                    ordinal=5,
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                )
            )
        stale_transcript = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/note",
            json={
                **self.mutation_body("transcript-stale-operation", before_change),
                "note": "Must reload the canonical transcript first.",
            },
            headers=self.owner_headers,
        )
        self.assertEqual(stale_transcript.status_code, 409, stale_transcript.text)
        reloaded = self.detail()
        self.assertEqual(len(reloaded["turns"]), 5)
        self.assertNotEqual(
            reloaded["transcript_checksum"], before_change["transcript_checksum"]
        )
        self.assertEqual(
            reloaded["evaluated_transcript_checksum"],
            before_change["transcript_checksum"],
        )
        accepted_after_reload = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/note",
            json={
                **self.mutation_body("reloaded-transcript-operation", reloaded),
                "note": "The reviewer reloaded the expanded canonical transcript.",
            },
            headers=self.owner_headers,
        )
        self.assertEqual(
            accepted_after_reload.status_code, 200, accepted_after_reload.text
        )
        current = self.detail()
        self.assertEqual(current["evaluation_version"], 2)
        self.assertEqual(
            [event["transcript_checksum"] for event in current["history"]],
            [before_change["transcript_checksum"], current["transcript_checksum"]],
        )

    def test_deleting_one_annotated_turn_does_not_break_the_event_stream(self):
        initial = self.detail()
        deleted_turn_id = initial["turns"][-1]["id"]
        annotation = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/annotations/{deleted_turn_id}",
            json={
                **self.mutation_body("retained-annotation-operation", initial),
                "category": "unclear",
                "note": "This response may later be removed from the transcript.",
            },
            headers=self.owner_headers,
        )
        self.assertEqual(annotation.status_code, 200, annotation.text)
        after_annotation = self.detail()
        note = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/note",
            json={
                **self.mutation_body(
                    "post-annotation-note-operation", after_annotation
                ),
                "note": "A later event must remain replayable.",
            },
            headers=self.owner_headers,
        )
        self.assertEqual(note.status_code, 200, note.text)

        with self.app.state.db_session_factory() as session, session.begin():
            session.delete(session.get(ConversationTurn, deleted_turn_id))

        reloaded = self.detail()
        self.assertEqual(len(reloaded["turns"]), 3)
        self.assertEqual(reloaded["evaluation_version"], 2)
        self.assertEqual(
            [event["evaluation_version"] for event in reloaded["history"]], [1, 2]
        )
        self.assertEqual(reloaded["annotations"][0]["turn_id"], deleted_turn_id)
        placement = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/placement",
            json={
                **self.mutation_body("post-deletion-placement-operation", reloaded),
                "bucket_id": "handoff",
            },
            headers=self.owner_headers,
        )
        self.assertEqual(placement.status_code, 200, placement.text)
        self.assertEqual(placement.json()["evaluation_version"], 3)

    def test_custom_bucket_is_reviewer_owned_and_canonical_rows_are_untouched(self):
        created = self.owner.post(
            "/api/evaluation/buckets",
            json={
                "label": "Follow up",
                "color_key": "violet",
                "operation_id": "custom-bucket-operation",
            },
            headers=self.owner_headers,
        )
        self.assertEqual(created.status_code, 201, created.text)
        bucket_id = created.json()["bucket"]["id"]
        retry = self.owner.post(
            "/api/evaluation/buckets",
            json={
                "label": "Follow up",
                "color_key": "violet",
                "operation_id": "custom-bucket-operation",
            },
            headers=self.owner_headers,
        )
        self.assertTrue(retry.json()["idempotent_replay"])
        reviewer_detail = self.detail(self.reviewer)
        forbidden_bucket = self.reviewer.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/placement",
            json={
                **self.mutation_body("foreign-bucket-operation", reviewer_detail),
                "bucket_id": bucket_id,
            },
            headers=self.reviewer_headers,
        )
        self.assertEqual(forbidden_bucket.status_code, 422, forbidden_bucket.text)

        with self.app.state.db_session_factory() as session:
            turn_count = session.scalar(
                select(func.count())
                .select_from(ConversationTurn)
                .where(ConversationTurn.record_id == self.record_id)
            )
            state_count = session.scalar(
                select(func.count())
                .select_from(StageState)
                .where(StageState.record_id == self.record_id)
            )
        owner_detail = self.detail()
        response = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/placement",
            json={
                **self.mutation_body("custom-placement-operation", owner_detail),
                "bucket_id": bucket_id,
            },
            headers=self.owner_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.db_session_factory() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(ConversationTurn)
                    .where(ConversationTurn.record_id == self.record_id)
                ),
                turn_count,
            )
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(StageState)
                    .where(StageState.record_id == self.record_id)
                ),
                state_count,
            )

    def test_events_are_orm_immutable_and_cascade_with_record(self):
        initial = self.detail()
        response = self.owner.put(
            f"/api/evaluation/conversations/{self.stage_state_id}/note",
            json={
                **self.mutation_body("cascade-note-operation", initial),
                "note": "Review note",
            },
            headers=self.owner_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.db_session_factory() as session:
            row = session.scalar(select(ConversationEvaluationEventRow))
            row.note = "Attempted edit"
            with self.assertRaises(EvaluationError):
                session.commit()
            session.rollback()
        with self.app.state.db_session_factory() as session, session.begin():
            record = session.get(AdoptionRecord, self.record_id)
            session.delete(record)
        with self.app.state.db_session_factory() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(ConversationEvaluationEventRow)
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()
