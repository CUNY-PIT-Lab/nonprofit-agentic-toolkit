"""Server-owned stage definitions, routing prompts, and model-output cleanup.

The review is defined by coverage, not by a number of messages. Each stage names
the dimensions that may need coverage, the branch rules that open or close
dimensions in response to extracted signals, and the completion rules that
decide when the stage can be drafted. The model routes and phrases; the server
validates every routing decision against these definitions.
"""

from __future__ import annotations

import json
import re
from typing import Iterable


_REASONING_BLOCK = re.compile(
    r"<think\b[^>]*>.*?</think\s*>"
    r"|<thinking\b[^>]*>.*?</thinking\s*>"
    r"|◁think▷.*?◁/think▷",
    re.IGNORECASE | re.DOTALL,
)
_CLOSE_TAG = re.compile(r"</think\s*>|</thinking\s*>|◁/think▷", re.IGNORECASE)
_ORPHAN_OPEN = re.compile(r"<think\b[^>]*>|<thinking\b[^>]*>|◁think▷", re.IGNORECASE)


def strip_reasoning(text: str | None) -> str | None:
    """Remove common inline reasoning traces without altering clean content."""
    if not text:
        return text
    cleaned = _REASONING_BLOCK.sub("", text)
    closes = list(_CLOSE_TAG.finditer(cleaned))
    if closes:
        cleaned = cleaned[closes[-1].end() :]
    return _ORPHAN_OPEN.sub("", cleaned).strip()


STAGE_ORDER = (
    "entry",
    "redline",
    "stress",
    "cost_benefit",
    "hidden_curriculum",
    "accountability",
    "internal_external_review",
)

STAGE_LABELS = {
    "entry": "Describe the proposal",
    "redline": "Red line test",
    "stress": "Stress test",
    "cost_benefit": "Costs and benefits",
    "hidden_curriculum": "Hidden curriculum",
    "accountability": "Accountability",
    "internal_external_review": "Internal and external review",
}


# Coverage replaces answer counts. A dimension is closed for completion once it
# reaches a terminal status; unresolved terminal statuses stay visible in the
# working record so nothing disappears into an assistant paragraph.
COVERAGE_STATUSES = (
    "unknown",
    "partial",
    "covered",
    "recorded_unknown",
    "delegated",
    "blocked",
    "skipped",
    "not_applicable",
)
TERMINAL_COVERAGE = {
    "covered",
    "recorded_unknown",
    "delegated",
    "blocked",
    "skipped",
    "not_applicable",
}
UNRESOLVED_COVERAGE = {"recorded_unknown", "delegated", "blocked"}


# The complete interface vocabulary the browser knows how to render.
INTERFACE_STATES = (
    "ask",
    "choose",
    "confirm",
    "classify",
    "resolve_conflict",
    "delegate",
    "record_unknown",
    "review_stage",
    "complete_stage",
    "stop_route",
)

# Signals extracted from every reply. Deliberately small and legible.
SIGNAL_KEYS = (
    "facts",
    "uncertain",
    "constraints",
    "resources_available",
    "resources_missing",
    "affected_people",
    "owners",
    "approvals",
    "risks",
    "open_questions",
    "contradictions",
)

# A closed tag vocabulary. Branch rules read these; nothing else steers routing.
SIGNAL_TAGS = (
    "public_data_only",
    "internal_material",
    "sensitive_data",
    "data_class_unclear",
    "participant_facing",
    "internal_staff_tool",
    "decision_support",
    "external_service",
    "consent_missing",
    "no_human_review",
    "no_recourse",
    "language_access_gap",
    "prohibited_use",
    "small_organization",
    "volunteer_run",
    "internal_technical_staff",
    "vendor_dependence",
    "capacity_gap",
    "owner_unassigned",
    "existing_system_integration",
)

USE_PATTERNS = (
    "internal_workflow",
    "organizational_knowledge",
    "public_information",
    "participant_services",
    "decision_support",
    "other",
)

REVIEW_ROLES = (
    "program_staff",
    "participant_advisory",
    "operations",
    "board_leadership",
    "technical_support",
    "legal_or_compliance",
)

ROLE_LABELS = {
    "program_staff": "Program staff",
    "participant_advisory": "Participant advisory group",
    "operations": "Operations",
    "board_leadership": "Board or leadership",
    "technical_support": "Technical support",
    "legal_or_compliance": "Legal or compliance",
}


