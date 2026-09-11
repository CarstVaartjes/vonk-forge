"""Strict HTTP boundary for durable browser authentication."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .audit import AuditRecord
from .auth import ADMIN_ROLE, Actor, AdministratorRole, TokenCodec
from .browser_auth import (
    BrowserAuthenticationError,
    BrowserAuthenticationThrottledError,
    BrowserAuthService,
    BrowserIdentity,
)

_COOKIE_MAX_AGE = 43_200
_CLI_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
_SESSION_COOKIE = "vonk_session"
_CSRF_COOKIE = "vonk_csrf"


class AuditSink(Protocol):
    def append(self, event: AuditRecord) -> None: ...


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    subject: Literal["admin"]
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]

    @field_validator("password")
    @classmethod
    def bounded_password(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 256:
            raise ValueError("password is invalid")
        return value


class AuthSession(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    subject: str = Field(min_length=1, max_length=64)
    role: AdministratorRole
    expires_at: datetime


class LoginRequestInvalid(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    detail: Literal["login request is invalid"]


def install_auth_routes(
    app: FastAPI,
    service: BrowserAuthService,
    audits: AuditSink,
    actor_dependency: Any,
    tokens: TokenCodec,
    now: Callable[[], int],
) -> None:
    from .operation_api import _ADMIN_OPERATION_IDS, bounded_error_responses

    _ADMIN_OPERATION_IDS.update(
        {
            ("post", "/api/auth/login"): "loginBrowser",
            ("get", "/api/auth/session"): "getBrowserSession",
            ("post", "/api/auth/logout"): "logoutBrowser",
            ("post", "/api/auth/cli-token"): "downloadCliToken",
        }
    )
    authenticated = actor_dependency

    def summary(identity: BrowserIdentity) -> AuthSession:
        actor = identity.actor
        # BrowserAuthService only issues a browser identity for the sole
        # administrator, so this guard states the session contract's invariant
        # and refuses anything else instead of emitting a non-administrator
        # session or an opaque response-model failure.
        if actor.role != ADMIN_ROLE:
            raise HTTPException(status_code=401, detail="authentication failed")
        return AuthSession(
            subject=actor.subject,
            role=actor.role,
            expires_at=identity.expires_at,
        )

    def audit(request: Request, actor: str, action: str) -> None:
        audits.append(AuditRecord(request.state.request_id, actor, action, None, ()))

    def cookie_identity(request: Request) -> BrowserIdentity:
        token = request.cookies.get(_SESSION_COOKIE, "")
        try:
            return service.resolve(token)
        except BrowserAuthenticationError:
            raise HTTPException(
                status_code=401, detail="authentication failed"
            ) from None

    def require_csrf(request: Request) -> None:
        cookie = request.cookies.get(_CSRF_COOKIE)
        header = request.headers.get("x-csrf-token")
        if not cookie or not header or not secrets.compare_digest(cookie, header):
            raise HTTPException(status_code=403, detail="CSRF validation failed")

    @app.post(
        "/api/auth/login",
        response_model=AuthSession,
        responses={
            **bounded_error_responses(401, 403, 429),
            422: {
                "description": "Invalid login request",
                "model": LoginRequestInvalid,
            }
        },
        operation_id="loginBrowser",
    )
    def login(body: LoginRequest, request: Request, response: Response) -> AuthSession:
        if request.headers.get("origin") != f"https://{request.headers['host']}":
            raise HTTPException(status_code=403, detail="origin validation failed")
        try:
            issued = service.login(body.subject, body.password)
        except BrowserAuthenticationThrottledError:
            audit(request, "anonymous", "auth.login.throttled")
            raise HTTPException(
                status_code=429,
                detail="authentication temporarily unavailable",
            ) from None
        except BrowserAuthenticationError:
            audit(request, "anonymous", "auth.login.failed")
            raise HTTPException(
                status_code=401, detail="authentication failed"
            ) from None
        response.headers.append(
            "set-cookie",
            f"{_SESSION_COOKIE}={issued.token}; HttpOnly; Max-Age={_COOKIE_MAX_AGE}; "
            "Path=/; SameSite=strict; Secure",
        )
        response.headers.append(
            "set-cookie",
            f"{_CSRF_COOKIE}={issued.csrf}; Max-Age={_COOKIE_MAX_AGE}; Path=/; "
            "SameSite=strict; Secure",
        )
        audit(request, issued.identity.actor.subject, "auth.login.succeeded")
        return summary(issued.identity)

    @app.get(
        "/api/auth/session",
        response_model=AuthSession,
        responses=bounded_error_responses(401),
        operation_id="getBrowserSession",
    )
    def session(request: Request, _actor: Actor = authenticated) -> AuthSession:
        return summary(cookie_identity(request))

    @app.post(
        "/api/auth/logout",
        status_code=204,
        response_model=None,
        responses=bounded_error_responses(401, 403),
        operation_id="logoutBrowser",
        openapi_extra={"x-vonk-request-body": "none"},
    )
    def logout(
        request: Request,
        response: Response,
        _authenticated_actor: Actor = authenticated,
    ) -> None:
        require_csrf(request)
        identity = cookie_identity(request)
        service.logout(request.cookies[_SESSION_COOKIE])
        response.delete_cookie(
            _SESSION_COOKIE,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        response.delete_cookie(
            _CSRF_COOKIE,
            path="/",
            secure=True,
            httponly=False,
            samesite="strict",
        )
        audit(request, identity.actor.subject, "auth.logout")

    @app.post(
        "/api/auth/cli-token",
        response_class=Response,
        response_model=None,
        responses={
            **bounded_error_responses(401, 403),
            200: {
                "description": "A short-lived administrator bearer token download",
                "content": {
                    "text/plain": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            },
        },
        operation_id="downloadCliToken",
        openapi_extra={
            "x-vonk-request-body": "none",
            # The token is returned as exact bytes and downloaded by the
            # browser transport; it is never reconstructed as JSON.
            "x-vonk-streaming-transport": True,
        },
    )
    def cli_token(
        request: Request,
        _authenticated_actor: Actor = authenticated,
    ) -> Response:
        # This endpoint is deliberately browser-session-only. The general
        # actor dependency enforces CSRF for cookie mutations; resolving the
        # cookie again prevents a bearer caller from minting another bearer.
        identity = cookie_identity(request)
        issued_at = now()
        token = tokens.issue(
            identity.actor,
            ttl_seconds=_CLI_TOKEN_TTL_SECONDS,
            now=issued_at,
        )
        expires_at = datetime.fromtimestamp(
            issued_at + _CLI_TOKEN_TTL_SECONDS, tz=UTC
        ).isoformat().replace("+00:00", "Z")
        audit(request, identity.actor.subject, "auth.cli_token.issued")
        return Response(
            content=f"{token}\n",
            media_type="text/plain",
            headers={
                "Content-Disposition": 'attachment; filename="vonkctl-token"',
                "X-Vonk-Token-Expires-At": expires_at,
            },
        )
