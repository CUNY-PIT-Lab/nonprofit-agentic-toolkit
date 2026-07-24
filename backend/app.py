"""FastAPI service for verified accounts and record-scoped toolkit work."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from .config import Settings
from .database import build_database, run_safe_migrations
from .mailer import (
    MemoryEmailBackend,
    ResendEmailBackend,
    reset_link,
    verification_link,
)
from .model_client import ModelUnavailable, OllamaClient, StubModelClient
from .models import (
    AdoptionRecord,
    Annotation,
    AuditEvent,
    CompletedStep,
    ConceptMap,
    ConversationTurn,
    EmailToken,
    KnowledgeSnippet,
    ModelRun,
    Organization,
    OrganizationMembership,
    Session,
    Synthesis,
    User,
    utcnow,
)
from .prompts import (
    STAGE_LABELS,
    STAGE_ORDER,
    STAGE_SPECS,
    stage_prompt,
    synthesis_prompt,
)
from .security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    RateLimiter,
    constant_equal,
    hash_password,
    is_expired,
    needs_password_rehash,
    normalize_email,
    opaque_token,
    token_hash,
    user_agent_hash,
    validate_password,
    verify_password,
)
from .synthesis import deterministic_fallback, parse_json_object, validate_synthesis


AUTH_GENERIC = {
    "message": "If the account can continue, an email will arrive with the next step."
}
FORGOT_GENERIC = {
    "message": "If an eligible account exists, an email will arrive with reset instructions."
}


class RegisterBody(BaseModel):
    email: str
    password: str
    display_name: str | None = Field(default=None, max_length=120)


class EmailBody(BaseModel):
    email: str


class LoginBody(BaseModel):
    email: str
    password: str


class TokenBody(BaseModel):
    token: str = Field(min_length=20, max_length=500)


class ResetBody(TokenBody):
    password: str


class RecordCreateBody(BaseModel):
    organization_id: str | None = None
    organization_name: str | None = Field(default=None, max_length=160)
    title: str = Field(min_length=1, max_length=180)
    proposed_use: str | None = Field(default=None, max_length=12000)


class RecordUpdateBody(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=180)
    proposed_use: str | None = Field(default=None, max_length=12000)
    status: str | None = None


class MessageBody(BaseModel):
    content: str = Field(min_length=1, max_length=12000)
    idempotency_key: str = Field(min_length=8, max_length=120)


class CompleteBody(BaseModel):
    record_text: str | None = Field(default=None, max_length=30000)


class AnnotationBody(BaseModel):
    concept_map_id: str | None = None
    target_type: str = Field(default="node", pattern="^(node|edge|map)$")
    target_id: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=5000)
    position: dict = Field(default_factory=dict)


class AnnotationUpdateBody(BaseModel):
    body: str | None = Field(default=None, min_length=1, max_length=5000)
    position: dict | None = None


class MemberBody(BaseModel):
    email: str
    role: str = Field(default="member", pattern="^(owner|member|reviewer)$")


def _safe_text(value: str | None) -> str:
    return " ".join((value or "").split())


def _serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "email_verified": bool(user.email_verified_at),
    }


def _serialize_turn(turn: ConversationTurn) -> dict:
    return {
        "id": turn.id,
        "stage": turn.stage,
        "role": turn.role,
        "content": turn.content,
        "ordinal": turn.ordinal,
        "created_at": turn.created_at.isoformat(),
    }


def _serialize_annotation(item: Annotation) -> dict:
    return {
        "id": item.id,
        "concept_map_id": item.concept_map_id,
        "target_type": item.target_type,
        "target_id": item.target_id,
        "body": item.body,
        "position": item.position,
        "created_by_id": item.created_by_id,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def create_app(
    settings: Settings | None = None,
    *,
    email_backend=None,
    model_client=None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    engine, session_factory = build_database(settings.database_url)
    run_safe_migrations(engine)
    limiter = RateLimiter()
    if email_backend is None:
        if settings.production:
            email_backend = (
                ResendEmailBackend(settings.resend_api_key, settings.email_from)
                if settings.email_ready
                else None
            )
        else:
            email_backend = MemoryEmailBackend()
    model_client = model_client or (
        StubModelClient()
        if settings.model_backend == "stub"
        else OllamaClient(settings.ollama_api_key, settings.toolkit_model)
    )
    app = FastAPI(
        title="Nonprofit AI toolkit",
        docs_url=None if settings.production else "/api/docs",
        redoc_url=None,
        openapi_url=None if settings.production else "/api/openapi.json",
    )
    app.state.settings = settings
    app.state.email_backend = email_backend
    app.state.model_client = model_client
    app.state.db_engine = engine
    app.state.db_session_factory = session_factory
    session_cookie_name = (
        "__Host-toolkit_session" if settings.production else SESSION_COOKIE
    )
    csrf_cookie_name = "__Host-toolkit_csrf" if settings.production else CSRF_COOKIE

    def db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        if settings.production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def request_key(request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        return forwarded or (request.client.host if request.client else "unknown")

    def rate_limit(
        request: Request, bucket: str, *, limit: int = 8, window: int = 900
    ) -> None:
        if not limiter.allow(bucket, request_key(request), limit, window):
            raise HTTPException(429, "Try again later")

    def rate_limit_identifier(
        bucket: str, identifier: str, *, limit: int, window: int
    ) -> None:
        key = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        if not limiter.allow(bucket, key, limit, window):
            raise HTTPException(429, "Try again later")

    def require_origin_csrf(request: Request, dbs: OrmSession | None = None) -> None:
        origin = (request.headers.get("Origin") or "").rstrip("/")
        if origin != settings.allowed_origin:
            raise HTTPException(403, "Request origin was rejected")
        cookie = request.cookies.get(csrf_cookie_name)
        header = request.headers.get("X-CSRF-Token")
        if not constant_equal(cookie, header):
            raise HTTPException(403, "CSRF check failed")
        if dbs is not None:
            raw_session = request.cookies.get(session_cookie_name)
            if raw_session:
                stored = dbs.scalar(
                    select(Session).where(Session.token_hash == token_hash(raw_session))
                )
                if stored and not constant_equal(stored.csrf_hash, token_hash(cookie or "")):
                    raise HTTPException(403, "CSRF check failed")

    def session_from_request(
        request: Request, dbs: OrmSession, *, require_verified: bool = True
    ) -> tuple[User, Session]:
        raw = request.cookies.get(session_cookie_name)
        if not raw:
            raise HTTPException(401, "Sign in required")
        stored = dbs.scalar(select(Session).where(Session.token_hash == token_hash(raw)))
        if (
            not stored
            or stored.revoked_at
            or is_expired(stored.expires_at)
            or not stored.user.is_active
        ):
            raise HTTPException(401, "Sign in required")
        if require_verified and not stored.user.email_verified_at:
            raise HTTPException(403, "Email verification required")
        stored.last_seen_at = utcnow()
        return stored.user, stored

    def auth(request: Request, dbs: OrmSession = Depends(db)):
        return session_from_request(request, dbs)

    def set_csrf(response: Response, csrf: str) -> None:
        response.set_cookie(
            csrf_cookie_name,
            csrf,
            max_age=settings.session_days * 86400,
            path="/",
            secure=settings.cookie_secure,
            httponly=False,
            samesite="lax",
        )

    def set_session_cookies(response: Response, raw_session: str, csrf: str) -> None:
        response.set_cookie(
            session_cookie_name,
            raw_session,
            max_age=settings.session_days * 86400,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        set_csrf(response, csrf)

    def clear_session_cookies(response: Response) -> None:
        response.delete_cookie(
            session_cookie_name,
            path="/",
            secure=settings.cookie_secure,
            samesite="lax",
        )
        response.delete_cookie(
            csrf_cookie_name,
            path="/",
            secure=settings.cookie_secure,
            samesite="lax",
        )

    def audit(
        dbs: OrmSession,
        event_type: str,
        *,
        actor: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        dbs.add(
            AuditEvent(
                actor_user_id=actor,
                event_type=event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                metadata_json=metadata or {},
            )
        )

    def issue_email_token(
        dbs: OrmSession, user: User, purpose: str, lifetime: timedelta
    ) -> str:
        now = utcnow()
        dbs.execute(
            update(EmailToken)
            .where(
                EmailToken.user_id == user.id,
                EmailToken.purpose == purpose,
                EmailToken.used_at.is_(None),
            )
            .values(used_at=now)
        )
        raw = opaque_token()
        dbs.add(
            EmailToken(
                user_id=user.id,
                purpose=purpose,
                token_hash=token_hash(raw),
                expires_at=now + lifetime,
            )
        )
        return raw

    def send_verification(user: User, raw: str) -> None:
        if not email_backend:
            raise RuntimeError("Email delivery is unavailable")
        link = verification_link(settings.public_app_url, raw)
        email_backend.send(
            to=user.email,
            subject="Verify your Nonprofit AI toolkit account",
            text=(
                "Verify this email address before opening a toolkit review. "
                "The link expires in "
                f"{settings.verification_hours} hours."
            ),
            link=link,
        )

    def send_reset(user: User, raw: str) -> None:
        if not email_backend:
            raise RuntimeError("Email delivery is unavailable")
        link = reset_link(settings.public_app_url, raw)
        email_backend.send(
            to=user.email,
            subject="Reset your Nonprofit AI toolkit password",
            text=(
                "Use this link to set a new password. The link expires in "
                f"{settings.reset_minutes} minutes. Ignore this message if you did not request it."
            ),
            link=link,
        )

    def membership_for(
        dbs: OrmSession, user_id: str, organization_id: str
    ) -> OrganizationMembership | None:
        return dbs.scalar(
            select(OrganizationMembership).where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.organization_id == organization_id,
            )
        )

    def record_for_user(
        dbs: OrmSession, user_id: str, record_id: str
    ) -> AdoptionRecord:
        record = dbs.scalar(
            select(AdoptionRecord)
            .join(
                OrganizationMembership,
                OrganizationMembership.organization_id
                == AdoptionRecord.organization_id,
            )
            .where(
                AdoptionRecord.id == record_id,
                OrganizationMembership.user_id == user_id,
            )
        )
        if not record:
            raise HTTPException(404, "Review record not found")
        return record

    def map_for_user(
        dbs: OrmSession, user_id: str, map_id: str
    ) -> ConceptMap:
        concept_map = dbs.scalar(
            select(ConceptMap)
            .join(AdoptionRecord, AdoptionRecord.id == ConceptMap.record_id)
            .join(
                OrganizationMembership,
                OrganizationMembership.organization_id
                == AdoptionRecord.organization_id,
            )
            .where(
                ConceptMap.id == map_id,
                OrganizationMembership.user_id == user_id,
            )
        )
        if not concept_map:
            raise HTTPException(404, "Concept map not found")
        return concept_map

    def serialize_record(
        dbs: OrmSession, record: AdoptionRecord, *, detail: bool = False
    ) -> dict:
        org = dbs.get(Organization, record.organization_id)
        payload = {
            "id": record.id,
            "organization_id": record.organization_id,
            "organization_name": org.name if org else "",
            "title": record.title,
            "proposed_use": record.proposed_use,
            "current_stage": record.current_stage,
            "status": record.status,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        }
        if detail:
            turns = dbs.scalars(
                select(ConversationTurn)
                .where(ConversationTurn.record_id == record.id)
                .order_by(ConversationTurn.stage, ConversationTurn.ordinal)
            ).all()
            completed = dbs.scalars(
                select(CompletedStep)
                .where(CompletedStep.record_id == record.id)
                .order_by(CompletedStep.completed_at)
            ).all()
            snippets = dbs.scalars(
                select(KnowledgeSnippet)
                .where(KnowledgeSnippet.record_id == record.id)
                .order_by(KnowledgeSnippet.created_at)
            ).all()
            synthesis = dbs.scalar(
                select(Synthesis)
                .where(Synthesis.record_id == record.id)
                .order_by(Synthesis.version.desc())
            )
            concept_map = dbs.scalar(
                select(ConceptMap)
                .where(ConceptMap.record_id == record.id)
                .order_by(ConceptMap.version.desc())
            )
            annotations = (
                dbs.scalars(
                    select(Annotation)
                    .where(Annotation.concept_map_id == concept_map.id)
                    .order_by(Annotation.created_at)
                ).all()
                if concept_map
                else []
            )
            payload.update(
                {
                    "turns": [_serialize_turn(turn) for turn in turns],
                    "completed_steps": [
                        {
                            "stage": step.stage,
                            "record_text": step.record_text,
                            "completed_at": step.completed_at.isoformat(),
                        }
                        for step in completed
                    ],
                    "knowledge_snippets": [
                        {
                            "id": snippet.id,
                            "stage": snippet.stage,
                            "kind": snippet.kind,
                            "title": snippet.title,
                            "content": snippet.content,
                            "provenance": snippet.provenance,
                            "created_at": snippet.created_at.isoformat(),
                        }
                        for snippet in snippets
                    ],
                    "synthesis": (
                        {
                            "id": synthesis.id,
                            "version": synthesis.version,
                            "summary": synthesis.summary,
                            "analysis": synthesis.analysis,
                            "key_points": synthesis.key_points,
                            "open_questions": synthesis.open_questions,
                            "source": synthesis.source,
                        }
                        if synthesis
                        else None
                    ),
                    "concept_map": (
                        {
                            "id": concept_map.id,
                            "version": concept_map.version,
                            "graph": concept_map.graph,
                        }
                        if concept_map
                        else None
                    ),
                    "annotations": [
                        _serialize_annotation(item) for item in annotations
                    ],
                }
            )
        return payload

    def fallback_stage_reply(
        stage: str, turns: list[ConversationTurn]
    ) -> str:
        user_turns = [turn for turn in turns if turn.role == "user"]
        prompts = {
            "entry": [
                "What need or current practice led you to consider this use?",
                "Who would be affected by this use and how?",
                "What outcome would make the review worthwhile?",
                "Who could own the work, and what would make you stop?",
            ],
            "redline": [
                "Which categories of information could this use touch?",
                "Who has authority over those categories and any required consent?",
                "Which decisions must remain with people?",
                "How could affected people question or correct an outcome?",
                "Which condition would make the organization stop this use?",
            ],
        }
        generic = [
            "What condition in this stage is most important to establish?",
            "Who would be affected by that condition?",
            "Who can verify it and decide what happens next?",
            "What remains unresolved before the organization continues?",
        ]
        questions = prompts.get(stage, generic)
        count = len(user_turns)
        if count < STAGE_SPECS[stage]["answers"]:
            return questions[min(count, len(questions) - 1)]
        facts = "\n".join(f"- {turn.content}" for turn in user_turns)
        return (
            f"{STAGE_LABELS[stage]}\n\n"
            f"Organization-supplied responses\n{facts}\n\n"
            "Draft route\nThe organization should review these responses, resolve unknowns, "
            "and decide whether to continue."
        )

    def add_assistant_turn(
        dbs: OrmSession,
        record: AdoptionRecord,
        stage: str,
        turns: list[ConversationTurn],
    ) -> ConversationTurn:
        context_steps = dbs.scalars(
            select(CompletedStep)
            .where(CompletedStep.record_id == record.id)
            .order_by(CompletedStep.completed_at)
        ).all()
        context = "\n\n".join(
            f"{STAGE_LABELS.get(step.stage, step.stage)}\n{step.record_text}"
            for step in context_steps
        )
        org = dbs.get(Organization, record.organization_id)
        prompt = stage_prompt(stage, org.name if org else "the organization", context)
        history = [{"role": turn.role, "content": turn.content} for turn in turns]
        try:
            content = model_client.complete(prompt, history)
            status, error_code = "succeeded", None
        except ModelUnavailable:
            content = fallback_stage_reply(stage, turns)
            status, error_code = "fallback", "model_unavailable"
        ordinal = max((turn.ordinal for turn in turns), default=0) + 1
        assistant = ConversationTurn(
            record_id=record.id,
            stage=stage,
            role="assistant",
            content=content,
            ordinal=ordinal,
        )
        dbs.add(assistant)
        dbs.flush()
        dbs.add(
            ModelRun(
                record_id=record.id,
                stage=stage,
                model=settings.toolkit_model,
                status=status,
                output_turn_id=assistant.id,
                error_code=error_code,
            )
        )
        return assistant

    @app.get("/health")
    def health():
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {
            "status": "ok",
            "auth_ready": settings.email_ready if settings.production else bool(email_backend),
            "database": "connected",
        }

    if (
        not settings.production
        and settings.email_backend == "memory"
        and isinstance(email_backend, MemoryEmailBackend)
    ):

        @app.get("/api/dev/outbox")
        def development_outbox(request: Request):
            host = request.client.host if request.client else ""
            if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
                raise HTTPException(404, "Not found")
            return {
                "messages": [
                    {
                        "to": message.to,
                        "subject": message.subject,
                        "text": message.text,
                        "link": message.link,
                    }
                    for message in email_backend.messages
                ]
            }

    @app.get("/api/auth/session")
    def auth_session(request: Request, response: Response, dbs: OrmSession = Depends(db)):
        csrf = request.cookies.get(csrf_cookie_name) or opaque_token()
        raw = request.cookies.get(session_cookie_name)
        if not raw:
            set_csrf(response, csrf)
            return {"authenticated": False, "user": None, "csrf_token": csrf}
        stored = dbs.scalar(select(Session).where(Session.token_hash == token_hash(raw)))
        if (
            not stored
            or stored.revoked_at
            or is_expired(stored.expires_at)
            or not stored.user.is_active
            or not stored.user.email_verified_at
        ):
            clear_session_cookies(response)
            csrf = opaque_token()
            set_csrf(response, csrf)
            return {"authenticated": False, "user": None, "csrf_token": csrf}
        if not constant_equal(stored.csrf_hash, token_hash(csrf)):
            csrf = opaque_token()
            stored.csrf_hash = token_hash(csrf)
            dbs.commit()
        set_csrf(response, csrf)
        return {
            "authenticated": True,
            "user": _serialize_user(stored.user),
            "csrf_token": csrf,
        }

    @app.post("/api/auth/register", status_code=202)
    def register(
        body: RegisterBody,
        request: Request,
        dbs: OrmSession = Depends(db),
    ):
        require_origin_csrf(request)
        rate_limit(request, "register", limit=5, window=3600)
        if settings.production and not settings.email_ready:
            raise HTTPException(503, "Account registration is temporarily unavailable")
        try:
            email = normalize_email(body.email)
            password = validate_password(body.password)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        rate_limit_identifier("register-email", email, limit=4, window=3600)
        existing = dbs.scalar(select(User).where(User.email == email))
        raw = None
        user = existing
        if not existing:
            user = User(
                email=email,
                password_hash=hash_password(password),
                display_name=_safe_text(body.display_name)[:120] or None,
            )
            dbs.add(user)
            try:
                dbs.flush()
            except IntegrityError:
                dbs.rollback()
                return AUTH_GENERIC
            raw = issue_email_token(
                dbs,
                user,
                "verify",
                timedelta(hours=settings.verification_hours),
            )
            audit(dbs, "account.registered", actor=user.id, entity_type="user", entity_id=user.id)
            dbs.commit()
        elif not existing.email_verified_at:
            raw = issue_email_token(
                dbs,
                existing,
                "verify",
                timedelta(hours=settings.verification_hours),
            )
            dbs.commit()
        if raw and user:
            try:
                send_verification(user, raw)
            except RuntimeError:
                pass
        return AUTH_GENERIC

    @app.post("/api/auth/resend-verification", status_code=202)
    def resend_verification(
        body: EmailBody, request: Request, dbs: OrmSession = Depends(db)
    ):
        require_origin_csrf(request)
        rate_limit(request, "resend-verification", limit=4, window=3600)
        try:
            email = normalize_email(body.email)
        except ValueError:
            return AUTH_GENERIC
        rate_limit_identifier("resend-email", email, limit=4, window=3600)
        user = dbs.scalar(select(User).where(User.email == email))
        if user and not user.email_verified_at and email_backend:
            raw = issue_email_token(
                dbs, user, "verify", timedelta(hours=settings.verification_hours)
            )
            dbs.commit()
            try:
                send_verification(user, raw)
            except RuntimeError:
                pass
        return AUTH_GENERIC

    @app.post("/api/auth/verify")
    def verify_email(
        body: TokenBody, request: Request, dbs: OrmSession = Depends(db)
    ):
        require_origin_csrf(request)
        rate_limit(request, "verify", limit=12, window=900)
        token = dbs.scalar(
            select(EmailToken).where(
                EmailToken.token_hash == token_hash(body.token),
                EmailToken.purpose == "verify",
                EmailToken.used_at.is_(None),
            )
        )
        if not token or is_expired(token.expires_at):
            raise HTTPException(400, "Verification link is invalid or expired")
        token.used_at = utcnow()
        token.user.email_verified_at = utcnow()
        audit(
            dbs,
            "account.email_verified",
            actor=token.user.id,
            entity_type="user",
            entity_id=token.user.id,
        )
        dbs.commit()
        return {"message": "Email verified. You can sign in."}

    @app.post("/api/auth/login")
    def login(
        body: LoginBody,
        request: Request,
        response: Response,
        dbs: OrmSession = Depends(db),
    ):
        require_origin_csrf(request)
        rate_limit(request, "login", limit=10, window=900)
        try:
            email = normalize_email(body.email)
        except ValueError:
            raise HTTPException(401, "Email or password was not accepted")
        rate_limit_identifier("login-email", email, limit=12, window=3600)
        user = dbs.scalar(select(User).where(User.email == email))
        if (
            not user
            or not verify_password(user.password_hash, body.password)
            or not user.is_active
            or not user.email_verified_at
        ):
            raise HTTPException(401, "Email or password was not accepted")
        if needs_password_rehash(user.password_hash):
            user.password_hash = hash_password(body.password)
        raw_session, csrf = opaque_token(), opaque_token()
        stored = Session(
            user_id=user.id,
            token_hash=token_hash(raw_session),
            csrf_hash=token_hash(csrf),
            expires_at=utcnow() + timedelta(days=settings.session_days),
            user_agent_hash=user_agent_hash(request.headers.get("User-Agent")),
        )
        dbs.add(stored)
        audit(dbs, "account.signed_in", actor=user.id, entity_type="session", entity_id=stored.id)
        dbs.commit()
        set_session_cookies(response, raw_session, csrf)
        return {"authenticated": True, "user": _serialize_user(user), "csrf_token": csrf}

    @app.post("/api/auth/logout")
    def logout(
        request: Request,
        response: Response,
        dbs: OrmSession = Depends(db),
    ):
        require_origin_csrf(request, dbs)
        raw = request.cookies.get(session_cookie_name)
        if raw:
            stored = dbs.scalar(
                select(Session).where(Session.token_hash == token_hash(raw))
            )
            if stored and not stored.revoked_at:
                stored.revoked_at = utcnow()
                audit(
                    dbs,
                    "account.signed_out",
                    actor=stored.user_id,
                    entity_type="session",
                    entity_id=stored.id,
                )
                dbs.commit()
        clear_session_cookies(response)
        return {"authenticated": False}

    @app.post("/api/auth/forgot-password", status_code=202)
    def forgot_password(
        body: EmailBody, request: Request, dbs: OrmSession = Depends(db)
    ):
        require_origin_csrf(request)
        rate_limit(request, "forgot-password", limit=5, window=3600)
        try:
            email = normalize_email(body.email)
        except ValueError:
            return FORGOT_GENERIC
        rate_limit_identifier("forgot-email", email, limit=4, window=3600)
        user = dbs.scalar(select(User).where(User.email == email))
        if user and user.email_verified_at and user.is_active and email_backend:
            raw = issue_email_token(
                dbs,
                user,
                "reset",
                timedelta(minutes=settings.reset_minutes),
            )
            audit(
                dbs,
                "account.password_reset_requested",
                actor=user.id,
                entity_type="user",
                entity_id=user.id,
            )
            dbs.commit()
            try:
                send_reset(user, raw)
            except RuntimeError:
                pass
        return FORGOT_GENERIC

    @app.post("/api/auth/reset-password")
    def reset_password(
        body: ResetBody, request: Request, dbs: OrmSession = Depends(db)
    ):
        require_origin_csrf(request)
        rate_limit(request, "reset-password", limit=8, window=900)
        try:
            password = validate_password(body.password)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        token = dbs.scalar(
            select(EmailToken).where(
                EmailToken.token_hash == token_hash(body.token),
                EmailToken.purpose == "reset",
                EmailToken.used_at.is_(None),
            )
        )
        if not token or is_expired(token.expires_at):
            raise HTTPException(400, "Reset link is invalid or expired")
        token.used_at = utcnow()
        token.user.password_hash = hash_password(password)
        dbs.execute(
            update(Session)
            .where(Session.user_id == token.user_id, Session.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        audit(
            dbs,
            "account.password_reset",
            actor=token.user_id,
            entity_type="user",
            entity_id=token.user_id,
        )
        dbs.commit()
        return {"message": "Password changed. Sign in with the new password."}

    @app.get("/api/organizations")
    def list_organizations(
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        rows = dbs.execute(
            select(Organization, OrganizationMembership.role)
            .join(
                OrganizationMembership,
                OrganizationMembership.organization_id == Organization.id,
            )
            .where(OrganizationMembership.user_id == user.id)
            .order_by(Organization.name)
        ).all()
        return {
            "organizations": [
                {"id": org.id, "name": org.name, "role": role} for org, role in rows
            ]
        }

    @app.post("/api/organizations/{organization_id}/members")
    def add_member(
        organization_id: str,
        body: MemberBody,
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        require_origin_csrf(request, dbs)
        membership = membership_for(dbs, user.id, organization_id)
        if not membership or membership.role != "owner":
            raise HTTPException(404, "Organization not found")
        try:
            email = normalize_email(body.email)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        invited = dbs.scalar(
            select(User).where(
                User.email == email,
                User.email_verified_at.is_not(None),
                User.is_active.is_(True),
            )
        )
        if not invited:
            raise HTTPException(422, "This person must create and verify an account first")
        existing = membership_for(dbs, invited.id, organization_id)
        if existing:
            existing.role = body.role
        else:
            dbs.add(
                OrganizationMembership(
                    organization_id=organization_id,
                    user_id=invited.id,
                    role=body.role,
                )
            )
        audit(
            dbs,
            "organization.member_changed",
            actor=user.id,
            entity_type="organization",
            entity_id=organization_id,
            metadata={"role": body.role},
        )
        dbs.commit()
        return {"message": "Organization membership updated"}

    @app.get("/api/records")
    def list_records(
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        records = dbs.scalars(
            select(AdoptionRecord)
            .join(
                OrganizationMembership,
                OrganizationMembership.organization_id
                == AdoptionRecord.organization_id,
            )
            .where(OrganizationMembership.user_id == user.id)
            .order_by(AdoptionRecord.updated_at.desc())
        ).all()
        return {"records": [serialize_record(dbs, record) for record in records]}

    @app.post("/api/records", status_code=201)
    def create_record(
        body: RecordCreateBody,
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        require_origin_csrf(request, dbs)
        organization = None
        if body.organization_id:
            organization = dbs.get(Organization, body.organization_id)
            if not organization or not membership_for(dbs, user.id, organization.id):
                raise HTTPException(404, "Organization not found")
        else:
            name = _safe_text(body.organization_name)[:160]
            if not name:
                raise HTTPException(422, "Organization name is required")
            organization = Organization(name=name, created_by_id=user.id)
            dbs.add(organization)
            dbs.flush()
            dbs.add(
                OrganizationMembership(
                    organization_id=organization.id, user_id=user.id, role="owner"
                )
            )
        record = AdoptionRecord(
            organization_id=organization.id,
            title=_safe_text(body.title)[:180],
            proposed_use=(body.proposed_use or "").strip() or None,
            created_by_id=user.id,
        )
        dbs.add(record)
        dbs.flush()
        audit(
            dbs,
            "record.created",
            actor=user.id,
            entity_type="record",
            entity_id=record.id,
        )
        dbs.commit()
        return {"record": serialize_record(dbs, record, detail=True)}

    @app.get("/api/records/{record_id}")
    def get_record(
        record_id: str,
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        record = record_for_user(dbs, user.id, record_id)
        return {"record": serialize_record(dbs, record, detail=True)}

    @app.patch("/api/records/{record_id}")
    def update_record(
        record_id: str,
        body: RecordUpdateBody,
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        require_origin_csrf(request, dbs)
        record = record_for_user(dbs, user.id, record_id)
        if body.title is not None:
            record.title = _safe_text(body.title)[:180]
        if body.proposed_use is not None:
            record.proposed_use = body.proposed_use.strip() or None
        if body.status is not None:
            if body.status not in {"active", "complete", "archived"}:
                raise HTTPException(422, "Unknown record status")
            record.status = body.status
        record.updated_at = utcnow()
        audit(
            dbs,
            "record.updated",
            actor=user.id,
            entity_type="record",
            entity_id=record.id,
        )
        dbs.commit()
        return {"record": serialize_record(dbs, record, detail=True)}

    def validate_stage(stage: str) -> None:
        if stage not in STAGE_ORDER:
            raise HTTPException(404, "Review stage not found")

    def enforce_stage_order(
        dbs: OrmSession, record: AdoptionRecord, stage: str
    ) -> None:
        stage_index = STAGE_ORDER.index(stage)
        if stage_index == 0:
            return
        completed = set(
            dbs.scalars(
                select(CompletedStep.stage).where(
                    CompletedStep.record_id == record.id
                )
            ).all()
        )
        missing = [
            earlier for earlier in STAGE_ORDER[:stage_index] if earlier not in completed
        ]
        if missing:
            raise HTTPException(
                409,
                "Complete earlier review stages first",
                headers={"X-Missing-Stages": ",".join(missing)},
            )

    @app.post("/api/records/{record_id}/stages/{stage}/start")
    def start_stage(
        record_id: str,
        stage: str,
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        require_origin_csrf(request, dbs)
        validate_stage(stage)
        record = record_for_user(dbs, user.id, record_id)
        enforce_stage_order(dbs, record, stage)
        turns = dbs.scalars(
            select(ConversationTurn)
            .where(
                ConversationTurn.record_id == record.id,
                ConversationTurn.stage == stage,
            )
            .order_by(ConversationTurn.ordinal)
        ).all()
        if not turns:
            add_assistant_turn(dbs, record, stage, [])
            record.current_stage = stage
            record.updated_at = utcnow()
            dbs.commit()
            turns = dbs.scalars(
                select(ConversationTurn)
                .where(
                    ConversationTurn.record_id == record.id,
                    ConversationTurn.stage == stage,
                )
                .order_by(ConversationTurn.ordinal)
            ).all()
        return {"stage": stage, "messages": [_serialize_turn(turn) for turn in turns]}

    @app.post("/api/records/{record_id}/stages/{stage}/messages")
    def stage_message(
        record_id: str,
        stage: str,
        body: MessageBody,
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        require_origin_csrf(request, dbs)
        rate_limit(request, "stage-message", limit=90, window=3600)
        validate_stage(stage)
        record = record_for_user(dbs, user.id, record_id)
        enforce_stage_order(dbs, record, stage)
        completed = dbs.scalar(
            select(CompletedStep).where(
                CompletedStep.record_id == record.id, CompletedStep.stage == stage
            )
        )
        if completed:
            raise HTTPException(409, "This stage is already complete")
        existing_user_turn = dbs.scalar(
            select(ConversationTurn).where(
                ConversationTurn.record_id == record.id,
                ConversationTurn.stage == stage,
                ConversationTurn.idempotency_key == body.idempotency_key,
                ConversationTurn.role == "user",
            )
        )
        if existing_user_turn:
            existing_assistant = dbs.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.record_id == record.id,
                    ConversationTurn.stage == stage,
                    ConversationTurn.role == "assistant",
                    ConversationTurn.ordinal == existing_user_turn.ordinal + 1,
                )
            )
            if not existing_assistant:
                raise HTTPException(409, "The earlier request is still being processed")
            return {
                "stage": stage,
                "message": _serialize_turn(existing_assistant),
                "user_message": _serialize_turn(existing_user_turn),
                "idempotent_replay": True,
            }
        turns = dbs.scalars(
            select(ConversationTurn)
            .where(
                ConversationTurn.record_id == record.id,
                ConversationTurn.stage == stage,
            )
            .order_by(ConversationTurn.ordinal)
        ).all()
        ordinal = max((turn.ordinal for turn in turns), default=0) + 1
        content = body.content.strip()
        user_turn = ConversationTurn(
            record_id=record.id,
            stage=stage,
            role="user",
            content=content,
            ordinal=ordinal,
            idempotency_key=body.idempotency_key,
            created_by_id=user.id,
        )
        dbs.add(user_turn)
        try:
            dbs.flush()
        except IntegrityError:
            dbs.rollback()
            existing_user_turn = dbs.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.record_id == record_id,
                    ConversationTurn.stage == stage,
                    ConversationTurn.idempotency_key == body.idempotency_key,
                    ConversationTurn.role == "user",
                )
            )
            existing_assistant = (
                dbs.scalar(
                    select(ConversationTurn).where(
                        ConversationTurn.record_id == record_id,
                        ConversationTurn.stage == stage,
                        ConversationTurn.role == "assistant",
                        ConversationTurn.ordinal == existing_user_turn.ordinal + 1,
                    )
                )
                if existing_user_turn
                else None
            )
            if existing_user_turn and existing_assistant:
                return {
                    "stage": stage,
                    "message": _serialize_turn(existing_assistant),
                    "user_message": _serialize_turn(existing_user_turn),
                    "idempotent_replay": True,
                }
            raise HTTPException(409, "The earlier request is still being processed")
        dbs.add(
            KnowledgeSnippet(
                record_id=record.id,
                stage=stage,
                kind="response",
                title=f"{STAGE_LABELS[stage]} response",
                content=content,
                provenance={"turn_ids": [user_turn.id]},
                created_by_id=user.id,
            )
        )
        turns = list(turns) + [user_turn]
        assistant = add_assistant_turn(dbs, record, stage, turns)
        record.current_stage = stage
        record.updated_at = utcnow()
        audit(
            dbs,
            "record.response_saved",
            actor=user.id,
            entity_type="record",
            entity_id=record.id,
            metadata={"stage": stage},
        )
        dbs.commit()
        return {
            "stage": stage,
            "message": _serialize_turn(assistant),
            "user_message": _serialize_turn(user_turn),
        }

    @app.post("/api/records/{record_id}/stages/{stage}/complete")
    def complete_stage(
        record_id: str,
        stage: str,
        body: CompleteBody,
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        require_origin_csrf(request, dbs)
        validate_stage(stage)
        record = record_for_user(dbs, user.id, record_id)
        enforce_stage_order(dbs, record, stage)
        existing = dbs.scalar(
            select(CompletedStep).where(
                CompletedStep.record_id == record.id, CompletedStep.stage == stage
            )
        )
        if existing:
            return {
                "stage": stage,
                "record_text": existing.record_text,
                "already_complete": True,
            }
        turns = dbs.scalars(
            select(ConversationTurn)
            .where(
                ConversationTurn.record_id == record.id,
                ConversationTurn.stage == stage,
            )
            .order_by(ConversationTurn.ordinal)
        ).all()
        user_turns = [turn for turn in turns if turn.role == "user"]
        if len(user_turns) < STAGE_SPECS[stage]["answers"]:
            raise HTTPException(409, "Complete the stage conversation first")
        assistant_turns = [turn for turn in turns if turn.role == "assistant"]
        record_text = (body.record_text or "").strip()
        if not record_text and assistant_turns:
            record_text = assistant_turns[-1].content
        if not record_text:
            raise HTTPException(409, "A stage record is required")
        completed = CompletedStep(
            record_id=record.id,
            stage=stage,
            record_text=record_text,
            completed_by_id=user.id,
        )
        dbs.add(completed)
        dbs.add(
            KnowledgeSnippet(
                record_id=record.id,
                stage=stage,
                kind="stage_record",
                title=f"{STAGE_LABELS[stage]} record",
                content=record_text,
                provenance={"turn_ids": [turn.id for turn in turns]},
                created_by_id=user.id,
            )
        )
        stage_index = STAGE_ORDER.index(stage)
        record.current_stage = (
            STAGE_ORDER[stage_index + 1]
            if stage_index + 1 < len(STAGE_ORDER)
            else "synthesis"
        )
        record.updated_at = utcnow()
        audit(
            dbs,
            "record.stage_completed",
            actor=user.id,
            entity_type="record",
            entity_id=record.id,
            metadata={"stage": stage},
        )
        dbs.commit()
        return {
            "stage": stage,
            "record_text": record_text,
            "next_stage": record.current_stage,
        }

    def build_synthesis(
        dbs: OrmSession, record: AdoptionRecord, user: User
    ) -> tuple[Synthesis, ConceptMap]:
        completed_stages = set(
            dbs.scalars(
                select(CompletedStep.stage).where(CompletedStep.record_id == record.id)
            ).all()
        )
        missing = [stage for stage in STAGE_ORDER if stage not in completed_stages]
        if missing:
            raise HTTPException(
                409,
                "Complete every review stage before synthesis",
                headers={"X-Missing-Stages": ",".join(missing)},
            )
        turns = dbs.scalars(
            select(ConversationTurn)
            .where(ConversationTurn.record_id == record.id)
            .order_by(ConversationTurn.created_at, ConversationTurn.ordinal)
        ).all()
        evidence = [
            {
                "id": turn.id,
                "stage": turn.stage,
                "role": turn.role,
                "content": turn.content,
            }
            for turn in turns
        ]
        org = dbs.get(Organization, record.organization_id)
        source = "model"
        error_code = None
        try:
            raw = model_client.complete(
                synthesis_prompt(org.name if org else "the organization", evidence),
                [],
                json_mode=True,
            )
            result = validate_synthesis(parse_json_object(raw), evidence)
        except (ModelUnavailable, ValueError, json.JSONDecodeError):
            source = "deterministic_fallback"
            error_code = "model_or_validation_unavailable"
            result = deterministic_fallback(
                org.name if org else "the organization", evidence
            )
        version = (
            dbs.scalar(
                select(func.max(Synthesis.version)).where(
                    Synthesis.record_id == record.id
                )
            )
            or 0
        ) + 1
        synthesis = Synthesis(
            record_id=record.id,
            version=version,
            summary=result["summary"],
            analysis=result["analysis"],
            key_points=result["key_points"],
            open_questions=result["open_questions"],
            source=source,
            created_by_id=user.id,
        )
        dbs.add(synthesis)
        dbs.flush()
        concept_map = ConceptMap(
            record_id=record.id,
            synthesis_id=synthesis.id,
            version=version,
            graph=result["graph"],
            created_by_id=user.id,
        )
        dbs.add(concept_map)
        dbs.add(
            ModelRun(
                record_id=record.id,
                stage="synthesis",
                model=settings.toolkit_model,
                status="succeeded" if source == "model" else "fallback",
                error_code=error_code,
            )
        )
        dbs.add(
            KnowledgeSnippet(
                record_id=record.id,
                stage="synthesis",
                kind="synthesis",
                title=f"Synthesis version {version}",
                content=result["summary"],
                provenance={
                    "turn_ids": [turn.id for turn in turns],
                    "synthesis_id": synthesis.id,
                    "concept_map_id": concept_map.id,
                },
                created_by_id=user.id,
            )
        )
        record.current_stage = "synthesis"
        record.updated_at = utcnow()
        audit(
            dbs,
            "record.synthesis_created",
            actor=user.id,
            entity_type="record",
            entity_id=record.id,
            metadata={"version": version, "source": source},
        )
        dbs.commit()
        return synthesis, concept_map

    def synthesis_response(synthesis: Synthesis, concept_map: ConceptMap) -> dict:
        return {
            "synthesis": {
                "id": synthesis.id,
                "version": synthesis.version,
                "summary": synthesis.summary,
                "analysis": synthesis.analysis,
                "key_points": synthesis.key_points,
                "open_questions": synthesis.open_questions,
                "source": synthesis.source,
            },
            "concept_map": {
                "id": concept_map.id,
                "version": concept_map.version,
                "graph": concept_map.graph,
            },
        }

    @app.post("/api/records/{record_id}/synthesis")
    def synthesize(
        record_id: str,
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        require_origin_csrf(request, dbs)
        rate_limit(request, "synthesis", limit=12, window=3600)
        record = record_for_user(dbs, user.id, record_id)
        latest = dbs.scalar(
            select(Synthesis)
            .where(Synthesis.record_id == record.id)
            .order_by(Synthesis.version.desc())
        )
        if latest:
            concept_map = dbs.scalar(
                select(ConceptMap).where(ConceptMap.synthesis_id == latest.id)
            )
            return synthesis_response(latest, concept_map)
        synthesis, concept_map = build_synthesis(dbs, record, user)
        return synthesis_response(synthesis, concept_map)

    @app.post("/api/records/{record_id}/synthesis/regenerate")
    def regenerate_synthesis(
        record_id: str,
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        require_origin_csrf(request, dbs)
        rate_limit(request, "synthesis-regenerate", limit=5, window=3600)
        record = record_for_user(dbs, user.id, record_id)
        synthesis, concept_map = build_synthesis(dbs, record, user)
        return synthesis_response(synthesis, concept_map)

    @app.get("/api/records/{record_id}/maps")
    def list_maps(
        record_id: str,
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        record_for_user(dbs, user.id, record_id)
        maps = dbs.scalars(
            select(ConceptMap)
            .where(ConceptMap.record_id == record_id)
            .order_by(ConceptMap.version.desc())
        ).all()
        return {
            "concept_maps": [
                {
                    "id": item.id,
                    "version": item.version,
                    "graph": item.graph,
                    "created_at": item.created_at.isoformat(),
                }
                for item in maps
            ]
        }

    @app.get("/api/records/{record_id}/annotations")
    def list_annotations(
        record_id: str,
        concept_map_id: str | None = Query(default=None),
        request: Request = None,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        record_for_user(dbs, user.id, record_id)
        concept_map = (
            map_for_user(dbs, user.id, concept_map_id)
            if concept_map_id
            else dbs.scalar(
                select(ConceptMap)
                .where(ConceptMap.record_id == record_id)
                .order_by(ConceptMap.version.desc())
            )
        )
        if not concept_map:
            return {"annotations": []}
        if concept_map.record_id != record_id:
            raise HTTPException(404, "Concept map not found")
        annotations = dbs.scalars(
            select(Annotation)
            .where(Annotation.concept_map_id == concept_map.id)
            .order_by(Annotation.created_at)
        ).all()
        return {
            "concept_map_id": concept_map.id,
            "annotations": [_serialize_annotation(item) for item in annotations],
        }

    @app.post("/api/records/{record_id}/annotations", status_code=201)
    def create_annotation(
        record_id: str,
        body: AnnotationBody,
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        require_origin_csrf(request, dbs)
        record_for_user(dbs, user.id, record_id)
        concept_map = (
            map_for_user(dbs, user.id, body.concept_map_id)
            if body.concept_map_id
            else dbs.scalar(
                select(ConceptMap)
                .where(ConceptMap.record_id == record_id)
                .order_by(ConceptMap.version.desc())
            )
        )
        if not concept_map or concept_map.record_id != record_id:
            raise HTTPException(404, "Concept map not found")
        graph = concept_map.graph or {}
        valid_targets = {
            str(item.get("id"))
            for collection in ("nodes", "edges")
            for item in graph.get(collection, [])
        }
        if body.target_type != "map" and body.target_id not in valid_targets:
            raise HTTPException(422, "Annotation target is not in this map version")
        item = Annotation(
            concept_map_id=concept_map.id,
            target_type=body.target_type,
            target_id=body.target_id,
            body=body.body.strip(),
            position=body.position,
            created_by_id=user.id,
        )
        dbs.add(item)
        dbs.flush()
        audit(
            dbs,
            "map.annotation_created",
            actor=user.id,
            entity_type="concept_map",
            entity_id=concept_map.id,
        )
        dbs.commit()
        return {"annotation": _serialize_annotation(item)}

    @app.patch("/api/annotations/{annotation_id}")
    def update_annotation(
        annotation_id: str,
        body: AnnotationUpdateBody,
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        require_origin_csrf(request, dbs)
        item = dbs.get(Annotation, annotation_id)
        if not item:
            raise HTTPException(404, "Annotation not found")
        map_for_user(dbs, user.id, item.concept_map_id)
        if item.created_by_id != user.id:
            raise HTTPException(403, "Only the annotation author can edit it")
        if body.body is not None:
            item.body = body.body.strip()
        if body.position is not None:
            item.position = body.position
        item.updated_at = utcnow()
        audit(
            dbs,
            "map.annotation_updated",
            actor=user.id,
            entity_type="concept_map",
            entity_id=item.concept_map_id,
        )
        dbs.commit()
        return {"annotation": _serialize_annotation(item)}

    @app.delete("/api/annotations/{annotation_id}", status_code=204)
    def delete_annotation(
        annotation_id: str,
        request: Request,
        dbs: OrmSession = Depends(db),
        auth_context=Depends(auth),
    ):
        user, _ = auth_context
        require_origin_csrf(request, dbs)
        item = dbs.get(Annotation, annotation_id)
        if not item:
            raise HTTPException(404, "Annotation not found")
        map_for_user(dbs, user.id, item.concept_map_id)
        if item.created_by_id != user.id:
            raise HTTPException(403, "Only the annotation author can delete it")
        dbs.delete(item)
        audit(
            dbs,
            "map.annotation_deleted",
            actor=user.id,
            entity_type="concept_map",
            entity_id=item.concept_map_id,
        )
        dbs.commit()
        return Response(status_code=204)

    static_root = Path(__file__).resolve().parent.parent

    @app.get("/")
    def index():
        return FileResponse(static_root / "index.html")

    @app.get("/sw.js")
    def retire_service_worker():
        return FileResponse(
            static_root / "sw.js",
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-store",
                "Service-Worker-Allowed": "/",
            },
        )

    @app.get("/{path:path}")
    def static_file(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Not found")
        candidate = (static_root / path).resolve()
        if static_root not in candidate.parents or not candidate.is_file():
            return FileResponse(static_root / "index.html")
        return FileResponse(candidate)

    return app