# Bounded option sets for the classify state. The server never accepts a
# classification the stage definition does not name.
CLASSIFICATIONS = {
    "use_pattern": {
        "question": "Which description fits the proposal best?",
        "options": [
            {
                "id": "internal_workflow",
                "label": "Internal workflow or administrative task",
                "detail": "Staff work that happens inside the organization.",
            },
            {
                "id": "organizational_knowledge",
                "label": "Finding or organizing our own material",
                "detail": "Internal documents, policies, notes, or history.",
            },
            {
                "id": "public_information",
                "label": "Public information for a website or guide",
                "detail": "Material already approved for publication.",
            },
            {
                "id": "participant_services",
                "label": "Something participants would use directly",
                "detail": "Applicants, clients, members, or the public interacting with it.",
            },
            {
                "id": "decision_support",
                "label": "Help deciding about people or resources",
                "detail": "Eligibility, prioritization, allocation, or assessment.",
            },
            {"id": "other", "label": "Something else", "detail": "None of these fit."},
        ],
    },
    "data_class": {
        "question": "Which description fits the information involved?",
        "options": [
            {
                "id": "public_data_only",
                "label": "Public information approved for publication",
                "detail": "Already published or cleared for the public.",
            },
            {
                "id": "internal_material",
                "label": "Internal material used only by staff",
                "detail": "Notes, budgets, procedures, grant material, or unpublished stories.",
            },
            {
                "id": "sensitive_data",
                "label": "Participant, applicant, donor, or staff information",
                "detail": "Identifying, health, legal, financial, or case information.",
            },
            {
                "id": "data_class_unclear",
                "label": "I am not sure",
                "detail": "The toolkit keeps this restricted until a person confirms it.",
            },
        ],
    },
    "organization_scale": {
        "question": "Which description fits the organization best?",
        "options": [
            {
                "id": "volunteer_run",
                "label": "Volunteer-run, no paid technical staff",
                "detail": "Work depends on volunteers and shared time.",
            },
            {
                "id": "small_organization",
                "label": "Small paid staff, no internal technical support",
                "detail": "Staff carry software work alongside program work.",
            },
            {
                "id": "internal_technical_staff",
                "label": "Internal technical or operations support",
                "detail": "Someone is responsible for systems, security, or procurement.",
            },
        ],
    },
}


def _dimension(
    identifier: str,
    label: str,
    question: str,
    *,
    required: bool = True,
    classification: str | None = None,
    owner_function: str | None = None,
    role: str | None = None,
) -> dict:
    return {
        "id": identifier,
        "label": label,
        "question": question,
        "required": required,
        "classification": classification,
        "owner_function": owner_function,
        "role": role,
    }


