"""Strict HTTP boundary for durable browser authentication."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from vonk_agent_protocol.http_failure import TransientReason
from vonk_control.auth import (
    ADMIN_ROLE,
    CSRF_COOKIE,
    SESSION_COOKIE,
    Actor,
    AdministratorRole,
)
from vonk_control.http_errors import temporary_http_answer
from vonk_control.identifiers import UUID_PATTERN
from vonk_control.operation_api.constants import _ADMIN_OPERATION_IDS
from vonk_control.operation_api.contracts import bounded_error_responses
from vonk_control.operator_acceptance.receipt_contract import SecurityEdge
from vonk_control.operator_acceptance.resolution import security_edge
from vonk_control.security.browser_auth import (
    BrowserAuthenticationError,
    BrowserAuthService,
    BrowserIdentity,
    BrowserLoginBusyError,
)

_COOKIE_MAX_AGE = 43_200


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


class CliTokenDownload(BaseModel):
    """What the browser learns from a token download: when the token stops working.

    The token itself is the response body, exact bytes; the expiry travels in the
    ``X-Vonk-Token-Expires-At`` header, which the web client reads into this shape.
    """

    model_config = ConfigDict(extra="forbid")

    expires_at: str


class CliTokenSummary(BaseModel):
    """One stored CLI token: never the token itself, only what identifies it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    expires_at: datetime
    revoked: bool
    presented: bool = Field(
        description="True for the CLI token that authenticated this request"
    )


class CliTokenList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tokens: list[CliTokenSummary]


