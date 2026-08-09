"""Protected HTTP surface for replayable conversation evaluation."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .evaluation import (
    AnnotationCategory,
    BUCKET_COLORS,
    EvaluationConflict,
    EvaluationError,
    EvaluationEventType,
    EvaluationNotEligible,
    STANDARD_BUCKETS,
    STANDARD_BUCKET_IDS,
)
from .evaluation_store import EvaluationStore


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BucketCreateBody(StrictBody):
    label: str = Field(min_length=1, max_length=40)
    color_key: str
    operation_id: str = Field(
        min_length=8,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,119}$",
    )

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Bucket label cannot be blank")
        return value

    @field_validator("color_key")
    @classmethod
    def registered_color(cls, value: str) -> str:
        if value not in BUCKET_COLORS:
            raise ValueError("Bucket color is not registered")
        return value


class MutationBody(StrictBody):
    operation_id: str = Field(
        min_length=8,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,119}$",
    )
    expected_version: int = Field(ge=0)
    expected_transcript_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlacementBody(MutationBody):
    bucket_id: str | None = Field(default=None, max_length=120)


class NoteBody(MutationBody):
    note: str = Field(default="", max_length=1_000)


class AnnotationBody(MutationBody):
    category: AnnotationCategory | None = None
    note: str = Field(default="", max_length=500)

    @field_validator("category", mode="before")
    @classmethod
    def blank_category_removes(cls, value):
        return None if value == "" else value


def _actor(auth_result: Any) -> Any:
    return auth_result[0] if isinstance(auth_result, (tuple, list)) else auth_result


def _actor_id(auth_result: Any) -> str:
    actor = _actor(auth_result)
    value = actor.get("id") if isinstance(actor, dict) else getattr(actor, "id", None)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(401, "Authenticated actor attribution is unavailable")
    return value


def _evaluation_payload(
    state: dict[str, Any], *, transcript_checksum: str | None = None
) -> dict[str, Any]:
    annotations = [
        {
            **annotation,
            "message_id": annotation.get("turn_id"),
        }
        for annotation in state.get("annotations", [])
    ]
    return {
        "evaluation_version": state.get("evaluation_version", 0),
        "transcript_checksum": (
            transcript_checksum
            if transcript_checksum is not None
            else state.get("transcript_checksum", "")
        ),
        "bucket_id": state.get("bucket_id"),
        "note": state.get("note"),
        "annotations": annotations,
        "event_hash": state.get("event_hash", ""),
    }


def _domain_error(error: EvaluationError) -> HTTPException:
    if isinstance(error, EvaluationNotEligible):
        return HTTPException(404, "Conversation not found")
    if isinstance(error, EvaluationConflict):
        detail: dict[str, Any] = {"message": str(error)}
        if error.current is not None:
            detail["current"] = _evaluation_payload(error.current)
        return HTTPException(409, detail)
    if "Persisted" in str(error) or "hash chain" in str(error):
        return HTTPException(500, "Evaluation history could not be verified")
    return HTTPException(422, str(error))


def create_evaluation_router(
    *,
    db_dependency: Callable[..., Any],
    auth_dependency: Callable[..., Any],
    require_csrf: Callable[[Request, Any], None],
    reviewer_organizations: Callable[[Any, str], list[Any]],
    conversation_access: Callable[[Any, str, str], Any],
    audit: Callable[..., None],
    store_factory: Callable[[], EvaluationStore],
    enabled: bool,
    min_inactive_seconds: int,
) -> APIRouter:
    router = APIRouter(prefix="/api/evaluation", tags=["evaluation"])

    def require_enabled() -> None:
        if not enabled:
            raise HTTPException(404, "Evaluation workspace is not enabled")

    def organizations(dbs: Any, actor_id: str) -> list[Any]:
        values = reviewer_organizations(dbs, actor_id)
        if not values:
            raise HTTPException(404, "Evaluation workspace not found")
        return values

    def snapshot_for(
        stage_state_id: str,
        dbs: Any,
        actor_id: str,
        store: EvaluationStore,
    ):
        # This is the sole record-level authorization and eligibility path used
        # by list, detail, and every mutation.
        conversation_access(dbs, actor_id, stage_state_id)
        try:
            return store.snapshot(
                stage_state_id,
                min_inactive_seconds=min_inactive_seconds,
            )
        except EvaluationError as error:
            raise _domain_error(error) from error

    def commit_audit(
        dbs: Any,
        *,
        actor_id: str,
        stage_state_id: str,
        event_type: str,
        event_id: str,
        evaluation_version: int,
    ) -> None:
        audit(
            dbs,
            f"evaluation.{event_type}",
            actor=actor_id,
            entity_type="stage_state",
            entity_id=stage_state_id,
            metadata={
                "evaluation_event_id": event_id,
                "evaluation_version": evaluation_version,
            },
        )
        dbs.commit()

    @router.get("/status")
    def status():
        return {
            "enabled": enabled,
            "ready": enabled,
            "available": enabled,
            "authentication_required": True,
            "content_collection": "canonical-guided-turns-only",
            "min_inactive_seconds": min_inactive_seconds,
        }

    @router.get("/organizations")
    def list_organizations(
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        require_enabled()
        actor_id = _actor_id(auth_result)
        values = organizations(dbs, actor_id)
        return {
            "organizations": [
                {"id": organization.id, "name": organization.name}
                for organization in values
            ]
        }

    @router.get("/buckets")
    def list_buckets(
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        require_enabled()
        actor_id = _actor_id(auth_result)
        organizations(dbs, actor_id)
        standard = [
            {**bucket, "sort_position": (index + 1) * 10}
            for index, bucket in enumerate(STANDARD_BUCKETS)
        ]
        return {"buckets": [*standard, *store_factory().custom_buckets(actor_id)]}

    @router.post("/buckets", status_code=201)
    def create_bucket(
        body: BucketCreateBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        require_enabled()
        actor_id = _actor_id(auth_result)
        organizations(dbs, actor_id)
        require_csrf(request, dbs)
        try:
            bucket, created = store_factory().create_bucket(
                reviewer_id=actor_id,
                operation_id=body.operation_id,
                label=body.label,
                color_key=body.color_key,
            )
        except EvaluationError as error:
            raise _domain_error(error) from error
        if created:
            audit(
                dbs,
                "evaluation.bucket_created",
                actor=actor_id,
                entity_type="evaluation_bucket",
                entity_id=bucket["id"],
                metadata={"color_key": bucket["color_key"]},
            )
            dbs.commit()
        return {"bucket": bucket, "idempotent_replay": not created}

    @router.get("/conversations")
    def list_conversations(
        organization_id: str | None = Query(default=None, max_length=36),
        limit: int = Query(default=100, ge=1, le=100),
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        require_enabled()
        actor_id = _actor_id(auth_result)
        allowed = organizations(dbs, actor_id)
        allowed_ids = [organization.id for organization in allowed]
        if organization_id is not None:
            if organization_id not in allowed_ids:
                raise HTTPException(404, "Organization not found")
            allowed_ids = [organization_id]
        store = store_factory()
        candidate_ids = store.candidate_stage_state_ids(
            allowed_ids, limit=min(500, limit * 5)
        )
        conversations: list[dict[str, Any]] = []
        for stage_state_id in candidate_ids:
            try:
                snapshot = snapshot_for(stage_state_id, dbs, actor_id, store)
            except HTTPException as error:
                if error.status_code == 404:
                    continue
                raise
            try:
                evaluation = store.state(stage_state_id, actor_id)
            except EvaluationError as error:
                raise _domain_error(error) from error
            conversations.append(
                {
                    **snapshot.metadata(),
                    **_evaluation_payload(
                        evaluation,
                        transcript_checksum=snapshot.transcript_checksum,
                    ),
                }
            )
            # Metadata only: never include transcript, note, annotations, or
            # event history in this aggregate endpoint.
            conversations[-1].pop("note", None)
            conversations[-1].pop("annotations", None)
            conversations[-1].pop("event_hash", None)
            if len(conversations) >= limit:
                break
        return {"conversations": conversations}

    @router.get("/conversations/{stage_state_id}")
    def get_conversation(
        stage_state_id: str,
        as_of_evaluation_version: int | None = Query(default=None, ge=0),
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        require_enabled()
        actor_id = _actor_id(auth_result)
        store = store_factory()
        snapshot = snapshot_for(stage_state_id, dbs, actor_id, store)
        try:
            current = store.state(stage_state_id, actor_id)
            if (
                as_of_evaluation_version is not None
                and as_of_evaluation_version > current["evaluation_version"]
            ):
                raise EvaluationError("Requested evaluation version does not exist")
            evaluation = store.state(
                stage_state_id,
                actor_id,
                as_of_version=as_of_evaluation_version,
            )
        except EvaluationError as error:
            raise _domain_error(error) from error
        evaluated_transcript_checksum = evaluation.get("transcript_checksum") or None
        return {
            "conversation": {
                **snapshot.metadata(),
                **_evaluation_payload(
                    evaluation,
                    # Returned turns are always the current canonical turns.
                    # Bind the mutation token to those turns even when an older
                    # event was evaluated against an earlier transcript. Event
                    # history retains each event's original checksum.
                    transcript_checksum=snapshot.transcript_checksum,
                ),
                "evaluated_transcript_checksum": evaluated_transcript_checksum,
                "turns": [turn.as_dict() for turn in snapshot.turns],
                "history": evaluation.get("history", []),
            },
            "evaluation_version": evaluation["evaluation_version"],
            "transcript_checksum": snapshot.transcript_checksum,
            "evaluated_transcript_checksum": evaluated_transcript_checksum,
        }

    def apply_event(
        *,
        stage_state_id: str,
        body: MutationBody,
        request: Request,
        dbs: Any,
        auth_result: Any,
        event_type: EvaluationEventType,
        bucket_id: str | None = None,
        note: str | None = None,
        turn_id: str | None = None,
        annotation_category: AnnotationCategory | None = None,
    ) -> tuple[dict[str, Any], bool]:
        require_enabled()
        actor_id = _actor_id(auth_result)
        store = store_factory()
        snapshot = snapshot_for(stage_state_id, dbs, actor_id, store)
        require_csrf(request, dbs)
        if (
            event_type is EvaluationEventType.PLACEMENT_SET
            and bucket_id
            and bucket_id not in STANDARD_BUCKET_IDS
        ):
            try:
                store.validate_custom_bucket(actor_id, bucket_id)
            except EvaluationError as error:
                raise _domain_error(error) from error
        try:
            event, state, created = store.append_event(
                snapshot,
                reviewer_id=actor_id,
                event_type=event_type,
                operation_id=body.operation_id,
                expected_version=body.expected_version,
                expected_transcript_checksum=body.expected_transcript_checksum,
                bucket_id=bucket_id,
                note=note,
                turn_id=turn_id,
                annotation_category=annotation_category,
            )
        except EvaluationError as error:
            raise _domain_error(error) from error
        if created:
            commit_audit(
                dbs,
                actor_id=actor_id,
                stage_state_id=stage_state_id,
                event_type=event.event_type.value,
                event_id=event.event_id,
                evaluation_version=event.evaluation_version,
            )
        evaluation = _evaluation_payload(state)
        return {
            "evaluation": evaluation,
            "evaluation_version": evaluation["evaluation_version"],
            "transcript_checksum": evaluation["transcript_checksum"],
            "event_id": event.event_id,
            "idempotent_replay": not created,
        }, created

    @router.put("/conversations/{stage_state_id}/placement")
    def set_placement(
        stage_state_id: str,
        body: PlacementBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        response, _created = apply_event(
            stage_state_id=stage_state_id,
            body=body,
            request=request,
            dbs=dbs,
            auth_result=auth_result,
            event_type=EvaluationEventType.PLACEMENT_SET,
            bucket_id=body.bucket_id,
        )
        return response

    @router.put("/conversations/{stage_state_id}/note")
    def set_note(
        stage_state_id: str,
        body: NoteBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        response, _created = apply_event(
            stage_state_id=stage_state_id,
            body=body,
            request=request,
            dbs=dbs,
            auth_result=auth_result,
            event_type=EvaluationEventType.NOTE_SET,
            note=body.note.strip() or None,
        )
        return response

    @router.put("/conversations/{stage_state_id}/annotations/{turn_id}")
    def set_annotation(
        stage_state_id: str,
        turn_id: str,
        body: AnnotationBody,
        request: Request,
        dbs: Any = Depends(db_dependency),
        auth_result: Any = Depends(auth_dependency),
    ):
        note = body.note.strip() or None
        if body.category is None and note is not None:
            raise HTTPException(422, "Removed annotations cannot retain a note")
        response, _created = apply_event(
            stage_state_id=stage_state_id,
            body=body,
            request=request,
            dbs=dbs,
            auth_result=auth_result,
            event_type=(
                EvaluationEventType.ANNOTATION_SET
                if body.category is not None
                else EvaluationEventType.ANNOTATION_REMOVED
            ),
            note=note,
            turn_id=turn_id,
            annotation_category=body.category,
        )
        annotations = response["evaluation"]["annotations"]
        response["annotation"] = next(
            (
                annotation
                for annotation in annotations
                if annotation["turn_id"] == turn_id
            ),
            None,
        )
        return response

    return router


__all__ = [
    "AnnotationBody",
    "BucketCreateBody",
    "NoteBody",
    "PlacementBody",
    "create_evaluation_router",
]