STAGE_DEFINITIONS: dict[str, dict] = {
    "entry": {
        "id": "entry",
        "label": STAGE_LABELS["entry"],
        "purpose": (
            "Gather only enough to choose the first route: what is proposed, how the "
            "work happens now, who is affected, and what worries the organization."
        ),
        "dimensions": [
            _dimension(
                "proposed_use",
                "Proposed use",
                "In your own words, what would this tool or system do?",
            ),
            _dimension(
                "current_workflow",
                "Current workflow",
                "How is this handled now, or what problem made you consider it?",
            ),
            _dimension(
                "affected_people",
                "People affected",
                "Who would come into contact with this, inside or outside the organization?",
            ),
            _dimension(
                "immediate_concern",
                "Immediate concern",
                "What concerns you most about this proposal right now?",
            ),
            _dimension(
                "use_pattern",
                "Kind of use",
                "Which description fits the proposal best?",
                classification="use_pattern",
            ),
        ],
        "branch_rules": [],
        "completion_rules": {"required": "all_required_dimensions"},
        "question_sources": ["dimension_seed", "prior_reply", "classification"],
    },
    "redline": {
        "id": "redline",
        "label": STAGE_LABELS["redline"],
        "purpose": (
            "Branch quickly around the proposed data and the people affected, and make "
            "prohibited or unmet conditions visible as blockers."
        ),
        "dimensions": [
            _dimension(
                "data_categories",
                "Data categories",
                "Which categories of information could this use touch?",
                classification="data_class",
            ),
            _dimension(
                "consent_ownership",
                "Consent and ownership",
                "Who holds authority over that information, and what consent already covers this use?",
            ),
            _dimension(
                "human_authority",
                "Human decision authority",
                "Which decisions must stay with a person rather than the system?",
            ),
            _dimension(
                "equity_access",
                "Equity and access",
                "Who could be served less well by this than by the current practice?",
            ),
            _dimension(
                "audit_recourse",
                "Audit and recourse",
                "How would someone question or correct an output they believe is wrong?",
            ),
            _dimension(
                "ability_to_stop",
                "Ability to suspend or stop",
                "Who could suspend this use, and how quickly?",
            ),
            _dimension(
                "participant_records",
                "Participant records",
                "Would staff copy any information from participant records into this tool?",
                required=False,
            ),
            _dimension(
                "language_access",
                "Language and access needs",
                "Which languages and access needs must this serve as well as the current practice does?",
                required=False,
            ),
            _dimension(
                "human_escalation",
                "Reaching a person",
                "How would someone reach a person instead of the system?",
                required=False,
            ),
            _dimension(
                "external_service_boundary",
                "External service boundary",
                "Which parts would run on an outside service, and what would leave the organization?",
                required=False,
            ),
        ],
        "branch_rules": [
            {
                "id": "sensitive_information_opens_participant_conditions",
                "when_tags": ["sensitive_data", "participant_facing"],
                "activate": [
                    "participant_records",
                    "language_access",
                    "human_escalation",
                ],
            },
            {
                "id": "outside_service_boundary",
                "when_tags": ["external_service", "existing_system_integration"],
                "activate": ["external_service_boundary"],
            },
            {
                "id": "public_material_closes_participant_records",
                "when_tags": ["public_data_only"],
                "skip": ["participant_records"],
                "skip_note": "The proposal uses material already approved for publication.",
            },
            {
                "id": "sensitive_data_in_an_external_service",
                "when_tags": ["prohibited_use"],
                "blocker": {
                    "title": "Sensitive information in an external AI service",
                    "detail": (
                        "Identifying participant, applicant, staff, or donor information "
                        "in an outside AI service is prohibited under the red line test. "
                        "The organization decides what happens next; the toolkit records "
                        "the condition."
                    ),
                    "dimension": "data_categories",
                },
                "stop_route": True,
            },
            {
                "id": "unclear_information_stays_restricted",
                "when_tags": ["data_class_unclear"],
                "blocker": {
                    "title": "Information class is unconfirmed",
                    "detail": (
                        "An unclear classification remains restricted until a person "
                        "inside the organization confirms it."
                    ),
                    "dimension": "data_categories",
                },
            },
        ],
        "completion_rules": {"required": "all_required_dimensions"},
        "question_sources": [
            "dimension_seed",
            "prior_reply",
            "classification",
            "branch_rule",
        ],
    },
    "stress": {
        "id": "stress",
        "label": STAGE_LABELS["stress"],
        "purpose": (
            "Build concrete scenarios from earlier replies instead of asking abstract "
            "failure questions, and record what would detect and correct each failure."
        ),
        "dimensions": [
            _dimension(
                "failure_mode",
                "Failure mode",
                "What is the most likely way this goes wrong in ordinary use?",
            ),
            _dimension(
                "detection",
                "Detection",
                "How would the organization notice that failure?",
            ),
            _dimension(
                "fallback",
                "Fallback",
                "What happens while the failure is being fixed?",
            ),
            _dimension(
                "correction",
                "Corrective action",
                "What would the organization do to correct the output and its effects?",
            ),
            _dimension(
                "affected_people",
                "People affected by the failure",
                "Who carries the consequence when that happens?",
            ),
            _dimension(
                "owner",
                "Owner of the response",
                "Who would be responsible for responding?",
                owner_function="incident_response",
            ),
        ],
        "branch_rules": [
            {
                "id": "no_detection_path",
                "when_tags": ["no_human_review"],
                "blocker": {
                    "title": "No one reviews the output before it is used",
                    "detail": "The stress test has no detection path for this failure.",
                    "dimension": "detection",
                },
            }
        ],
        "completion_rules": {"required": "all_required_dimensions"},
        "question_sources": ["scenario_from_prior_replies", "dimension_seed"],
    },
    "cost_benefit": {
        "id": "cost_benefit",
        "label": STAGE_LABELS["cost_benefit"],
        "purpose": (
            "Respond to organizational scale and capacity, and accumulate a resource "
            "view of what is known and unresolved."
        ),
        "dimensions": [
            _dimension("benefit", "Benefit", "Who benefits from this, and how?"),
            _dimension(
                "affected_labor",
                "Affected labor",
                "Whose work changes, and in what direction?",
            ),
            _dimension(
                "direct_cost",
                "Direct cost",
                "What would this cost to start, and who approves that spending?",
            ),
            _dimension(
                "maintenance",
                "Maintenance",
                "Who keeps it working once the first version is running?",
            ),
            _dimension(
                "vendor_dependence",
                "Vendor dependence",
                "What happens if the vendor changes the service, the price, or the terms?",
            ),
            _dimension(
                "non_ai_alternative",
                "Non-AI alternative",
                "What would a version of this without AI cost and require?",
            ),
            _dimension(
                "measurement",
                "Measurement",
                "How would the organization know whether this was worth it?",
            ),
            _dimension(
                "organization_scale",
                "Organizational capacity",
                "Which description fits the organization best?",
                classification="organization_scale",
            ),
            _dimension(
                "staff_time",
                "Staff time",
                "Whose hours would this take, and what would they stop doing?",
                required=False,
            ),
            _dimension(
                "training_support",
                "Training and support",
                "Who trains people to use it, and who answers questions afterward?",
                required=False,
            ),
            _dimension(
                "procurement",
                "Procurement",
                "Which procurement or contracting steps would this need?",
                required=False,
            ),
            _dimension(
                "integration",
                "Integration",
                "Which existing systems would this need to connect to?",
                required=False,
            ),
            _dimension(
                "security_review",
                "Security review",
                "Which security or privacy review would this need to pass?",
                required=False,
            ),
            _dimension(
                "cross_department_ownership",
                "Cross-department ownership",
                "Which departments share responsibility for this once it runs?",
                required=False,
            ),
        ],
        "branch_rules": [
            {
                "id": "small_organization_capacity_branch",
                "when_tags": ["small_organization", "volunteer_run", "capacity_gap"],
                "activate": ["staff_time", "training_support"],
                "skip": ["procurement", "cross_department_ownership"],
                "skip_note": "The organization does not run a separate procurement process.",
            },
            {
                "id": "technical_support_branch",
                "when_tags": ["internal_technical_staff", "existing_system_integration"],
                "activate": [
                    "procurement",
                    "integration",
                    "security_review",
                    "cross_department_ownership",
                ],
            },
        ],
        "completion_rules": {"required": "all_required_dimensions"},
        "question_sources": ["dimension_seed", "prior_reply", "classification"],
    },
    "hidden_curriculum": {
        "id": "hidden_curriculum",
        "label": STAGE_LABELS["hidden_curriculum"],
        "purpose": (
            "Branch by who interacts with the system, and offer consequences inferred "
            "from earlier replies rather than abstract prompts."
        ),
        "dimensions": [
            _dimension(
                "embedded_values",
                "Embedded values",
                "What would this system treat as normal or correct?",
            ),
            _dimension(
                "authority_shift",
                "Shifts in authority",
                "Whose judgment carries less weight once this runs?",
            ),
            _dimension(
                "invisible_work",
                "Invisible work",
                "Who would quietly correct or clean up after it?",
            ),
            _dimension(
                "workload_redistribution",
                "Workload redistribution",
                "Whose workload grows, and whose shrinks?",
                required=False,
            ),
            _dimension(
                "surveillance",
                "Surveillance",
                "What would this let the organization see about staff work?",
                required=False,
            ),
            _dimension(
                "deskilling",
                "Deskilling",
                "Which skill would people practice less often?",
                required=False,
            ),
            _dimension(
                "knowledge_authority",
                "Whose knowledge becomes authoritative",
                "Whose account of the work would this present as the organization's answer?",
                required=False,
            ),
            _dimension(
                "representation",
                "Representation",
                "Who is most likely to be described poorly or left out?",
                required=False,
            ),
            _dimension(
                "language_disability_access",
                "Language and disability access",
                "Which access needs would this serve worse than the current practice?",
                required=False,
            ),
            _dimension(
                "institutional_authority",
                "Perceived institutional authority",
                "Would people read its answers as the organization speaking?",
                required=False,
            ),
            _dimension(
                "human_escalation",
                "Reaching a person",
                "How would someone reach a person instead of the system?",
                required=False,
            ),
        ],
        "branch_rules": [
            {
                "id": "internal_tool_branch",
                "when_tags": ["internal_staff_tool"],
                "activate": ["workload_redistribution", "surveillance", "deskilling"],
            },
            {
                "id": "participant_facing_branch",
                "when_tags": ["participant_facing", "decision_support"],
                "activate": [
                    "knowledge_authority",
                    "representation",
                    "language_disability_access",
                    "institutional_authority",
                    "human_escalation",
                ],
            },
        ],
        "completion_rules": {"required": "all_required_dimensions"},
        "question_sources": ["inferred_consequences", "prior_reply", "dimension_seed"],
    },
    "accountability": {
        "id": "accountability",
        "label": STAGE_LABELS["accountability"],
        "purpose": (
            "Convert abstract concerns into named organizational functions with an "
            "owner, a role, an unknown, or a person to consult."
        ),
        "dimensions": [
            _dimension(
                "source_material_owner",
                "Approving source material",
                "Who approves the material this system would draw on?",
                owner_function="approve_source_material",
            ),
            _dimension(
                "disputed_output_owner",
                "Reviewing disputed outputs",
                "Who reviews an output that someone disputes?",
                owner_function="review_disputed_output",
            ),
            _dimension(
                "suspension_owner",
                "Suspending after an incident",
                "Who can suspend this after an incident, without waiting for a meeting?",
                owner_function="suspend_after_incident",
            ),
            _dimension(
                "continued_use_owner",
                "Reviewing continued use",
                "Who reviews whether this should continue after six months?",
                owner_function="review_continued_use",
            ),
        ],
        "branch_rules": [
            {
                "id": "unassigned_owner",
                "when_tags": ["owner_unassigned"],
                "blocker": {
                    "title": "An accountability function has no owner",
                    "detail": "The function is recorded without a person or role.",
                },
            }
        ],
        "completion_rules": {"required": "all_required_dimensions"},
        "question_sources": ["organization_membership", "dimension_seed", "prior_reply"],
    },
    "internal_external_review": {
        "id": "internal_external_review",
        "label": STAGE_LABELS["internal_external_review"],
        "purpose": (
            "Route accumulated gaps to the people who can answer them. The stage "
            "completes when each review item has a response, an explicit deferral, or "
            "recorded dissent."
        ),
        "dimensions": [
            _dimension(
                "program_staff",
                "Program staff",
                "What do the staff who do this work every day need to say about it?",
                role="program_staff",
            ),
            _dimension(
                "participant_advisory",
                "Participants or an advisory group",
                "What would the people this serves need to be asked?",
                role="participant_advisory",
            ),
            _dimension(
                "operations",
                "Operations",
                "What do the people responsible for systems, budget, or contracts need to review?",
                role="operations",
            ),
            _dimension(
                "board_leadership",
                "Board or leadership",
                "What approval or notice does leadership need before this continues?",
                role="board_leadership",
            ),
        ],
        "branch_rules": [],
        "completion_rules": {"required": "all_required_dimensions"},
        "question_sources": ["accumulated_gaps", "organization_membership"],
    },
}


