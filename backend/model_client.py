"""Bounded Ollama Cloud client used only by record-scoped routes."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from .prompts import strip_reasoning


class ModelUnavailable(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def complete(self, system: str, messages: list[dict], *, json_mode: bool = False) -> str:
        if not self.api_key:
            raise ModelUnavailable("The model service is not configured")
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream": False,
            "think": False,
        }
        if json_mode:
            payload["format"] = "json"
        request = urllib.request.Request(
            "https://ollama.com/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            raise ModelUnavailable(f"Model service returned {exc.code}") from exc
        except Exception as exc:
            raise ModelUnavailable("Model service could not be reached") from exc
        content = strip_reasoning(data.get("message", {}).get("content") or "")
        if not content:
            raise ModelUnavailable("Model service returned an empty response")
        return content


class StubModelClient:
    """Deterministic, key-free model adapter for local browser QA."""

    model = "local-stub"

    def complete(self, system: str, messages: list[dict], *, json_mode: bool = False) -> str:
        if json_mode:
            match = re.search(r"\nEvidence:\s*(\[.*\])\s*$", system, re.DOTALL)
            evidence = json.loads(match.group(1)) if match else []
            usable = [item for item in evidence if item.get("role") == "user"]
            if not usable:
                raise ModelUnavailable("No user evidence is available")
            cited = [str(item["id"]) for item in usable]
            first = usable[0]
            label = "Organization review evidence"
            return json.dumps(
                {
                    "summary": (
                        "The completed review identifies current conditions, decisions, and "
                        "questions that the organization can confirm before choosing a path."
                    ),
                    "key_points": [
                        {
                            "title": "Review evidence",
                            "detail": "The organization supplied responses across the review.",
                            "evidence_ids": cited,
                        }
                    ],
                    "analysis": {
                        "context": ["The organization completed the guided review."],
                        "constraints": [],
                        "affordances": [],
                        "existing_ai_infrastructure": [],
                        "targeted_use_patterns": [],
                        "current_conditions": ["Review responses are available for human confirmation."],
                        "decision_points": ["Confirm the map and unresolved conditions."],
                        "pathways": ["Revise, continue, or end the proposed use after review."],
                        "potentials": [],
                    },
                    "open_questions": [
                        "Which conditions should be confirmed before a final decision?"
                    ],
                    "nodes": [
                        {
                            "label": label,
                            "kind": "context",
                            "detail": first.get("content", "Organization-supplied evidence."),
                            "evidence_ids": cited,
                        },
                        {
                            "label": "Human confirmation",
                            "kind": "decision",
                            "detail": "The organization reviews the evidence and chooses the next path.",
                            "evidence_ids": cited,
                        },
                    ],
                    "edges": [
                        {
                            "source_label": label,
                            "target_label": "Human confirmation",
                            "relation": "informs",
                            "evidence_ids": cited,
                        }
                    ],
                }
            )
        count = len([message for message in messages if message.get("role") == "user"])
        limit_match = re.search(r"fewer than (\d+)", system, re.IGNORECASE)
        answer_limit = int(limit_match.group(1)) if limit_match else 4
        if count < answer_limit:
            questions = (
                "What current condition matters most for this part of the review?",
                "Who would be affected, and what should remain under their control?",
                "Who can verify this condition and act if it fails?",
                "What remains unresolved before the organization continues?",
                "Which condition would make the organization stop this proposed use?",
            )
            return questions[min(count, len(questions) - 1)]
        answers = [
            message.get("content", "")
            for message in messages
            if message.get("role") == "user"
        ]
        return (
            "Stage record\n\n"
            + "\n".join(f"Response {index + 1}: {answer}" for index, answer in enumerate(answers))
            + "\n\nDraft route\nThe organization reviews these responses and decides whether to continue."
        )
