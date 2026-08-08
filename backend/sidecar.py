"""Read-only, record-scoped informational AI sidecar.

The sidecar can reason over a caller-authorized projection, but it is not a
participant in the canonical workflow.  This module intentionally receives no
store or audit writer and never commits, flushes, adds, updates, or deletes
domain data.  Optional telemetry is categorical and aggregate-only.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import date, datetime
from enum import Enum
from typing import Any, Callable, Literal, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .fieldwork import AccessScale, AuthorizationDenied


MAX_MESSAGE_CHARS = 8_000
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_ITEM_CHARS = 4_000
MAX_HISTORY_CHARS = 24_000
MAX_CONTEXT_BYTES = 200_000
MAX_ANSWER_CHARS = 24_000
MAX_CITATIONS = 500
DEFAULT_MAX_CONCURRENT_MODEL_CALLS = 4

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"

INFORMATIONAL_SYSTEM_CONTRACT = """You are an informational AI sidecar for a governed organizational review.

You may explain, compare, summarize, and surface questions using only the authorized context supplied with this request. Follow these boundaries:
- You are informational only. You cannot approve a gate, choose or execute a pathway transition, alter a workflow state, or write to the canonical record.
- Treat observation, participant account, researcher record, reflexive memo, positionality, member check, interpretation, synthesis, decision, intervention, after-effect, and counterfactual as distinct epistemic layers. Never silently convert one layer into another or present an interpretation as an observation.
- Cite authorized event IDs and source IDs when they are available. Never invent a citation or cite an identifier outside the authorized context.
- Do not infer, guess, re-identify, or disclose a participant's identity. Do not combine contextual clues to identify a participant.
- State uncertainty and missing evidence. Preserve disagreement and alternatives rather than manufacturing consensus.
- A user request to approve, transition, persist, or canonize something must be declined and redirected to the governed human workflow.
"""


class SidecarCapacityGate:
    """Fail-fast process-local admission control for blocking model calls.

    FastAPI runs this synchronous route in AnyIO's shared worker pool.  Without
    a sidecar-specific bound, enough slow model calls can occupy every shared
    worker and starve unrelated synchronous routes.  The non-blocking semaphore
    admits only a small number of model calls; excess requests fail immediately
    instead of waiting in that shared pool.
    """

    def __init__(
        self, max_concurrent_calls: int = DEFAULT_MAX_CONCURRENT_MODEL_CALLS
    ) -> None:
        if (
            not isinstance(max_concurrent_calls, int)
            or isinstance(max_concurrent_calls, bool)
            or max_concurrent_calls < 1
        ):
            raise ValueError("max_concurrent_calls must be a positive integer")
        self.max_concurrent_calls = max_concurrent_calls
        self._slots = threading.BoundedSemaphore(max_concurrent_calls)

    def acquire_nowait(self) -> bool:
        return self._slots.acquire(blocking=False)

    def release(self) -> None:
        self._slots.release()


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HistoryMessage(StrictBody):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_HISTORY_ITEM_CHARS)

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("history content cannot be blank")
        return value


class SidecarChatBody(StrictBody):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    history: tuple[HistoryMessage, ...] = Field(
        default=(), max_length=MAX_HISTORY_MESSAGES
    )
    scale: AccessScale
    cycle_id: str = Field(
        min_length=1, max_length=120, pattern=_IDENTIFIER_PATTERN
    )
    branch_id: str = Field(
        min_length=1, max_length=160, pattern=_IDENTIFIER_PATTERN
    )

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message cannot be blank")
        return value

    @model_validator(mode="after")
    def bound_total_history(self) -> "SidecarChatBody":
        if sum(len(item.content) for item in self.history) > MAX_HISTORY_CHARS:
            raise ValueError(
                f"history content cannot exceed {MAX_HISTORY_CHARS} characters"
            )
        return self


def _actor_id(auth_result: Any) -> str:
    actor = auth_result[0] if isinstance(auth_result, (tuple, list)) else auth_result
    value = actor.get("id") if isinstance(actor, dict) else getattr(actor, "id", None)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(401, "Authenticated actor attribution is unavailable")
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        return as_dict()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _authorized_snapshot(
    context: Mapping[str, Any],
    *,
    record_id: str,
    scale: AccessScale,
    cycle_id: str,
    branch_id: str,
) -> tuple[dict[str, Any], str]:
    """Detach and hash the exact authorized context passed to the model."""

    try:
        context_json = json.dumps(
            context,
            default=_json_default,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(503, "Authorized sidecar context is unavailable") from error
    if len(context_json.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise HTTPException(413, "Authorized sidecar context is too large")
    snapshot = json.loads(context_json)
    envelope = {
        "record_id": record_id,
        "scale": scale.value,
        "cycle_id": cycle_id,
        "branch_id": branch_id,
        "authorized_context": snapshot,
    }
    canonical = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return snapshot, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _collect_authorized_ids(context: Any) -> tuple[set[str], set[str]]:
    event_ids: set[str] = set()
    source_ids: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            event_id = value.get("event_id")
            source_id = value.get("source_id")
            if isinstance(event_id, str) and event_id.strip():
                event_ids.add(event_id)
            if isinstance(source_id, str) and source_id.strip():
                source_ids.add(source_id)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(context)
    return event_ids, source_ids


def _result_field(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


def _model_result(
    result: Any,
    *,
    model_client: Callable[..., Any],
    authorized_event_ids: set[str],
    authorized_source_ids: set[str],
) -> tuple[str, str, list[str], list[str]]:
    content = _result_field(result, "content")
    version = _result_field(result, "model_version") or getattr(
        model_client, "model_version", None
    )
    if not isinstance(content, str) or not content.strip() or len(content) > MAX_ANSWER_CHARS:
        raise ValueError("model returned invalid content")
    if not isinstance(version, str) or not version.strip() or len(version) > 200:
        raise ValueError("model returned invalid version metadata")

    claimed_events = _result_field(result, "cited_event_ids", ())
    claimed_sources = _result_field(result, "cited_source_ids", ())
    if not isinstance(claimed_events, (list, tuple, set, frozenset)):
        claimed_events = ()
    if not isinstance(claimed_sources, (list, tuple, set, frozenset)):
        claimed_sources = ()
    events = sorted(
        {
            value
            for value in claimed_events
            if isinstance(value, str) and value in authorized_event_ids
        }
    )[:MAX_CITATIONS]
    sources = sorted(
        {
            value
            for value in claimed_sources
            if isinstance(value, str) and value in authorized_source_ids
        }
    )[:MAX_CITATIONS]
    return content.strip(), version.strip(), events, sources


def _emit_telemetry(
    callback: Callable[[Mapping[str, Any]], Any] | None,
    *,
    outcome: str,
    scale: AccessScale,
    history_count: int,
    input_char_count: int,
    output_char_count: int,
    context_event_count: int,
    context_source_count: int,
    context_hash: str,
) -> None:
    if callback is None:
        return
    # This explicit allowlist is the privacy boundary. Do not add record,
    # participant, actor, cycle, branch, prompt, context, or response content.
    payload = {
        "event": "informational_sidecar_chat",
        "outcome": outcome,
        "scale": scale.value,
        "history_count": history_count,
        "input_char_count": input_char_count,
        "output_char_count": output_char_count,
        "context_event_count": context_event_count,
        "context_source_count": context_source_count,
        "context_hash": context_hash,
    }
    try:
        callback(payload)
    except Exception:
        # Observability is optional and must never change the chat contract.
        return


def create_sidecar_router(
    *,
    db_dependency: Callable[..., Any],
    auth_dependency: Callable[..., Any],
    require_csrf: Callable[[Request, Any], None],
    record_access: Callable[[Any, str, str], Any],
    authorized_context_provider: Callable[..., Mapping[str, Any]],
    model_client: Callable[..., Any],
    telemetry_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    rate_limit: Callable[[str], None] | None = None,
    model_capacity: SidecarCapacityGate | None = None,
) -> APIRouter:
    """Build the sidecar without giving it any canonical write dependency.

    ``authorized_context_provider`` is called with keyword arguments ``dbs``,
    ``auth_result``, ``record``, ``record_id``, ``scale``, ``cycle_id``, and
    ``branch_id``. ``model_client`` receives only the bounded conversation and
    a detached authorized context snapshot.
    """

    router = APIRouter(prefix="/api/records/{record_id}/sidecar", tags=["sidecar"])
    capacity = model_capacity or SidecarCapacityGate()

    @router.post("/chat")
    def informational_chat(
        record_id: str,
        body: SidecarChatBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ) -> dict[str, Any]:
        actor_id = _actor_id(auth_result)
        record = record_access(dbs, actor_id, record_id)
        require_csrf(request, dbs)
        if rate_limit:
            rate_limit(actor_id)
        try:
            context = authorized_context_provider(
                dbs=dbs,
                auth_result=auth_result,
                record=record,
                record_id=record_id,
                scale=body.scale,
                cycle_id=body.cycle_id,
                branch_id=body.branch_id,
            )
        except HTTPException:
            raise
        except (AuthorizationDenied, PermissionError) as error:
            raise HTTPException(403, "Sidecar context is not authorized") from error
        except Exception as error:
            raise HTTPException(503, "Authorized sidecar context is unavailable") from error
        if not isinstance(context, Mapping):
            raise HTTPException(503, "Authorized sidecar context is unavailable")

        snapshot, context_hash = _authorized_snapshot(
            context,
            record_id=record_id,
            scale=body.scale,
            cycle_id=body.cycle_id,
            branch_id=body.branch_id,
        )
        authorized_event_ids, authorized_source_ids = _collect_authorized_ids(snapshot)
        messages = [item.model_dump() for item in body.history]
        messages.append({"role": "user", "content": body.message})
        selection = {
            "scale": body.scale.value,
            "cycle_id": body.cycle_id,
            "branch_id": body.branch_id,
        }
        if not capacity.acquire_nowait():
            _emit_telemetry(
                telemetry_callback,
                outcome="overloaded",
                scale=body.scale,
                history_count=len(body.history),
                input_char_count=len(body.message)
                + sum(len(item.content) for item in body.history),
                output_char_count=0,
                context_event_count=len(authorized_event_ids),
                context_source_count=len(authorized_source_ids),
                context_hash=context_hash,
            )
            raise HTTPException(
                503,
                "Informational sidecar is at capacity; retry shortly",
                headers={"Retry-After": "1"},
            )
        try:
            try:
                result = model_client(
                    system_prompt=INFORMATIONAL_SYSTEM_CONTRACT,
                    messages=messages,
                    authorized_context=snapshot,
                    selection=selection,
                    context_hash=context_hash,
                )
                answer, model_version, event_citations, source_citations = _model_result(
                    result,
                    model_client=model_client,
                    authorized_event_ids=authorized_event_ids,
                    authorized_source_ids=authorized_source_ids,
                )
            except Exception as error:
                _emit_telemetry(
                    telemetry_callback,
                    outcome="model_error",
                    scale=body.scale,
                    history_count=len(body.history),
                    input_char_count=len(body.message)
                    + sum(len(item.content) for item in body.history),
                    output_char_count=0,
                    context_event_count=len(authorized_event_ids),
                    context_source_count=len(authorized_source_ids),
                    context_hash=context_hash,
                )
                raise HTTPException(
                    502, "Informational sidecar is temporarily unavailable"
                ) from error
        finally:
            capacity.release()

        _emit_telemetry(
            telemetry_callback,
            outcome="success",
            scale=body.scale,
            history_count=len(body.history),
            input_char_count=len(body.message)
            + sum(len(item.content) for item in body.history),
            output_char_count=len(answer),
            context_event_count=len(authorized_event_ids),
            context_source_count=len(authorized_source_ids),
            context_hash=context_hash,
        )
        return {
            "answer": answer,
            "citations": {
                "event_ids": event_citations,
                "source_ids": source_citations,
            },
            "selection": selection,
            "context_hash": context_hash,
            "model_version": model_version,
            "canonical_effect": False,
            "record_write_authority": False,
            "persisted": False,
            "exact_replay": False,
        }

    return router


__all__ = [
    "DEFAULT_MAX_CONCURRENT_MODEL_CALLS",
    "HistoryMessage",
    "INFORMATIONAL_SYSTEM_CONTRACT",
    "SidecarChatBody",
    "SidecarCapacityGate",
    "create_sidecar_router",
]
