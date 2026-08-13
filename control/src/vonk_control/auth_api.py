"""Strict HTTP boundary for durable browser authentication."""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .audit import AuditRecord
from .auth import Actor
from .browser_auth import (
    BrowserAuthenticationError,
    BrowserAuthenticationThrottledError,
    BrowserAuthService,
    BrowserIdentity,
)

_COOKIE_MAX_AGE = 43_200
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
    role: Literal["administrator"]
    expires_at: datetime


def install_auth_routes(
    app: FastAPI,
    service: BrowserAuthService,
    audits: AuditSink,
    actor_dependency: Any,
) -> None:
    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(
        {
            ("post", "/api/v1/auth/login"): "loginBrowser",
            ("get", "/api/v1/auth/session"): "getBrowserSession",
            ("post", "/api/v1/auth/logout"): "logoutBrowser",
        }
    )
    authenticated = actor_dependency

    def summary(identity: BrowserIdentity) -> AuthSession:
        return AuthSession(
            subject=identity.actor.subject,
            role=identity.actor.role,
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
        "/api/v1/auth/login",
        response_model=AuthSession,
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
        "/api/v1/auth/session",
        response_model=AuthSession,
        operation_id="getBrowserSession",
    )
    def session(request: Request, _actor: Actor = authenticated) -> AuthSession:
        return summary(cookie_identity(request))

    @app.post(
        "/api/v1/auth/logout",
        status_code=204,
        response_model=None,
        operation_id="logoutBrowser",
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
