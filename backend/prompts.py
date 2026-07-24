"""Server-owned prompts and model-output cleanup."""

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
    "entry": "Strategic fit",
    "redline": "Red line test",
    "stress": "Stress test",
    "cost_benefit": "Cost-benefit",
    "hidden_curriculum": "Hidden curriculum",
    "accountability": "Accountability",
    "internal_external_review": "Internal and external review",
}

STAGE_SPECS = {
    "entry": {
        "answers": 4,
        "questions": (
            "mission connection, underlying need and current practice, people affected, a useful "
            "outcome, a non-AI option, staff capacity, accountable owner, or reasons to stop"
        ),
        "record": (
            "Proposed use, Strategic fit, People affected, Current practice, Desired outcome, "
            "Readiness, Accountable owner, Reasons to stop, Unknowns"
        ),
    },
    "redline": {
        "answers": 5,
        "questions": (
            "data categories, ownership, consent, storage, access, privacy policy, permitted "
            "environment, human decision authority, equitable access, discriminatory effects, "
            "audit, correction, recourse, intellectual-property ownership, staff capacity, or "
            "the ability to stop the work"
        ),
        "record": (
            "Proposed use, Data boundary, Human authority, Equity and access, Audit and recourse, "
            "Ownership and capacity, Unmet conditions, Decision owners, Unknowns"
        ),
    },
    "stress": {
        "answers": 4,
        "questions": (
            "likely failure, unsupported output, security, reliability, accessibility, correction, "
            "recourse, monitoring, or a safe fallback"
        ),
        "record": (
            "Failure conditions, Unsupported output, Security and reliability, Accessibility, "
            "Correction and recourse, Safe fallback, Owners, Unknowns"
        ),
    },
    "cost_benefit": {
        "answers": 4,
        "questions": (
            "who benefits, whose labor changes, risks, direct and ongoing costs, maintenance, "
            "vendor dependence, non-AI alternatives, or how value would be measured"
        ),
        "record": (
            "Expected benefit, People and labor, Risks, Costs and maintenance, Non-AI comparison, "
            "Measures, Owners, Unknowns"
        ),
    },
    "hidden_curriculum": {
        "answers": 4,
        "questions": (
            "values built into the system, behavior it rewards, shifts in authority, excluded "
            "knowledge, invisible work, deskilling, dependence, or effects on staff and participants"
        ),
        "record": (
            "Embedded values, Behavior and authority, Excluded knowledge, Invisible work, "
            "Dependence, Mitigations, Owners, Unknowns"
        ),
    },
    "accountability": {
        "answers": 4,
        "questions": (
            "a responsible owner, explanation, audit, appeal, incident handling, suspension, "
            "review schedule, change control, or retirement"
        ),
        "record": (
            "Responsible owners, Explanation, Audit and appeal, Incidents and suspension, "
            "Review and retirement, Evidence, Unknowns"
        ),
    },
    "internal_external_review": {
        "answers": 4,
        "questions": (
            "affected staff and participants, existing advisory or governance groups, internal "
            "approval, external review, negotiated conditions, dissent, or the final decision process"
        ),
        "record": (
            "People consulted, Internal review, External review, Negotiated conditions, "
            "Dissent and recourse, Approval owners, Open decisions, Unknowns"
        ),
    },
}


def stage_prompt(
    stage: str,
    organization_name: str,
    cumulative_record: str = "",
) -> str:
    """Build one server-owned, record-scoped review prompt."""
    if stage not in STAGE_SPECS:
        raise ValueError("Unknown review stage")
    spec = STAGE_SPECS[stage]
    label = STAGE_LABELS[stage]
    data_boundary = ""
    if stage == "redline":
        data_boundary = (
            "\nUse three data classes: Public means approved public information. Restricted means "
            "internal documents, meeting notes, budgets, grant material, staff procedures, or "
            "community stories without explicit public consent. Sensitive means identifying "
            "participant, applicant, staff, donor, health, legal, financial, credential, or case "
            "information. Sensitive data in an external AI service is prohibited. An unclear "
            "classification remains restricted pending human review.\n"
        )
    context = cumulative_record.strip() or "No earlier record has been completed."
    return (
        f"You guide {label}, a stage in the Nonprofit AI toolkit, for {organization_name}. "
        "The organization owns every decision. Use only the supplied record and conversation. "
        "Treat organization statements as context that still may need verification."
        f"{data_boundary}\n\n"
        "Read the full stage conversation. Count user answers, excluding assistant messages. "
        f"If the user has answered fewer than {spec['answers']} questions, ask exactly one short "
        f"follow-up about the largest remaining unknown among {spec['questions']}. Build on the "
        "last answer. Never request names, identifying details, raw records, confidential text, "
        "credentials, or document uploads. Ask for data categories and practices. Reply with at "
        "most one short context sentence and exactly one question. Use no question number, praise, "
        "recap, list, or example. The reply may contain at most one question mark.\n\n"
        f"After {spec['answers']} user answers, stop asking. Write a concise stage record with these "
        f"sentence-case labels: {spec['record']}. Use only supplied facts and write unknown for "
        "missing facts. Close with a short section called Draft route that explains whether the "
        "organization appears ready to continue, needs conditions resolved, or should end this "
        "proposed use. Avoid outcome tokens. The model drafts this route and the organization "
        "decides. Use plain language, no slogans, and no em-dash pivots.\n\n"
        f"Cumulative record:\n{context}"
    )


ONBOARD = stage_prompt("entry", "the organization")
ESTIMATE = (
    "Use only the organization-supplied strategic fit conversation. Write a concise entry record "
    "with proposed use, mission connection, people affected, current practice, desired outcome, "
    "non-AI option, capacity, owner, reasons to stop, decisions made, and unknowns. End by naming "
    "the conditions the red line test must resolve. Use sentence case and plain language."
)


def redline_prompt(org: dict, context: str = "") -> str:
    name = (org.get("name") or "").strip() or "the organization"
    return stage_prompt("redline", name, context)


def _compact_evidence(evidence: Iterable[dict]) -> str:
    return json.dumps(list(evidence), ensure_ascii=False, separators=(",", ":"))


def synthesis_prompt(organization_name: str, evidence: Iterable[dict]) -> str:
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
    return (
        "Synthesize a nonprofit's completed AI adoption review. Use only the evidence below. "
        "Separate established context from open questions. Analyze context, constraints, "
        "affordances, existing AI infrastructure, and targeted use patterns. Assess four use "
        "patterns when relevant: workflow support; company-knowledge discoverability, legibility, "
        "and interpretability; a general-purpose chatbot; and a public informational guide or "
        "website sidecar. Map current conditions, decision points, plausible pathways, potentials, "
        "and unresolved questions. Preserve the organization's ability to choose a non-AI path.\n\n"
        "Return one JSON object and no markdown. Follow this shape exactly:\n"
        f"{json.dumps(schema, ensure_ascii=False)}\n\n"
        "Every claim, node, and edge must cite one or more evidence_ids from the supplied evidence. "
        "Omit unsupported claims. Use sentence case and plain language. Do not infer policies, "
        "consent, capacity, approvals, infrastructure, or decisions.\n\n"
        f"Organization: {organization_name}\nEvidence: {_compact_evidence(evidence)}"
    )
