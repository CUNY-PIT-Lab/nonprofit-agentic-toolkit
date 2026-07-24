"""Strict synthesis validation and deterministic evidence-preserving fallback."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any


KINDS = {
    "context",
    "constraint",
    "affordance",
    "infrastructure",
    "use_pattern",
    "decision",
    "pathway",
    "potential",
    "question",
}
PATTERN_LABELS = {
    "workflow": "Workflow support",
    "company_knowledge": "Company knowledge",
    "general_purpose_chatbot": "General-purpose chatbot",
    "public_information_guide": "Public information guide or sidecar",
}
ANALYSIS_KEYS = (
    "context",
    "constraints",
    "affordances",
    "existing_ai_infrastructure",
    "targeted_use_patterns",
    "current_conditions",
    "decision_points",
    "pathways",
    "potentials",
)


def stable_id(prefix: str, *parts: str) -> str:
    material = "\x1f".join(str(part).strip().casefold() for part in parts)
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()[:14]}"


def parse_json_object(text: str) -> dict:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Synthesis was not valid JSON")
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("Synthesis must be a JSON object")
    return value


def _clean_text(value: Any, limit: int = 1200) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _valid_evidence(value: Any, allowed: set[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item) for item in value if str(item) in allowed))[:20]


def validate_synthesis(value: dict, evidence: list[dict]) -> dict:
    """Validate claims and build a Cytoscape-friendly stable graph."""
    allowed = {str(item["id"]) for item in evidence}
    summary = _clean_text(value.get("summary"), 2400)
    if not summary:
        raise ValueError("Synthesis summary is missing")

    key_points = []
    for point in value.get("key_points", []):
        if not isinstance(point, dict):
            continue
        ids = _valid_evidence(point.get("evidence_ids"), allowed)
        title, detail = _clean_text(point.get("title"), 180), _clean_text(
            point.get("detail"), 900
        )
        if title and detail and ids:
            key_points.append({"title": title, "detail": detail, "evidence_ids": ids})

    analysis: dict[str, list] = {}
    raw_analysis = value.get("analysis")
    if not isinstance(raw_analysis, dict):
        raw_analysis = {}
    for key in ANALYSIS_KEYS:
        items = raw_analysis.get(key, [])
        if key == "targeted_use_patterns":
            cleaned_patterns = []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict) or item.get("pattern") not in PATTERN_LABELS:
                    continue
                ids = _valid_evidence(item.get("evidence_ids"), allowed)
                fit = _clean_text(item.get("fit"), 800)
                if ids and fit:
                    cleaned_patterns.append(
                        {"pattern": item["pattern"], "fit": fit, "evidence_ids": ids}
                    )
            analysis[key] = cleaned_patterns
        else:
            analysis[key] = [
                _clean_text(item, 800)
                for item in (items if isinstance(items, list) else [])
                if _clean_text(item, 800)
            ][:20]

    label_to_id: dict[str, str] = {}
    nodes = []
    for raw in value.get("nodes", []):
        if not isinstance(raw, dict):
            continue
        label = _clean_text(raw.get("label"), 160)
        detail = _clean_text(raw.get("detail"), 1000)
        kind = raw.get("kind")
        ids = _valid_evidence(raw.get("evidence_ids"), allowed)
        if not label or not detail or kind not in KINDS or not ids:
            continue
        node_id = stable_id("node", kind, label)
        label_to_id[label.casefold()] = node_id
        nodes.append(
            {
                "id": node_id,
                "label": label,
                "kind": kind,
                "detail": detail,
                "evidence_ids": ids,
            }
        )
    if not nodes:
        raise ValueError("Synthesis graph contains no supported nodes")

    edges = []
    for raw in value.get("edges", []):
        if not isinstance(raw, dict):
            continue
        source_label = _clean_text(raw.get("source_label"), 160)
        target_label = _clean_text(raw.get("target_label"), 160)
        source = label_to_id.get(source_label.casefold())
        target = label_to_id.get(target_label.casefold())
        relation = _clean_text(raw.get("relation"), 120)
        ids = _valid_evidence(raw.get("evidence_ids"), allowed)
        if source and target and source != target and relation and ids:
            edges.append(
                {
                    "id": stable_id("edge", source, target, relation),
                    "source": source,
                    "target": target,
                    "relation": relation,
                    "evidence_ids": ids,
                }
            )

    open_questions = [
        _clean_text(item, 500)
        for item in value.get("open_questions", [])
        if _clean_text(item, 500)
    ][:20]
    return {
        "summary": summary,
        "key_points": key_points,
        "analysis": analysis,
        "open_questions": open_questions,
        "graph": {"nodes": nodes, "edges": edges},
    }


def deterministic_fallback(organization_name: str, evidence: list[dict]) -> dict:
    """Return a useful map when model JSON is unavailable or fails validation."""
    user_evidence = [item for item in evidence if item.get("role") == "user"]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in user_evidence:
        grouped[item.get("stage", "entry")].append(item)

    root_id = stable_id("node", "context", organization_name)
    nodes = [
        {
            "id": root_id,
            "label": organization_name,
            "kind": "context",
            "detail": "Organization-supplied review evidence.",
            "evidence_ids": [str(item["id"]) for item in user_evidence[:20]],
        }
    ]
    edges = []
    stage_labels = {
        "entry": "Strategic fit",
        "redline": "Red line conditions",
        "stress": "Failure and recourse",
        "cost_benefit": "Costs and benefits",
        "hidden_curriculum": "Values and authority",
        "accountability": "Accountability",
        "internal_external_review": "Review and approval",
    }
    for stage, items in grouped.items():
        ids = [str(item["id"]) for item in items]
        label = stage_labels.get(stage, stage.replace("_", " ").capitalize())
        node_id = stable_id("node", "decision", label)
        detail = " ".join(_clean_text(item.get("content"), 340) for item in items)[:1000]
        nodes.append(
            {
                "id": node_id,
                "label": label,
                "kind": "decision",
                "detail": detail,
                "evidence_ids": ids,
            }
        )
        edges.append(
            {
                "id": stable_id("edge", root_id, node_id, "is reviewed through"),
                "source": root_id,
                "target": node_id,
                "relation": "is reviewed through",
                "evidence_ids": ids,
            }
        )

    all_text = " ".join(item.get("content", "") for item in user_evidence).casefold()
    pattern_terms = {
        "workflow": ("workflow", "process", "task", "automation", "intake"),
        "company_knowledge": (
            "knowledge",
            "document",
            "search",
            "find",
            "policy",
            "discover",
        ),
        "general_purpose_chatbot": ("chatbot", "assistant", "general purpose", "chat"),
        "public_information_guide": (
            "website",
            "public",
            "informational",
            "guide",
            "sidecar",
            "resource",
        ),
    }
    patterns = []
    for pattern, terms in pattern_terms.items():
        if any(term in all_text for term in terms):
            ids = [
                str(item["id"])
                for item in user_evidence
                if any(term in item.get("content", "").casefold() for term in terms)
            ]
            label = PATTERN_LABELS[pattern]
            node_id = stable_id("node", "use_pattern", label)
            nodes.append(
                {
                    "id": node_id,
                    "label": label,
                    "kind": "use_pattern",
                    "detail": "The organization mentioned language associated with this use pattern.",
                    "evidence_ids": ids,
                }
            )
            edges.append(
                {
                    "id": stable_id("edge", root_id, node_id, "may consider"),
                    "source": root_id,
                    "target": node_id,
                    "relation": "may consider",
                    "evidence_ids": ids,
                }
            )
            patterns.append(
                {
                    "pattern": pattern,
                    "fit": "The conversation provides a possible fit to review. The organization has not approved this pattern.",
                    "evidence_ids": ids,
                }
            )

    summary = (
        f"{organization_name}'s map organizes the review evidence by stage. "
        "It preserves the supplied conditions and leaves unresolved decisions open for human review."
    )
    return {
        "summary": summary,
        "key_points": [],
        "analysis": {
            "context": ["The map contains organization-supplied review responses."],
            "constraints": [],
            "affordances": [],
            "existing_ai_infrastructure": [],
            "targeted_use_patterns": patterns,
            "current_conditions": [],
            "decision_points": [node["label"] for node in nodes if node["kind"] == "decision"],
            "pathways": [],
            "potentials": [],
        },
        "open_questions": [
            "Which claims and pathways should the organization confirm, revise, or remove?"
        ],
        "graph": {"nodes": nodes, "edges": edges},
    }
