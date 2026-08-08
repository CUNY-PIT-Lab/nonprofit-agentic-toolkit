"""Consent and bounded feedback API for governed product evolution."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .evolution import (
    ConsentBasis,
    ConsentStatus,
    EvolutionError,
    ProposalType,
    TelemetryManifest,
    TelemetrySensitivity,
)
from .evolution_store import EvolutionStore
from .fieldwork import AccessScale
from .pathways import RouteOutcome, default_pathway
from .prompts import INTERFACE_STATES


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TelemetryConsentBody(StrictBody):
    enabled: bool


class FeedbackSignal(str, Enum):
    HELP_REQUESTED = "interface.help_requested"
    ROUTE_CONFUSING = "interface.route_confusing"
    NEGOTIATE_SELECTED = "pathway.negotiate_selected"
    NON_AI_SELECTED = "pathway.non_ai_selected"
    WALK_AWAY_SELECTED = "pathway.walk_away_selected"
    REPLAY_USED = "fieldwork.replay_used"
    FIELDWORK_LOOP_NAME = "name.preference.fieldwork_loop"
    CURRENT_NAME = "name.preference.current_toolkit"


SIGNAL_AREAS = {
    FeedbackSignal.HELP_REQUESTED: ProposalType.INTERFACE,
    FeedbackSignal.ROUTE_CONFUSING: ProposalType.INTERFACE,
    FeedbackSignal.NEGOTIATE_SELECTED: ProposalType.PATHWAY,
    FeedbackSignal.NON_AI_SELECTED: ProposalType.PATHWAY,
    FeedbackSignal.WALK_AWAY_SELECTED: ProposalType.PATHWAY,
    FeedbackSignal.REPLAY_USED: ProposalType.INTERFACE,
    FeedbackSignal.FIELDWORK_LOOP_NAME: ProposalType.NAME,
    FeedbackSignal.CURRENT_NAME: ProposalType.NAME,
}

ALLOWED_SIGNAL_DIMENSIONS = {
    "pathway_stage": frozenset(default_pathway().nodes),
    "interface_state": frozenset(INTERFACE_STATES),
    "route": frozenset(outcome.value for outcome in RouteOutcome),
    "scale": frozenset(scale.value for scale in AccessScale),
}


class ProductFeedbackBody(StrictBody):
    signal: FeedbackSignal
    idempotency_key: str = Field(
        min_length=8,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,119}$",
    )
    helpful: bool | None = None
    elapsed_ms: int | None = Field(default=None, ge=0, le=300_000)
    pathway_stage: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9._:-]{0,79}$"
    )
    interface_state: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9._:-]{0,79}$"
    )
    route: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9._:-]{0,79}$"
    )
    scale: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9._:-]{0,79}$"
    )

    @field_validator("pathway_stage", "interface_state", "route", "scale")
    @classmethod
    def allow_known_dimension_values(cls, value: str | None, info):
        if value is not None and value not in ALLOWED_SIGNAL_DIMENSIONS[info.field_name]:
            raise ValueError("Product-signal dimensions must use a registered category")
        return value


def _auth_actor(auth_result: Any) -> Any:
    return auth_result[0] if isinstance(auth_result, (tuple, list)) else auth_result


def _actor_id(auth_result: Any) -> str:
    actor = _auth_actor(auth_result)
    value = actor.get("id") if isinstance(actor, dict) else getattr(actor, "id", None)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(401, "Authenticated actor attribution is unavailable")
    return value


def _consent_scope_id(auth_result: Any) -> str:
    actor = _auth_actor(auth_result)
    value = (
        actor.get("telemetry_scope_id")
        if isinstance(actor, dict)
        else getattr(actor, "telemetry_scope_id", None)
    )
    if not isinstance(value, str) or not value.strip():
        # Never fall back to a request field, raw user id, or auth-secret hash.
        raise HTTPException(500, "Stable telemetry consent scope is unavailable")
    return value


def create_evolution_router(
    *,
    db_dependency: Callable[..., Any],
    auth_dependency: Callable[..., Any],
    require_csrf: Callable[[Request, Any], None],
    store_factory: Callable[[], EvolutionStore],
    telemetry_enabled: bool,
    cohort_key: str,
    app_version: Callable[[], str],
    default_identity_name: str,
    default_identity_version: str,
    rate_limit: Callable[[str, str], None] | None = None,
) -> APIRouter:
    """Expose opt-in and constrained signals, never raw analytics payloads."""

    router = APIRouter(prefix="/api/product-evolution", tags=["product-evolution"])

    @router.get("/identity")
    def get_identity():
        """Return only the currently rolled-out public display identity."""

        return store_factory().active_identity(
            default_name=default_identity_name,
            default_version=default_identity_version,
        )

    @router.get("/consent")
    def get_consent(
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        del dbs
        _actor_id(auth_result)
        status = store_factory().current_consent_status(
            _consent_scope_id(auth_result)
        )
        return {
            "collection_enabled": telemetry_enabled,
            "consent": status.value if status else "not_set",
            "content_collection": False,
            "research_participation_required": False,
        }

    @router.post("/consent")
    def set_consent(
        body: TelemetryConsentBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        actor_id = _actor_id(auth_result)
        scope_id = _consent_scope_id(auth_result)
        require_csrf(request, dbs)
        if rate_limit:
            rate_limit(actor_id, "consent")
        try:
            decision = store_factory().append_consent_decision(
                consent_scope_id=scope_id,
                status=(ConsentStatus.GRANTED if body.enabled else ConsentStatus.WITHDRAWN),
                actor_id=scope_id,
                actor_role="authenticated_user",
                reason_code="user-opt-in" if body.enabled else "user-opt-out",
                decided_at=datetime.now(timezone.utc),
            )
        except EvolutionError as error:
            raise HTTPException(422, str(error)) from error
        return {
            "consent": decision.status.value,
            "collection_enabled": telemetry_enabled,
            "content_collection": False,
        }

    @router.post("/signals", status_code=202)
    def append_signal(
        body: ProductFeedbackBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        actor_id = _actor_id(auth_result)
        require_csrf(request, dbs)
        if rate_limit:
            rate_limit(actor_id, "signal")
        if not telemetry_enabled:
            raise HTTPException(404, "Product evolution signals are not enabled")
        scope_id = _consent_scope_id(auth_result)
        event_id = "feedback." + hashlib.sha256(
            f"{scope_id}\x1f{body.signal.value}\x1f{body.idempotency_key}".encode("utf-8")
        ).hexdigest()
        metrics: dict[str, int | bool] = {}
        if body.helpful is not None:
            metrics["helpful"] = body.helpful
        if body.elapsed_ms is not None:
            metrics["elapsed_ms"] = body.elapsed_ms
        dimensions = {
            key: value
            for key, value in {
                "pathway_stage": body.pathway_stage,
                "interface_state": body.interface_state,
                "route": body.route,
                "scale": body.scale,
                "client_kind": "web",
            }.items()
            if value is not None
        }
        try:
            event = store_factory().append_product_event(
                event_type=body.signal.value,
                product_area=SIGNAL_AREAS[body.signal],
                cohort_key=cohort_key,
                metrics=metrics,
                dimensions=dimensions,
                manifest=TelemetryManifest(
                    consent_basis=ConsentBasis.GRANTED,
                    consent_scope_id=scope_id,
                    sensitivity=TelemetrySensitivity.INTERNAL,
                    allowed_purposes=("analytics", "evolution", "quality"),
                    app_version=app_version(),
                ),
                occurred_at=datetime.now(timezone.utc),
                event_id=event_id,
            )
        except EvolutionError as error:
            status = 403 if "consent" in str(error).lower() else 422
            raise HTTPException(status, str(error)) from error
        return {
            "accepted": True,
            "event_id": event.event_id,
            "event_hash": event.event_hash,
            "content_collected": False,
        }

    return router


__all__ = [
    "FeedbackSignal",
    "ProductFeedbackBody",
    "TelemetryConsentBody",
    "create_evolution_router",
]