def stage_definition(stage: str) -> dict:
    if stage not in STAGE_DEFINITIONS:
        raise ValueError("Unknown review stage")
    return STAGE_DEFINITIONS[stage]


def dimensions_for(stage: str) -> list[dict]:
    return list(stage_definition(stage)["dimensions"])


def dimension_ids(stage: str) -> list[str]:
    return [item["id"] for item in dimensions_for(stage)]


def dimension_for(stage: str, dimension: str) -> dict | None:
    for item in dimensions_for(stage):
        if item["id"] == dimension:
            return item
    return None


def required_dimension_ids(stage: str) -> list[str]:
    return [item["id"] for item in dimensions_for(stage) if item["required"]]


def optional_dimension_ids(stage: str) -> list[str]:
    return [item["id"] for item in dimensions_for(stage) if not item["required"]]


def dimension_label(stage: str, dimension: str) -> str:
    found = dimension_for(stage, dimension)
    return found["label"] if found else dimension.replace("_", " ").capitalize()


def seed_question(stage: str, dimension: str) -> str:
    found = dimension_for(stage, dimension)
    return found["question"] if found else "What else should the record show here?"


def classification_for(stage: str, dimension: str) -> dict | None:
    found = dimension_for(stage, dimension)
    if not found or not found.get("classification"):
        return None
    return CLASSIFICATIONS.get(found["classification"])


