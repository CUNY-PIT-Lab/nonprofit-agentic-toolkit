#!/usr/bin/env python3
"""Key-free source checks for the seven-step, account-backed toolkit."""

from __future__ import annotations

import pathlib
import re
import sys
import unittest


APP = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from backend import prompts  # noqa: E402


INDEX = (APP / "index.html").read_text()
CSS = (APP / "static" / "app.css").read_text()
APP_JS = (APP / "static" / "app.js").read_text()
BACKEND = (APP / "backend" / "app.py").read_text()
SYNTHESIS = (APP / "backend" / "synthesis.py").read_text()
PI_TOOL = APP / "pi-harness" / ".pi" / "extensions" / "concept-map.ts"
PI_SKILL = APP / "pi-harness" / ".pi" / "skills" / "synthesis-concept-map" / "SKILL.md"


class PromptBoundary(unittest.TestCase):
    def test_review_prompts_are_server_owned_and_category_only(self):
        for stage in prompts.STAGE_ORDER:
            prompt = prompts.stage_prompt(stage, "Community organization")
            self.assertIn("exactly one short", prompt.lower())
            self.assertIn("raw records", prompt.lower())
            self.assertIn("data categories", prompt.lower())
            self.assertIn("the organization decides", prompt.lower())
            self.assertNotIn("Fortune", prompt)

    def test_red_line_prompt_preserves_the_external_service_boundary(self):
        prompt = prompts.redline_prompt({"name": "Community organization"}, "Entry facts")
        self.assertIn("Sensitive data in an external AI service is prohibited", prompt)
        self.assertIn("restricted pending human review", prompt)
        self.assertIn("Maple", prompts.redline_prompt({"name": "Maple"}, ""))

    def test_synthesis_prompt_names_the_requested_analysis(self):
        prompt = prompts.synthesis_prompt("Community organization", [])
        for phrase in (
            "context",
            "constraints",
            "affordances",
            "existing AI infrastructure",
            "workflow support",
            "company-knowledge discoverability",
            "general-purpose chatbot",
            "public informational guide",
            "current conditions",
            "decision points",
            "plausible pathways",
            "potentials",
        ):
            self.assertIn(phrase.casefold(), prompt.casefold())
        self.assertIn("evidence_ids", prompt)

    def test_synthesis_validation_requires_provenance(self):
        self.assertIn("if title and detail and ids", SYNTHESIS)
        self.assertIn("if not label or not detail", SYNTHESIS)
        self.assertIn("stable_id", SYNTHESIS)


class LandingInterface(unittest.TestCase):
    def test_landing_explains_the_project_and_all_seven_steps(self):
        self.assertIn("Review whether and how to use AI in your organization.", INDEX)
        self.assertIn("keeps context, decisions, evidence, and open questions", INDEX)
        review = INDEX[INDEX.index('id="review"') : INDEX.index("</ol>")]
        labels = (
            "Red line test",
            "Stress test",
            "Costs and benefits",
            "Hidden curriculum",
            "Accountability",
            "Internal and external review",
            "Synthesis",
        )
        positions = [review.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(review.count('class="step-number"'), 7)

    def test_old_outcome_rail_is_removed(self):
        landing = INDEX[: INDEX.index('class="toolkit-shell"')]
        for phrase in ("Proceed", "Negotiate and return", "Walk away", "outcome-rail"):
            self.assertNotIn(phrase, landing)

    def test_responsive_caret_targets_the_review_header(self):
        self.assertIn('class="review-jump"', INDEX)
        self.assertIn('aria-controls="review"', INDEX)
        self.assertIn("@media (max-width: 1100px)", CSS)
        self.assertRegex(
            CSS,
            r"@media \(max-width: 1100px\)[\s\S]*?\.review-jump\s*\{\s*display:\s*block",
        )
        self.assertIn('window.addEventListener("scroll"', APP_JS)
        self.assertIn('window.addEventListener("resize"', APP_JS)
        self.assertIn("window.scrollTo", APP_JS)
        self.assertIn('review.querySelector(".review-list > li:last-child")', APP_JS)
        self.assertIn('phase === "continue"', APP_JS)
        self.assertIn("lastStep.getBoundingClientRect()", APP_JS)
        self.assertIn("is-returning", APP_JS)
        self.assertNotIn("is-at-review", APP_JS)

    def test_type_scale_is_compact_and_visually_restrained(self):
        sizes = [int(value) for value in re.findall(r"font-size:\s*(\d+)px", CSS)]
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), 42)
        self.assertNotIn("radial-gradient", CSS)
        self.assertNotIn("linear-gradient", CSS)
        self.assertNotIn("box-shadow", CSS)
        self.assertIn("--sans:", CSS)


