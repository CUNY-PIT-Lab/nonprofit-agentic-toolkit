#!/usr/bin/env python3
"""Static contract checks for the protected evaluation workspace."""

from __future__ import annotations

import pathlib
import re
import unittest


APP = pathlib.Path(__file__).resolve().parents[1]
HTML = (APP / "evaluation.html").read_text()
CSS = (APP / "static" / "evaluation.css").read_text()
JS = (APP / "static" / "evaluation.js").read_text()


class EvaluationSurface(unittest.TestCase):
    def test_page_is_private_by_default_and_uses_only_local_assets(self):
        self.assertIn('name="robots" content="noindex,nofollow,noarchive"', HTML)
        self.assertRegex(HTML, r'href="/static/evaluation\.css(?:\?[^\"]+)?"')
        self.assertRegex(HTML, r'src="/static/evaluation\.js(?:\?[^\"]+)?"')
        self.assertNotRegex(HTML, r'(?:src|href)="https?://')
        self.assertNotIn("<style", HTML)
        self.assertNotRegex(HTML, r"\son[a-z]+=")

    def test_login_workspace_and_review_dialogs_are_present(self):
        for element_id in (
            "access-view",
            "login-form",
            "workspace",
            "conversation-board",
            "conversation-search",
            "bucket-visibility",
            "bucket-sort",
            "bucket-layout",
            "transcript-dialog",
            "review-note-form",
            "bucket-dialog",
            "account-dialog",
        ):
            self.assertIn(f'id="{element_id}"', HTML)
        for label in (
            "Not yet reviewed",
            "Success",
            "Needs work",
            "Handoff",
            "Helpful",
            "Unclear",
            "Incorrect",
            "Safety concern",
            "Other",
        ):
            self.assertIn(label, HTML + JS)

    def test_toolkit_brand_tokens_and_responsive_board_are_retained(self):
        self.assertIn("--ink: #0b1b42", CSS)
        self.assertIn("--blue: #064bc2", CSS)
        self.assertIn("--paper: #ffffff", CSS)
        self.assertIn("grid-template-columns: repeat(auto-fit", CSS)
        self.assertIn('.board[data-layout="compact"]', CSS)
        self.assertIn("@media (max-width: 720px)", CSS)
        self.assertIn("@media (prefers-reduced-motion: reduce)", CSS)


class EvaluationClientContract(unittest.TestCase):
    def test_existing_auth_and_planned_evaluation_routes_are_consumed(self):
        for route in (
            "/api/auth/session",
            "/api/auth/login",
            "/api/auth/logout",
            "/api/evaluation/status",
            "/api/evaluation/buckets",
            "/api/evaluation/conversations?limit=100",
            "/placement",
            "/note",
            "/annotations/",
        ):
            self.assertIn(route, JS)
        self.assertIn('method: "POST"', JS)
        self.assertIn('method: "PUT"', JS)

    def test_mutations_are_same_origin_csrf_protected_and_optimistic(self):
        self.assertIn('credentials: "same-origin"', JS)
        self.assertIn('headers.set("X-CSRF-Token", state.csrfToken)', JS)
        self.assertIn('cache: "no-store"', JS)
        self.assertIn("expected_version", JS)
        self.assertIn("expected_transcript_checksum", JS)
        self.assertIn("operation_id", JS)
        self.assertIn("window.crypto.randomUUID", JS)
        self.assertIn("error.status === 409", JS)
        self.assertIn(
            "expected_version: asNumber(conversation.evaluation_version, 0)",
            JS,
        )
        self.assertIn("state.pendingMutations.has(intentKey)", JS)
        self.assertIn("operationForIntent(intentKey)", JS)
        self.assertIn("submit.disabled = true", JS)

    def test_unauthorized_responses_clear_sensitive_rendered_state(self):
        self.assertIn("response.status === 401", JS)
        self.assertIn("clearSensitiveEvaluationState(", JS)
        self.assertIn("await refreshUnauthenticatedCsrf()", JS)
        self.assertIn('window.fetch("/api/auth/session"', JS)
        for reset in (
            'state.openConversation = null',
            'state.conversations = []',
            'state.buckets = []',
            "board.replaceChildren()",
            "transcript.replaceChildren()",
            'search.value = ""',
            "bucketForm.reset()",
            'transcriptMeta.textContent = ""',
            'reviewNote.value = ""',
            'moveStatus.textContent = ""',
        ):
            self.assertIn(reset, JS)

    def test_conflict_state_replaces_annotations(self):
        self.assertIn(
            'Object.prototype.hasOwnProperty.call(evaluation, "annotations")',
            JS,
        )
        self.assertIn(
            "evaluation.annotations.map(normalizeAnnotation)",
            JS,
        )

    def test_annotation_responses_accept_backend_turn_ids(self):
        self.assertIn("item && item.message_id, item && item.turn_id", JS)

    def test_server_text_is_rendered_with_dom_apis_not_html_injection(self):
        self.assertIn("document.createElement", JS)
        self.assertIn("textContent", JS)
        self.assertIn("replaceChildren", JS)
        self.assertNotIn("innerHTML", JS)
        self.assertNotIn("insertAdjacentHTML", JS)
        self.assertNotIn("document.write", JS)

    def test_dragging_has_click_keyboard_and_select_fallbacks(self):
        self.assertIn('card.addEventListener("dragstart"', JS)
        self.assertIn('section.addEventListener("drop"', JS)
        self.assertIn('select.setAttribute("aria-label"', JS)
        self.assertIn('card.addEventListener("keydown"', JS)
        self.assertIn('event.key', JS)
        self.assertIn("Open transcript", JS)

    def test_synthetic_preview_is_impossible_on_a_production_hostname(self):
        guard = re.compile(
            r'\["127\.0\.0\.1",\s*"localhost"\]\.includes\(window\.location\.hostname\)'
            r'[\s\S]*?get\("preview"\)\s*===\s*"1"'
        )
        self.assertRegex(JS, guard)
        self.assertNotIn("preview=true", JS)
        self.assertNotIn("window.location.hostname.includes", JS)


if __name__ == "__main__":
    unittest.main()