class LoginRequestInvalid(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    detail: Literal["login request is invalid"]


def install_auth_routes(
    app: FastAPI,
    service: BrowserAuthService,
    actor_dependency: Any,
) -> None:

    _ADMIN_OPERATION_IDS.update(
        {
            ("post", "/api/auth/login"): "loginBrowser",
            ("get", "/api/auth/session"): "getBrowserSession",
            ("post", "/api/auth/logout"): "logoutBrowser",
            ("post", "/api/auth/cli-token"): "downloadCliToken",
            ("get", "/api/auth/cli-tokens"): "listCliTokens",
            ("post", "/api/auth/cli-tokens/{token_id}/revoke"): "revokeCliToken",
            ("post", "/api/auth/cli-tokens/revoke-all"): "revokeAllCliTokens",
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

    def cookie_identity(request: Request) -> BrowserIdentity:
        token = request.cookies.get(SESSION_COOKIE, "")
        try:
            return service.resolve(token)
        except BrowserAuthenticationError:
            raise HTTPException(
                status_code=401, detail="authentication failed"
            ) from None

    def require_csrf(request: Request) -> None:
        cookie = request.cookies.get(CSRF_COOKIE)
        header = request.headers.get("x-csrf-token")
        if not cookie or not header or not secrets.compare_digest(cookie, header):
            raise HTTPException(status_code=403, detail="CSRF validation failed")

    @app.post(
        "/api/auth/login",
        response_model=AuthSession,
        responses={
            **bounded_error_responses(401, 403, 503),
            422: {
                "description": "Invalid login request",
                "model": LoginRequestInvalid,
            },
        },
        operation_id="loginBrowser",
    )
    @security_edge(SecurityEdge.LOGIN)
    async def login(
        body: LoginRequest, request: Request, response: Response
    ) -> AuthSession:
        host = request.headers.get("host")
        if host is None or request.headers.get("origin") != f"https://{host}":
            raise HTTPException(status_code=403, detail="origin validation failed")
        try:
            issued = await service.admit(body.subject, body.password)
        except BrowserLoginBusyError:
            temporary_http_answer(TransientReason.ADMISSION_BUSY)
        except BrowserAuthenticationError:
            raise HTTPException(
                status_code=401, detail="authentication failed"
            ) from None
        response.headers.append(
            "set-cookie",
            f"{SESSION_COOKIE}={issued.token}; HttpOnly; Max-Age={_COOKIE_MAX_AGE}; "
            "Path=/; SameSite=strict; Secure",
        )
        response.headers.append(
            "set-cookie",
            f"{CSRF_COOKIE}={issued.csrf}; Max-Age={_COOKIE_MAX_AGE}; Path=/; "
            "SameSite=strict; Secure",
        )
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
    @security_edge(SecurityEdge.LOGOUT)
    def logout(
        request: Request,
        response: Response,
        _authenticated_actor: Actor = authenticated,
    ) -> None:
        require_csrf(request)
        cookie_identity(request)
        service.logout(request.cookies[SESSION_COOKIE])
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        response.delete_cookie(
            CSRF_COOKIE,
            path="/",
            secure=True,
            httponly=False,
            samesite="strict",
        )

    @app.post(
        "/api/auth/cli-token",
        response_class=Response,
        response_model=None,
        responses={
            **bounded_error_responses(401, 403),
            200: {
                "description": "A short-lived administrator bearer token download",
                "content": {
                    "text/plain": {"schema": {"type": "string", "format": "binary"}}
                },
                "headers": {
                    "X-Vonk-Token-Expires-At": {
                        "description": "When the token stops working (CliTokenDownload.expires_at)",
                        "schema": {"type": "string"},
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
    @security_edge(SecurityEdge.CLI_TOKEN)
    def cli_token(
        request: Request,
        _authenticated_actor: Actor = authenticated,
    ) -> Response:
        # This endpoint is deliberately browser-session-only. CSRF is enforced
        # explicitly here, exactly like logout; resolving the cookie again
        # prevents a bearer caller from minting another bearer.
        identity = cookie_identity(request)
        require_csrf(request)
        issued = service.issue_cli_token(identity)
        expires_at = (
            issued.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        )
        return Response(
            content=f"{issued.token}\n",
            media_type="text/plain",
            headers={
                "Content-Disposition": 'attachment; filename="vonkctl-token"',
                "X-Vonk-Token-Expires-At": expires_at,
            },
        )

    def require_administrator(actor: Actor) -> None:
        if actor.role != ADMIN_ROLE:
            raise HTTPException(status_code=403, detail="insufficient role")

    @app.get(
        "/api/auth/cli-tokens",
        response_model=CliTokenList,
        responses=bounded_error_responses(401, 403),
        operation_id="listCliTokens",
    )
    def cli_tokens(request: Request, actor: Actor = authenticated) -> CliTokenList:
        require_administrator(actor)
        # The actor dependency already resolved this bearer; the listing only
        # marks its row by digest.
        authorization = request.headers.get("authorization", "")
        presented = (
            authorization.removeprefix("Bearer ")
            if authorization.startswith("Bearer ")
            else None
        )
        return CliTokenList(
            tokens=[
                CliTokenSummary(
                    id=record.session_id,
                    expires_at=record.expires_at,
                    revoked=record.revoked,
                    presented=record.presented,
                )
                for record in service.cli_tokens(presented)
            ]
        )

    @app.post(
        "/api/auth/cli-tokens/{token_id}/revoke",
        status_code=204,
        response_model=None,
        responses=bounded_error_responses(401, 403, 404),
        operation_id="revokeCliToken",
    )
    @security_edge(SecurityEdge.CLI_TOKEN_REVOKE)
    def cli_token_revoke(
        token_id: Annotated[str, Path(pattern=UUID_PATTERN)],
        actor: Actor = authenticated,
    ) -> None:
        require_administrator(actor)
        if not service.revoke_cli_token(token_id):
            raise HTTPException(status_code=404, detail="CLI token not found")

    @app.post(
        "/api/auth/cli-tokens/revoke-all",
        status_code=204,
        response_model=None,
        responses=bounded_error_responses(401, 403),
        operation_id="revokeAllCliTokens",
    )
    @security_edge(SecurityEdge.CLI_TOKEN_REVOKE_ALL)
    def cli_token_revoke_all(actor: Actor = authenticated) -> None:
        # Every CLI token and every browser session, this caller's included:
        # the recovery for a lost laptop or a leaked token file.
        require_administrator(actor)
        service.revoke_all()
