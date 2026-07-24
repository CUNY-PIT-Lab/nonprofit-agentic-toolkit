#!/usr/bin/env python3
"""Exercise the authenticated seven-stage API against a local development server.

The server must use EMAIL_BACKEND=memory. MODEL_BACKEND may be stub for a
key-free run or ollama for a live model run. Verification still follows the
ordinary token flow; the harness reads the loopback-only development outbox.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from backend.prompts import STAGE_LABELS, STAGE_ORDER, STAGE_SPECS


DEFAULT_BASE = os.environ.get(
    "TOOLKIT_BASE_URL", "http://127.0.0.1:8765"
).rstrip("/")
REASONING_MARKERS = (
    "<think",
    "</think",
    "◁think▷",
    "◁/think▷",
    "<thinking",
    "</thinking",
)

ANSWERS = {
    "entry": (
        "Staff spend hours finding approved public program information across several pages.",
        "Program staff and community members looking for services would be affected.",
        "A useful result would make approved information easier to find while staff retain authority.",
        "The program director can own the review, and the work stops if private records enter the guide.",
    ),
    "redline": (
        "The guide may use approved public pages; participant records and staff notes stay outside it.",
        "Program staff own the public content and must approve changes before publication.",
        "People retain every eligibility and service decision.",
        "Community members need an accessible correction route through program staff.",
        "The program director can suspend the guide when a boundary or approval fails.",
    ),
    "stress": (
        "The guide could return outdated hours or unsupported eligibility information.",
        "Every answer should link to an approved source and state when staff confirmation is needed.",
        "The public site needs accessible navigation and a staff contact when the guide fails.",
        "Program staff will review corrections and can return visitors to the existing website.",
    ),
    "cost_benefit": (
        "Community members may find information faster, while staff spend less time repeating lookups.",
        "Staff still need time to review sources, corrections, accessibility, and vendor changes.",
        "A maintained search page is the main non-AI comparison.",
        "The organization would compare findability, staff maintenance time, and correction requests.",
    ),
    "hidden_curriculum": (
        "The guide could make published information appear more complete than staff knowledge.",
        "Staff authority and community questions should remain visible in every pathway.",
        "Unpublished knowledge and translation work could be excluded from the public sources.",
        "The organization should review dependence on the provider and retain a usable website fallback.",
    ),
    "accountability": (
        "The program director owns the guide, and communications staff own approved source changes.",
        "Every answer should show its public source and a route to staff explanation.",
        "Staff will record incidents, corrections, and unresolved questions.",
        "A quarterly review can continue, revise, suspend, or retire the guide.",
    ),
    "internal_external_review": (
        "Program staff, communications staff, and community members who use the site should review it.",
        "The existing program and privacy leads provide internal approval.",
        "A small participant review can test access, clarity, and correction routes.",
        "The organization records conditions, dissent, owners, and the final human decision.",
    ),
}


def short(text: str, limit: int = 180) -> str:
    return " / ".join((text or "").strip().splitlines())[:limit]


def leaks_reasoning(text: str) -> bool:
    lowered = (text or "").casefold()
    return any(marker.casefold() in lowered for marker in REASONING_MARKERS)


class Checks:
    def __init__(self):
        self.passed = 0
        self.failed: list[str] = []

    def ok(self, condition: bool, label: str) -> None:
        if condition:
            self.passed += 1
            print(f"  ✓ {label}")
        else:
            self.failed.append(label)
            print(f"  ✗ {label}")


class LocalClient:
    def __init__(self, base_url: str):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("The simulation runs only against a loopback HTTP server")
        self.base = base_url.rstrip("/")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.csrf = ""
        cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookies)
        )

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict | None = None,
        idempotency_key: str | None = None,
        timeout: int = 140,
    ) -> dict:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if method not in {"GET", "HEAD", "OPTIONS"}:
            headers["Origin"] = self.origin
            if self.csrf:
                headers["X-CSRF-Token"] = self.csrf
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                detail = json.loads(raw or b"{}").get("detail", "request failed")
            except Exception:
                detail = "request failed"
            raise RuntimeError(f"{method} {path} returned {error.code}: {detail}") from error
        return json.loads(raw or b"{}")


def extract_fragment_token(link: str, expected_kind: str) -> str:
    fragment = urllib.parse.urlsplit(link).fragment
    kind, separator, query = fragment.partition("?")
    if separator != "?" or kind != expected_kind:
        raise RuntimeError("Development email contained an unexpected link")
    token = urllib.parse.parse_qs(query).get("token", [""])[0]
    if len(token) < 20:
        raise RuntimeError("Development email token was missing")
    return token


def latest_assistant(payload: dict) -> str:
    message = payload.get("message")
    if isinstance(message, dict):
        return str(message.get("content") or "")
    messages = payload.get("messages") or []
    for item in reversed(messages):
        if item.get("role") == "assistant":
            return str(item.get("content") or "")
    return ""


def run(base_url: str, verbose: bool) -> Checks:
    client = LocalClient(base_url)
    checks = Checks()

    health = client.request("/health")
    checks.ok(health.get("status") == "ok", "development server is healthy")
    session = client.request("/api/auth/session")
    client.csrf = str(session.get("csrf_token") or "")
    checks.ok(bool(client.csrf), "pre-authentication CSRF token issued")

    synthetic_email = f"toolkit-simulation-{uuid.uuid4().hex}@example.org"
    synthetic_password = secrets.token_urlsafe(24)
    client.request(
        "/api/auth/register",
        method="POST",
        body={
            "email": synthetic_email,
            "password": synthetic_password,
            "display_name": "Toolkit simulation",
        },
    )
    outbox = client.request("/api/dev/outbox")
    matching = [
        message
        for message in outbox.get("messages", [])
        if message.get("to") == synthetic_email
    ]
    checks.ok(bool(matching), "verification email reached the local memory outbox")
    if not matching:
        return checks
    verification_token = extract_fragment_token(matching[-1]["link"], "verify")
    client.request(
        "/api/auth/verify",
        method="POST",
        body={"token": verification_token},
    )
    login = client.request(
        "/api/auth/login",
        method="POST",
        body={"email": synthetic_email, "password": synthetic_password},
    )
    client.csrf = str(login.get("csrf_token") or "")
    checks.ok(
        bool(login.get("authenticated"))
        and bool((login.get("user") or {}).get("email_verified")),
        "verified account signed in",
    )

    created = client.request(
        "/api/records",
        method="POST",
        body={
            "organization_name": "Synthetic Community Network",
            "title": "Public information guide review",
            "proposed_use": (
                "A public information guide that helps visitors find approved program "
                "information without using participant records."
            ),
        },
    )
    record = created.get("record") or {}
    record_id = str(record.get("id") or "")
    checks.ok(bool(record_id), "adoption record created")

    for stage in STAGE_ORDER:
        started = client.request(
            f"/api/records/{record_id}/stages/{stage}/start",
            method="POST",
            body={},
        )
        opening = latest_assistant(started)
        checks.ok(bool(opening), f"{STAGE_LABELS[stage]} opened with guidance")
        checks.ok(
            not leaks_reasoning(opening),
            f"{STAGE_LABELS[stage]} opening contains no reasoning trace",
        )
        for index in range(STAGE_SPECS[stage]["answers"]):
            key = str(uuid.uuid4())
            reply = client.request(
                f"/api/records/{record_id}/stages/{stage}/messages",
                method="POST",
                body={
                    "content": ANSWERS[stage][index],
                    "idempotency_key": key,
                },
                idempotency_key=key,
            )
            assistant = latest_assistant(reply)
            checks.ok(bool(assistant), f"{STAGE_LABELS[stage]} response {index + 1} saved")
            checks.ok(
                not leaks_reasoning(assistant),
                f"{STAGE_LABELS[stage]} response {index + 1} contains no reasoning trace",
            )
            if verbose:
                print(f"    {short(assistant, 320)}")
        completed = client.request(
            f"/api/records/{record_id}/stages/{stage}/complete",
            method="POST",
            body={},
        )
        checks.ok(
            completed.get("next_stage") is not None,
            f"{STAGE_LABELS[stage]} completed",
        )

    generated = client.request(
        f"/api/records/{record_id}/synthesis",
        method="POST",
        body={},
    )
    synthesis = generated.get("synthesis") or {}
    concept_map = generated.get("concept_map") or {}
    graph = concept_map.get("graph") or {}
    nodes = graph.get("nodes") or []
    checks.ok(bool(synthesis.get("summary")), "synthesis summary generated")
    checks.ok(bool(nodes), "versioned concept map generated")

    if nodes:
        annotation = client.request(
            f"/api/records/{record_id}/annotations",
            method="POST",
            body={
                "concept_map_id": concept_map.get("id"),
                "target_type": "node",
                "target_id": nodes[0].get("id"),
                "body": "Confirm this point with program and community reviewers.",
            },
        )
        checks.ok(
            bool((annotation.get("annotation") or {}).get("id")),
            "concept-map annotation saved",
        )

    saved = (client.request(f"/api/records/{record_id}").get("record") or {})
    checks.ok(len(saved.get("completed_steps") or []) == 7, "all seven stages persisted")
    checks.ok(bool(saved.get("knowledge_snippets")), "knowledge snippets persisted")
    checks.ok(bool(saved.get("annotations")), "saved annotation returned with the record")
    client.request("/api/auth/logout", method="POST", body={})
    return checks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    try:
        checks = run(args.base_url.rstrip("/"), args.verbose)
    except Exception as error:
        sys.exit(f"simulation stopped: {error}")

    print(f"\n{checks.passed} passed · {len(checks.failed)} failed")
    if checks.failed:
        for label in checks.failed:
            print(f"  - {label}")
        raise SystemExit(1)
    print("authenticated seven-stage simulation passed")


if __name__ == "__main__":
    main()