def branch_rules(stage: str) -> list[dict]:
    return list(stage_definition(stage)["branch_rules"])


def completion_rules(stage: str) -> dict:
    return dict(stage_definition(stage)["completion_rules"])


DATA_BOUNDARY = (
    "Three data classes govern this review. Public means information already "
    "approved for publication. Restricted means internal documents, meeting notes, "
    "budgets, grant material, staff procedures, or community stories without "
    "explicit public consent. Sensitive means identifying participant, applicant, "
    "staff, donor, health, legal, financial, credential, or case information. "
    "Sensitive information in an external AI service is prohibited. An unclear "
    "classification stays restricted pending human review."
)

_SAFETY = (
    "Never request names, identifying details, raw records, confidential text, "
    "credentials, or document uploads. Ask about categories and current practice. "
    "Use plain language, sentence case, no praise, no recap, no lists, and no "
    "em-dash pivots."
)

# Compatibility for the original fixed-conversation clients. The dynamic API no
# longer uses answer counts, but the browser simulation and older integrations
# still use this shape to send enough replies for the model-free fallback to
# settle every required dimension. New clients should read ``/api/stages`` and
# the response's ``interface_state`` instead.
STAGE_SPECS = {
    stage: {
        "answers": len(required_dimension_ids(stage)) * 2,
        "questions": ", ".join(
            item["label"].lower() for item in dimensions_for(stage)
        ),
        "record": ", ".join(item["label"] for item in dimensions_for(stage)),
    }
    for stage in STAGE_ORDER
}


