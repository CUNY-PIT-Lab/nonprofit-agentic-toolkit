#!/usr/bin/env python3
"""Key-free checks for the decision-led toolkit flow."""
import pathlib
import sys
import unittest


APP = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import server  # noqa: E402


INDEX = (APP / "index.html").read_text()
SERVER = (APP / "server.py").read_text()


class DecisionPrompts(unittest.TestCase):
    def test_entry_screen_collects_a_cumulative_record(self):
        self.assertIn("strategic fit entry screen", server.ONBOARD)
        self.assertIn("exactly ONE", server.ONBOARD)
        self.assertIn("FEWER THAN 4", server.ONBOARD)
        self.assertIn("Use only facts", server.ONBOARD)
        self.assertIn("→ Step 1: red line test", server.ONBOARD)
        self.assertIn("Use sentence case", server.ONBOARD)
        self.assertIn("at most one question mark", server.ONBOARD)

    def test_entry_record_separates_decisions_and_unknowns(self):
        self.assertIn("**Entry record**", server.ESTIMATE)
        self.assertIn("**Decisions made**", server.ESTIMATE)
        self.assertIn("**Next test**", server.ESTIMATE)
        self.assertIn("Do not invent", server.ESTIMATE)

    def test_red_line_test_requests_categories_instead_of_records(self):
        prompt = server.redline_prompt({"name": "Maple Center"}, "Entry facts")
        self.assertIn("red line test", prompt)
        self.assertIn("Never request", prompt)
        self.assertIn("raw records", prompt)
        self.assertIn("Sensitive data in an external AI tool is Prohibited", prompt)
        self.assertIn("Outcome: yes", prompt)
        self.assertIn("Outcome: maybe", prompt)
        self.assertIn("Outcome: no", prompt)
        self.assertIn("Use no heading, question number, praise, recap, list, or example", prompt)
        self.assertIn("Maple Center", prompt)
        self.assertNotIn("Fortune", prompt)

    def test_redline_mode_is_dispatched_server_side(self):
        self.assertIn('elif mode == "redline":', SERVER)
        self.assertIn('system = redline_prompt(', SERVER)
        self.assertNotIn("def privacy_prompt", SERVER)
        self.assertNotIn("def assistant_prompt", SERVER)


class DecisionInterface(unittest.TestCase):
    def test_landing_screen_explains_the_process_before_entry(self):
        landing = INDEX[INDEX.index('id="landing"'):INDEX.index('id="toolkitApp"')]
        self.assertIn("Decide whether an AI proposal fits your organization.", landing)
        self.assertIn("Start the review", landing)
        self.assertIn("The review", landing)
        self.assertIn("Proceed", landing)
        self.assertIn("Negotiate and return", landing)
        self.assertIn("Walk away", landing)
        self.assertNotIn("<input", landing)
        self.assertNotIn("<textarea", landing)
        self.assertIn('<div class="app hidden" id="toolkitApp">', INDEX)
        self.assertIn("function startToolkit()", INDEX)

    def test_entry_screen_and_six_tests_are_present_in_order(self):
        nav = INDEX[INDEX.index("<aside"):INDEX.index("</aside>")]
        labels = [
            "Describe the proposal",
            "Red line test",
            "Stress test",
            "Costs and benefits",
            "Hidden curriculum",
            "Accountability",
            "Internal and external review",
        ]
        positions = [nav.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))

    def test_red_line_test_uses_its_mode_and_carries_context(self):
        self.assertGreaterEqual(INDEX.count("glm('redline'"), 2)
        self.assertIn("profile.context += '\\n— Step 1 response: ' + v", INDEX)
        self.assertIn("reviewed this with the responsible owners", INDEX)

    def test_three_routes_control_progression(self):
        self.assertIn("Outcome:\\s*YES", INDEX)
        self.assertIn("Outcome:\\s*MAYBE", INDEX)
        self.assertIn("Outcome:\\s*NO", INDEX)
        self.assertIn("Negotiate and return", INDEX)
        self.assertIn("Walk away recorded", INDEX)
        self.assertIn("The stress test is unlocked", INDEX)

    def test_legacy_application_assistant_is_removed(self):
        for stale in [
            "Application Layer",
            "Tool &amp; Vendor Fit",
            "vendor fit",
            "fortuneExample",
            "use the fortune example",
            "ask anything — a service question",
            "glm('assistant'",
        ]:
            self.assertNotIn(stale, INDEX)

    def test_sensitive_input_warning_is_visible(self):
        lower = INDEX.lower()
        self.assertIn("do not enter names, client records, or confidential text", lower)
        self.assertIn("describe categories and current practice", lower)
        self.assertIn("external model", lower)

    def test_chat_workspace_uses_the_available_viewport(self):
        self.assertIn("grid-template-columns:260px minmax(0,1fr)", INDEX)
        self.assertIn("min-height:calc(100vh - 280px)", INDEX)
        self.assertIn("max-width:none", INDEX)
        self.assertNotIn("max-width:680px", INDEX)
        self.assertIn('class="page-title">Describe the proposal', INDEX)

    def test_interface_uses_one_restrained_type_system(self):
        self.assertNotIn("Iowan Old Style", INDEX)
        self.assertNotIn("Palatino Linotype", INDEX)
        self.assertNotIn("radial-gradient", INDEX)
        self.assertNotIn("box-shadow:8px", INDEX)
        self.assertIn("--sans:-apple-system", INDEX)

    def test_interface_has_no_decorative_emoji(self):
        self.assertNotIn("🧰", INDEX)
        self.assertNotIn("🔒", INDEX)

    def test_access_code_gate_is_removed(self):
        combined = INDEX + "\n" + SERVER
        for stale in [
            'class="gate"',
            'id="gatecode"',
            "/api/auth",
            "X-Access-Code",
            "ACCESS_CODE",
        ]:
            self.assertNotIn(stale, combined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
