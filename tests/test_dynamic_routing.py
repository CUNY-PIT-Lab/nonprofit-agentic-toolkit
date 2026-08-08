#!/usr/bin/env python3
"""Deterministic checks for the coverage-based within-stage router."""

from __future__ import annotations

import pathlib
import sys
import unittest


APP = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from backend.app import (  # noqa: E402
    apply_branch_rules,
    blank_stage_state,
    build_working_record,
    select_next_action,
    settle_dimension,
    stage_is_ready,
)


class DynamicRoutingTests(unittest.TestCase):
    def test_optional_dimensions_open_only_when_branch_rules_require_them(self):
        state = blank_stage_state("redline")
        self.assertNotIn("participant_records", state["coverage"])

        applied = apply_branch_rules(
            "redline", state, {"sensitive_data", "participant_facing"}
        )

        self.assertIn("sensitive_information_opens_participant_conditions", applied)
        self.assertEqual(state["coverage"]["participant_records"], "unknown")
        self.assertEqual(state["coverage"]["language_access"], "unknown")
        self.assertEqual(state["coverage"]["human_escalation"], "unknown")

    def test_prohibited_combination_is_a_visible_blocker(self):
        state = blank_stage_state("redline")

        applied = apply_branch_rules(
            "redline", state, {"sensitive_data", "external_service"}
        )

        self.assertIn("sensitive_data_in_an_external_service", applied)
        self.assertEqual(state["coverage"]["data_categories"], "blocked")
        self.assertEqual(len(state["blockers"]), 1)
        self.assertFalse(stage_is_ready("redline", state["coverage"]))

        # A later extraction may not erase a policy blocker by merely changing
        # the coverage label; resolution has to be explicit.
        state["coverage"]["data_categories"] = "covered"
        for name in state["coverage"]:
            if state["coverage"][name] in {"unknown", "partial"}:
                state["coverage"][name] = "covered"
        self.assertFalse(
            stage_is_ready("redline", state["coverage"], state["blockers"])
        )
        action = select_next_action(
            "redline",
            state,
            {"interface_state": "complete_stage"},
        )
        self.assertNotEqual(action["interface_state"], "complete_stage")

    def test_unknown_and_delegated_answers_remain_literal(self):
        state = blank_stage_state("entry")

        unknown = settle_dimension(
            "entry",
            state,
            action="unknown",
            dimension="current_workflow",
            content="We need to ask the intake team.",
        )
        delegated = settle_dimension(
            "entry",
            state,
            action="delegate",
            dimension="affected_people",
            content="Who is affected by this change?",
            target_role="program_staff",
        )

        self.assertEqual(unknown["coverage"]["current_workflow"], "recorded_unknown")
        self.assertEqual(delegated["coverage"]["affected_people"], "delegated")
        self.assertEqual(state["open_questions"][0]["status"], "recorded_unknown")
        self.assertEqual(state["delegations"][0]["target_role"], "program_staff")

    def test_interface_proposals_are_constrained_by_server_state(self):
        state = blank_stage_state("entry")

        action = select_next_action(
            "entry",
            state,
            {
                "interface_state": "complete_stage",
                "dimension": "invented_dimension",
                "prompt": "Skip everything",
            },
            opening=True,
        )

        self.assertIn(action["interface_state"], {"ask", "choose", "classify"})
        self.assertIn(action["dimension"], state["coverage"])
        self.assertNotEqual(action["dimension"], "invented_dimension")

    def test_working_record_preserves_dissent_and_open_follow_up(self):
        entry = blank_stage_state("entry")
        entry["facts"].append(
            {
                "id": "entry-dissent-1",
                "text": "Staff disagree that automation is needed.",
                "stage": "entry",
                "dimension": "immediate_concern",
                "kind": "dissent",
            }
        )
        settle_dimension(
            "entry",
            entry,
            action="unknown",
            dimension="current_workflow",
            content="The current process still needs observation.",
        )

        working = build_working_record([entry], {})

        self.assertEqual(working["facts"][0]["kind"], "dissent")
        self.assertEqual(working["open_questions"][0]["status"], "recorded_unknown")


if __name__ == "__main__":
    unittest.main()