def stage_prompt(
    stage: str,
    organization_name: str,
    cumulative_record: str = "",
) -> str:
    """Build the legacy conversational prompt for existing integrations.

    The application routes new conversations through :func:`routing_prompt`.
    Keeping this bounded adapter avoids breaking the public module contract
    while callers move to the structured interface-state protocol.
    """
    if stage not in STAGE_SPECS:
        raise ValueError("Unknown review stage")
    spec = STAGE_SPECS[stage]
    boundary = (
        "\nUse three data classes: Public means approved public information. "
        "Restricted means internal documents, meeting notes, budgets, grant "
        "material, staff procedures, or community stories without explicit public "
        "consent. Sensitive means identifying participant, applicant, staff, donor, "
        "health, legal, financial, credential, or case information. Sensitive data "
        "in an external AI service is prohibited. An unclear classification remains "
        "restricted pending human review.\n"
        if stage == "redline"
        else ""
    )
    context = cumulative_record.strip() or "No earlier record has been completed."
    return (
        f"You guide {STAGE_LABELS[stage]}, a stage in the Nonprofit AI toolkit, "
        f"for {organization_name}. The organization owns every decision. Use only "
        "the supplied record and conversation. Treat organization statements as "
        f"context that still may need verification.{boundary}\n\n"
        "Read the full stage conversation. If more information is needed, ask "
        "exactly one short follow-up about the largest remaining unknown among "
        f"{spec['questions']}. Build on the last answer. Never request names, "
        "identifying details, raw records, confidential text, credentials, or "
        "document uploads. Ask for data categories and practices. Use no question "
        "number, praise, recap, list, or example.\n\n"
        "When the stage is ready, write a concise stage record using these "
        f"sentence-case areas: {spec['record']}. Use only supplied facts and write "
        "unknown for missing facts. Explain whether conditions appear settled, "
        "need resolution, or support ending this proposed use. The model drafts "
        "this route and the organization decides.\n\n"
        f"Cumulative record:\n{context}"
    )


ONBOARD = stage_prompt("entry", "the organization")
ESTIMATE = (
    "Use only the organization-supplied strategic fit conversation. Write a concise "
    "entry record with proposed use, mission connection, people affected, current "
    "practice, desired outcome, non-AI option, capacity, owner, reasons to stop, "
    "decisions made, and unknowns. End by naming the conditions the red line test "
    "must resolve. Use sentence case and plain language."
)


def redline_prompt(org: dict, context: str = "") -> str:
    """Compatibility wrapper for the former red-line-specific prompt."""
    name = (org.get("name") or "").strip() or "the organization"
    return stage_prompt("redline", name, context)