class AccountInterface(unittest.TestCase):
    def test_verified_account_flow_is_present(self):
        for form_id in (
            "loginForm",
            "registerForm",
            "forgotForm",
            "resetForm",
        ):
            self.assertIn(f'id="{form_id}"', INDEX)
        self.assertIn("verification email before the toolkit opens", INDEX)
        self.assertIn("/api/auth/session", APP_JS)
        self.assertIn("/api/auth/register", APP_JS)
        self.assertIn("/api/auth/login", APP_JS)
        self.assertIn("/api/auth/forgot-password", APP_JS)
        self.assertIn("/api/auth/reset-password", APP_JS)
        self.assertIn("/api/auth/verify", APP_JS)

    def test_sensitive_input_warning_is_visible(self):
        lower = INDEX.casefold()
        self.assertIn("leave out names, raw records, and confidential text", lower)
        self.assertIn("keep sensitive records in approved systems", lower)

    def test_external_files_support_a_strict_csp(self):
        self.assertRegex(INDEX, r'src="/?static/app\.js(?:\?[^"]+)?"')
        self.assertRegex(INDEX, r'src="/?static/vendor/cytoscape\.min\.js(?:\?[^"]+)?"')
        self.assertNotIn("<style", INDEX)
        self.assertNotIn("<script>", INDEX)
        self.assertNotRegex(INDEX, r"\son[a-z]+=")
        self.assertNotIn("'unsafe-inline'", BACKEND)

    def test_access_code_gate_is_absent(self):
        combined = INDEX + APP_JS + BACKEND
        for stale in ("X-Access-Code", "ACCESS_CODE", 'id="gatecode"', 'class="gate"'):
            self.assertNotIn(stale, combined)


class ReviewAndSynthesisInterface(unittest.TestCase):
    def test_entry_and_seven_numbered_steps_are_in_navigation_data(self):
        self.assertIn("Describe the proposal", INDEX)
        for label in (
            "Red line test",
            "Stress test",
            "Costs and benefits",
            "Hidden curriculum",
            "Accountability",
            "Internal and external review",
            "Synthesis",
        ):
            self.assertIn(label, INDEX + APP_JS)

    def test_server_backed_stage_calls_are_retry_safe(self):
        self.assertRegex(APP_JS, r"/stages/\$\{[^}]+\}/start")
        self.assertRegex(APP_JS, r"/stages/\$\{[^}]+\}/messages")
        self.assertRegex(APP_JS, r"/stages/\$\{[^}]+\}/complete")
        self.assertIn("idempotency_key", APP_JS)
        self.assertIn("crypto.randomUUID", APP_JS)

    def test_synthesis_workspace_has_analysis_map_and_annotation(self):
        for label in (
            "Context",
            "Constraints",
            "Affordances",
            "Existing AI infrastructure",
            "Targeted use patterns",
            "Review responses",
            "Regenerate map",
            "Export JSON",
            "Save annotation",
        ):
            self.assertIn(label, INDEX)
        self.assertIn("cytoscape", APP_JS.casefold())
        self.assertIn("/synthesis", APP_JS)
        self.assertIn("/annotations", APP_JS)

    def test_pi_tool_and_skill_ship_with_the_repository(self):
        self.assertTrue(PI_TOOL.is_file())
        self.assertTrue(PI_SKILL.is_file())
        tool = PI_TOOL.read_text()
        skill = PI_SKILL.read_text()
        self.assertIn("build_synthesis_concept_map", tool)
        self.assertIn("current conditions", skill.casefold())
        self.assertIn("decision", skill.casefold())
        self.assertIn("pathways", skill.casefold())
        self.assertIn("road ahead", skill.casefold())


if __name__ == "__main__":
    unittest.main(verbosity=2)
