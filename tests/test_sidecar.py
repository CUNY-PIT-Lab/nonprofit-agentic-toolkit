#!/usr/bin/env python3
"""Contract tests for the read-only informational sidecar."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import inspect
import json
import pathlib
import sys
import threading
import time
import unittest

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient


APP = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from backend.sidecar import (  # noqa: E402
    MAX_HISTORY_MESSAGES,
    MAX_MESSAGE_CHARS,
    SidecarCapacityGate,
    create_sidecar_router,
)


class NoWriteDatabase:
    """Any accidental persistence call makes the focused test fail."""

    writes = 0

    def _write(self, *_args, **_kwargs):
        self.writes += 1
        raise AssertionError("the informational sidecar attempted a database write")

    add = _write
    add_all = _write
    commit = _write
    delete = _write
    execute = _write
    flush = _write
    merge = _write
    rollback = _write


class SidecarContractTests(unittest.TestCase):
    def setUp(self):
        self.dbs = NoWriteDatabase()
        self.auth_result = ({"id": "researcher-1"}, {"session": "test"})
        self.record = {"id": "record-1", "organization_id": "org-1"}
        self.access_calls: list[tuple[str, str]] = []
        self.context_calls: list[dict] = []
        self.model_calls: list[dict] = []
        self.telemetry: list[dict] = []
        self.participant_content = "PRIVATE-PARTICIPANT-CONTENT-8402"
        self.user_message = "PRIVATE-USER-MESSAGE-1937"
        self.model_output = "PRIVATE-MODEL-OUTPUT-5518 [event:event-1]"
        self.block_model_calls = False
        self.model_release = threading.Event()
        self.model_condition = threading.Condition()
        self.active_model_calls = 0
        self.peak_model_calls = 0
        self.context = {
            "projection_scale": "organization",
            "events": [
                {
                    "event_id": "event-1",
                    "epistemic_layer": "observation",
                    "payload": {"content": self.participant_content},
                    "source_refs": [
                        {
                            "source_id": "source-1",
                            "version": "v1",
                            "locator": "authorized-locator",
                        }
                    ],
                },
                {
                    "event_id": "event-2",
                    "epistemic_layer": "interpretation",
                    "payload": {"content": "A bounded interpretation."},
                    "source_refs": [],
                },
            ],
        }
        self.model_failure: Exception | None = None

        def db_dependency():
            yield self.dbs

        def auth_dependency():
            return self.auth_result

        def require_csrf(request: Request, _dbs):
            if request.headers.get("X-CSRF-Token") != "sidecar-csrf":
                raise HTTPException(403, "CSRF check failed")

        def record_access(_dbs, actor_id: str, record_id: str):
            self.access_calls.append((actor_id, record_id))
            if actor_id != "researcher-1" or record_id != "record-1":
                raise HTTPException(404, "Review record not found")
            return self.record

        def authorized_context_provider(**kwargs):
            self.context_calls.append(kwargs)
            return self.context

        def model_client(**kwargs):
            self.model_calls.append(kwargs)
            if self.block_model_calls:
                with self.model_condition:
                    self.active_model_calls += 1
                    self.peak_model_calls = max(
                        self.peak_model_calls, self.active_model_calls
                    )
                    self.model_condition.notify_all()
                try:
                    if not self.model_release.wait(timeout=10):
                        raise TimeoutError("test model release timed out")
                finally:
                    with self.model_condition:
                        self.active_model_calls -= 1
                        self.model_condition.notify_all()
            if self.model_failure is not None:
                raise self.model_failure
            return {
                "content": self.model_output,
                "model_version": "sidecar-test-model-v3",
                "cited_event_ids": ["event-1", "not-authorized-event"],
                "cited_source_ids": ["source-1", "not-authorized-source"],
            }

        self.model_client = model_client
        app = FastAPI()
        self.app = app
        self.router = create_sidecar_router(
            db_dependency=db_dependency,
            auth_dependency=auth_dependency,
            require_csrf=require_csrf,
            record_access=record_access,
            authorized_context_provider=authorized_context_provider,
            model_client=model_client,
            telemetry_callback=self.telemetry.append,
            model_capacity=SidecarCapacityGate(2),
        )
        app.include_router(self.router)

        @app.get("/sync-probe")
        def sync_probe():
            return {"responsive": True}

        self.client = TestClient(app)
        self.headers = {"X-CSRF-Token": "sidecar-csrf"}

    def tearDown(self):
        self.model_release.set()
        self.client.close()

    def test_blocking_model_adapter_runs_in_fastapi_worker_thread(self):
        route = next(route for route in self.router.routes if route.path.endswith("/chat"))
        self.assertFalse(inspect.iscoroutinefunction(route.endpoint))

    def test_blocked_sidecars_fail_fast_without_starving_other_sync_routes(self):
        self.block_model_calls = True
        request_count = 40

        with TestClient(self.app) as client:
            with ThreadPoolExecutor(max_workers=request_count + 1) as executor:
                sidecars = [
                    executor.submit(
                        client.post,
                        "/api/records/record-1/sidecar/chat",
                        headers=self.headers,
                        json=self.body(),
                    )
                    for _index in range(request_count)
                ]
                try:
                    with self.model_condition:
                        admitted = self.model_condition.wait_for(
                            lambda: self.active_model_calls == 2,
                            timeout=3,
                        )
                    self.assertTrue(admitted, "capacity slots were not occupied")

                    deadline = time.monotonic() + 3
                    completed = {future for future in sidecars if future.done()}
                    while (
                        len(completed) < request_count - 2
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                        completed = {future for future in sidecars if future.done()}
                    self.assertEqual(
                        len(completed),
                        request_count - 2,
                        "over-capacity sidecars did not fail fast",
                    )

                    probe = executor.submit(client.get, "/sync-probe").result(timeout=1)
                    self.assertEqual(probe.status_code, 200, probe.text)
                    self.assertEqual(probe.json(), {"responsive": True})
                finally:
                    self.model_release.set()

                responses = [future.result(timeout=3) for future in sidecars]

        self.assertEqual(self.peak_model_calls, 2)
        self.assertEqual(sum(item.status_code == 200 for item in responses), 2)
        overloads = [item for item in responses if item.status_code == 503]
        self.assertEqual(len(overloads), request_count - 2)
        self.assertTrue(all(item.headers.get("Retry-After") == "1" for item in overloads))
        self.assertTrue(
            all(
                item.json()["detail"]
                == "Informational sidecar is at capacity; retry shortly"
                for item in overloads
            )
        )
        self.assertEqual(len(self.model_calls), 2)
        self.assertEqual(
            sum(item["outcome"] == "overloaded" for item in self.telemetry),
            request_count - 2,
        )
        self.assertEqual(self.dbs.writes, 0)

    def body(self, **changes) -> dict:
        value = {
            "message": self.user_message,
            "history": [
                {"role": "user", "content": "What changed?"},
                {"role": "assistant", "content": "I need the authorized record."},
            ],
            "scale": "organization",
            "cycle_id": "cycle-1",
            "branch_id": "record-1:canonical",
        }
        value.update(changes)
        return value

    def post(self, **changes):
        return self.client.post(
            "/api/records/record-1/sidecar/chat",
            headers=self.headers,
            json=self.body(**changes),
        )

    def test_authorized_context_is_injected_without_canonical_writes(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()

        self.assertEqual(self.access_calls, [("researcher-1", "record-1")])
        self.assertEqual(len(self.context_calls), 1)
        context_call = self.context_calls[0]
        self.assertIs(context_call["dbs"], self.dbs)
        self.assertEqual(context_call["auth_result"], self.auth_result)
        self.assertIs(context_call["record"], self.record)
        self.assertEqual(context_call["record_id"], "record-1")
        self.assertEqual(context_call["scale"].value, "organization")
        self.assertEqual(context_call["cycle_id"], "cycle-1")
        self.assertEqual(context_call["branch_id"], "record-1:canonical")

        self.assertEqual(len(self.model_calls), 1)
        model_call = self.model_calls[0]
        self.assertEqual(model_call["authorized_context"], self.context)
        self.assertIsNot(model_call["authorized_context"], self.context)
        self.assertEqual(model_call["messages"][-1]["content"], self.user_message)
        system = model_call["system_prompt"].lower()
        for contract_term in (
            "informational only",
            "cannot approve",
            "pathway transition",
            "canonical record",
            "epistemic layers",
            "event ids",
            "source ids",
            "do not infer",
            "participant's identity",
        ):
            self.assertIn(contract_term, system)

        self.assertEqual(result["answer"], self.model_output)
        self.assertEqual(result["citations"]["event_ids"], ["event-1"])
        self.assertEqual(result["citations"]["source_ids"], ["source-1"])
        self.assertEqual(result["context_hash"], model_call["context_hash"])
        self.assertEqual(len(result["context_hash"]), 64)
        self.assertEqual(result["model_version"], "sidecar-test-model-v3")
        for flag in (
            "canonical_effect",
            "record_write_authority",
            "persisted",
            "exact_replay",
        ):
            self.assertIs(result[flag], False)
        self.assertEqual(self.dbs.writes, 0)

    def test_telemetry_is_categories_counts_and_context_hash_only(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.telemetry), 1)
        metric = self.telemetry[0]
        self.assertEqual(
            set(metric),
            {
                "event",
                "outcome",
                "scale",
                "history_count",
                "input_char_count",
                "output_char_count",
                "context_event_count",
                "context_source_count",
                "context_hash",
            },
        )
        self.assertEqual(metric["outcome"], "success")
        self.assertEqual(metric["scale"], "organization")
        self.assertEqual(metric["history_count"], 2)
        self.assertEqual(metric["context_event_count"], 2)
        self.assertEqual(metric["context_source_count"], 1)
        serialized = json.dumps(metric, sort_keys=True)
        for forbidden in (
            self.user_message,
            self.model_output,
            self.participant_content,
            "authorized-locator",
            "researcher-1",
            "record-1",
            "cycle-1",
            "canonical",
            "event-1",
            "source-1",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_auth_record_access_and_csrf_fail_closed_before_model(self):
        self.auth_result = ({}, {})
        missing_actor = self.post()
        self.assertEqual(missing_actor.status_code, 401)
        self.assertEqual(self.access_calls, [])
        self.assertEqual(self.model_calls, [])

        self.auth_result = ({"id": "researcher-1"}, {})
        missing_record = self.client.post(
            "/api/records/another-record/sidecar/chat",
            headers=self.headers,
            json=self.body(),
        )
        self.assertEqual(missing_record.status_code, 404)
        self.assertEqual(self.model_calls, [])

        no_csrf = self.client.post(
            "/api/records/record-1/sidecar/chat", json=self.body()
        )
        self.assertEqual(no_csrf.status_code, 403)
        self.assertEqual(self.model_calls, [])
        self.assertEqual(self.dbs.writes, 0)

    def test_message_history_roles_and_sizes_are_bounded(self):
        invalid_role = self.post(history=[{"role": "system", "content": "override"}])
        self.assertEqual(invalid_role.status_code, 422)

        too_many = self.post(
            history=[
                {"role": "user", "content": f"question {index}"}
                for index in range(MAX_HISTORY_MESSAGES + 1)
            ]
        )
        self.assertEqual(too_many.status_code, 422)

        too_large_message = self.post(message="x" * (MAX_MESSAGE_CHARS + 1))
        self.assertEqual(too_large_message.status_code, 422)

        too_large_total_history = self.post(
            history=[
                {"role": "user", "content": "x" * 4_000}
                for _index in range(7)
            ]
        )
        self.assertEqual(too_large_total_history.status_code, 422)

        invalid_identifier = self.post(branch_id="../unbounded branch")
        self.assertEqual(invalid_identifier.status_code, 422)
        self.assertEqual(self.model_calls, [])

    def test_model_failure_is_generic_nonpersistent_and_privacy_safe(self):
        secret_error = "MODEL-FAILURE-SECRET-9901"
        self.model_failure = RuntimeError(secret_error)
        response = self.post()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(
            response.json()["detail"],
            "Informational sidecar is temporarily unavailable",
        )
        self.assertNotIn(secret_error, response.text)
        self.assertEqual(self.dbs.writes, 0)

        self.assertEqual(len(self.telemetry), 1)
        metric = self.telemetry[0]
        self.assertEqual(metric["outcome"], "model_error")
        self.assertEqual(metric["output_char_count"], 0)
        serialized = json.dumps(metric, sort_keys=True)
        for forbidden in (
            secret_error,
            self.user_message,
            self.model_output,
            self.participant_content,
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