_ROUTING_SCHEMA = {
    "signals": {
        "facts": ["short established statement"],
        "uncertain": ["claim the organization is not sure about"],
        "constraints": ["stated limit"],
        "resources_available": ["resource the organization already has"],
        "resources_missing": ["resource the organization lacks"],
        "affected_people": ["group affected"],
        "owners": [
            {
                "function": "short function name",
                "holder": "role or person as the user described it",
                "status": "assigned | role_only | unknown",
            }
        ],
        "approvals": ["approval this would require"],
        "risks": ["risk or red line raised"],
        "open_questions": ["question the organization cannot answer yet"],
        "contradictions": [
            {"earlier": "earlier statement", "now": "current statement", "dimension": "dimension id"}
        ],
        "tags": ["tag from the allowed tag list"],
    },
    "coverage_updates": {"dimension id": "coverage status"},
    "next_action": {
        "interface_state": "one allowed interface state",
        "dimension": "dimension id this action addresses",
        "context_sentence": (
            "one plain sentence naming what the organization already said that moved "
            "the review here, or an empty string"
        ),
        "prompt": "one short question or instruction",
        "options": [{"id": "short_id", "label": "short label", "detail": "one clause"}],
        "statement": "extracted statement for confirm, or an empty string",
        "conflict": {"earlier": "", "now": ""},
        "target_role": "role id for delegate, or an empty string",
        "consequence": "what stays unresolved for record_unknown, or an empty string",
    },
}


def _bullets(items: Iterable[str], empty: str) -> str:
    values = [str(item).strip() for item in items if str(item).strip()]
    return "\n".join(f"- {value}" for value in values) if values else f"- {empty}"


def coverage_brief(stage: str, coverage: dict[str, str]) -> str:
    """Describe every live dimension and its status for the routing prompt."""
    lines = []
    for item in dimensions_for(stage):
        status = coverage.get(item["id"])
        if status is None:
            continue
        note = " (opened by an earlier reply)" if not item["required"] else ""
        lines.append(f"- {item['id']} ({item['label']}): {status}{note}")
    return "\n".join(lines) if lines else "- no dimension is open"


def working_record_brief(working_record: dict) -> str:
    """Render the cumulative record the router may cite."""
    facts = [
        f"[{item.get('id', 'fact')}] {item.get('text', '')}"
        for item in working_record.get("facts", [])
    ]
    return (
        "Confirmed facts\n"
        + _bullets(facts, "nothing confirmed yet")
        + "\n\nUnresolved points\n"
        + _bullets(working_record.get("open_questions", []), "none recorded")
        + "\n\nBlocking conditions\n"
        + _bullets(
            [item.get("title", "") for item in working_record.get("blockers", [])],
            "none recorded",
        )
        + "\n\nOwners\n"
        + _bullets(
            [
                f"{item.get('function', 'function')}: {item.get('holder', 'unknown')}"
                for item in working_record.get("owners", [])
            ],
            "none named",
        )
    )


def routing_prompt(
    stage: str,
    organization_name: str,
    *,
    working_record: dict,
    coverage: dict[str, str],
    allowed_states: Iterable[str],
    open_dimensions: Iterable[str],
    members: Iterable[dict] = (),
    opening: bool = False,
) -> str:
    """Build the routing prompt that returns one validated interface decision."""
    definition = stage_definition(stage)
    states = ", ".join(allowed_states)
    open_list = ", ".join(open_dimensions) or "none"
    member_lines = _bullets(
        [
            f"{item.get('display_name') or 'a colleague'} ({item.get('role', 'member')})"
            for item in members
        ],
        "no other members are recorded",
    )
    boundary = f"\n\n{DATA_BOUNDARY}" if stage == "redline" else ""
    task = (
        "This is the first turn of the stage. Open with the single most useful "
        "dimension for this organization."
        if opening
        else "Read the latest organization reply, extract its signals, then choose one "
        "next action."
    )
    return (
        f"You route {definition['label']}, one stage of the Nonprofit AI toolkit, for "
        f"{organization_name}. {definition['purpose']} The organization owns every "
        "decision about adoption. You route, clarify, connect, and organize; you never "
        "decide whether the organization adopts the proposed system."
        f"{boundary}\n\n"
        f"{_SAFETY}\n\n"
        f"{task}\n\n"
        "Dimensions and current coverage\n"
        f"{coverage_brief(stage, coverage)}\n\n"
        f"Dimensions still open: {open_list}\n"
        f"Allowed interface states this turn: {states}\n"
        f"Allowed coverage statuses: {', '.join(COVERAGE_STATUSES)}\n"
        f"Allowed tags: {', '.join(SIGNAL_TAGS)}\n"
        f"Allowed delegate roles: {', '.join(REVIEW_ROLES)}\n\n"
        "Organization members on this record\n"
        f"{member_lines}\n\n"
        "Cumulative working record\n"
        f"{working_record_brief(working_record)}\n\n"
        "Rules for the next action. Ask about one dimension only. Set "
        "context_sentence from something the organization already said, in plain "
        "language, with no identifiers. Use choose when two to five bounded options "
        "would be clearer than an open question. Use classify when a reply leaves a "
        "category unclear. Use confirm when a statement should be checked before the "
        "record keeps it. Use resolve_conflict when a reply contradicts an earlier "
        "one. Use delegate when the answer belongs to another role. Use record_unknown "
        "when the organization cannot answer yet and the consequence should stay "
        "visible. Use complete_stage only when every open dimension has a terminal "
        "status. Use stop_route only to preserve a prohibited or unmet condition; it "
        "records the reason and never ends the review by itself.\n\n"
        "Return one JSON object and no markdown, in this shape:\n"
        f"{json.dumps(_ROUTING_SCHEMA, ensure_ascii=False)}\n\n"
        "Include only fields that apply. Use empty lists and empty strings elsewhere. "
        "Every signal must come from the organization's own words."
    )


