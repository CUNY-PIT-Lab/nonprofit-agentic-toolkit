#!/usr/bin/env python3
"""Key-free account, ownership, persistence, and synthesis checks."""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from fastapi.testclient import TestClient

APP = pathlib.Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(APP))

from backend.app import create_app  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.mailer import MemoryEmailBackend, ResendEmailBackend  # noqa: E402
from backend.model_client import StubModelClient  # noqa: E402
from backend.prompts import STAGE_ORDER, STAGE_SPECS  # noqa: E402


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
            }
        )
        self.mailer = MemoryEmailBackend()
        self.app = create_app(
            Settings.from_env(),
            email_backend=self.mailer,
            model_client=StubModelClient(),
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
        self.assertNotIn("unsafe-inline", session_response.headers["content-security-policy"])
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