def _compact_evidence(evidence: Iterable[dict]) -> str:
    return json.dumps(list(evidence), ensure_ascii=False, separators=(",", ":"))


def synthesis_prompt(
    organization_name: str,
    evidence: Iterable[dict],
    stage_state: Iterable[dict] = (),
) -> str:
    """Ask for bounded JSON that can be validated before it is stored."""
    schema = {
        "summary": "short paragraph",
        "key_points": [
            {
                "title": "plain label",
                "detail": "supported statement",
                "evidence_ids": ["turn UUID"],
            }
        ],
        "analysis": {
            "context": ["supported point"],
            "constraints": ["supported point"],
            "affordances": ["supported point"],
            "existing_ai_infrastructure": ["supported point"],
            "targeted_use_patterns": [
                {
                    "pattern": (
                        "workflow | company_knowledge | general_purpose_chatbot | "
                        "public_information_guide"
                    ),
                    "fit": "supported fit assessment",
                    "evidence_ids": ["turn UUID"],
                }
            ],
            "current_conditions": ["supported point"],
            "decision_points": ["supported point"],
            "pathways": ["supported point"],
            "potentials": ["supported point"],
        },
        "open_questions": ["unresolved question"],
        "nodes": [
            {
                "label": "plain label",
                "kind": (
                    "context | constraint | affordance | infrastructure | use_pattern | "
                    "decision | pathway | potential | question"
                ),
                "detail": "supported explanation",
                "evidence_ids": ["turn UUID"],
            }
        ],
        "edges": [
            {
                "source_label": "exact node label",
                "target_label": "exact node label",
                "relation": "short verb phrase",
                "evidence_ids": ["turn UUID"],
            }
        ],
    }
    structured = list(stage_state)
    structured_block = (
        "\nStructured stage state, already validated by the server. Use it to decide "
        "which claims are established, blocked, delegated, or unresolved:\n"
        f"{json.dumps(structured, ensure_ascii=False, separators=(',', ':'))}\n"
        if structured
        else ""
    )
    return (
        "Synthesize a nonprofit's completed AI adoption review. Use only the evidence "
        "below. Separate established context from open questions. Analyze context, "
        "constraints, affordances, existing AI infrastructure, and targeted use "
        "patterns. Assess four use patterns when relevant: workflow support; "
        "company-knowledge discoverability, legibility, and interpretability; a "
        "general-purpose chatbot; and a public informational guide or website sidecar. "
        "Map current conditions, decision points, plausible pathways, potentials, and "
        "unresolved questions. Preserve the organization's ability to choose a non-AI "
        "path.\n\n"
        "Return one JSON object and no markdown. Follow this shape exactly:\n"
        f"{json.dumps(schema, ensure_ascii=False)}\n\n"
        "Every claim, node, and edge must cite one or more evidence_ids from the "
        "supplied evidence. Omit unsupported claims. Use sentence case and plain "
        "language. Do not infer policies, consent, capacity, approvals, "
        "infrastructure, or decisions.\n"
        f"{structured_block}\n"
        f"Organization: {organization_name}\nEvidence: {_compact_evidence(evidence)}"
    )
