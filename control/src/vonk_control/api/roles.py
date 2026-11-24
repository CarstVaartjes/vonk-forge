"""Api: the authenticated actor dependency and the mutation role check."""

from __future__ import annotations

import secrets
from collections.abc import Callable

from fastapi import (
    HTTPException,
    Request,
)

from vonk_control.auth import MUTATION_ROLES, Actor, AuthError, TokenCodec
from vonk_control.security.browser_auth import (
    BrowserAuthenticationError,
    BrowserAuthService,
)


def make_actor_dependency(
    *,
    tokens: TokenCodec,
    browser_auth: BrowserAuthService | None,
    now: Callable[[], int],
) -> Callable[[Request], Actor]:
    def actor(request: Request) -> Actor:
        authorization = request.headers.get("authorization", "")
        cookie_auth = False
        if authorization.startswith("Bearer "):
            encoded = authorization.removeprefix("Bearer ")
            if not encoded:
                raise HTTPException(status_code=401, detail="authentication required")
            try:
                authenticated = tokens.verify(encoded, now=now())
            except AuthError:
                raise HTTPException(
                    status_code=401, detail="authentication failed"
                ) from None
        else:
            encoded = request.cookies.get("vonk_session", "")
            cookie_auth = bool(encoded)
            if not encoded:
                raise HTTPException(status_code=401, detail="authentication required")
            if browser_auth is None:
                raise HTTPException(status_code=401, detail="authentication failed")
            try:
                authenticated = browser_auth.resolve(encoded).actor
            except BrowserAuthenticationError:
                raise HTTPException(
                    status_code=401, detail="authentication failed"
                ) from None
        if cookie_auth and request.method not in {"GET", "HEAD", "OPTIONS"}:
            cookie = request.cookies.get("vonk_csrf")
            header = request.headers.get("x-csrf-token")
            if not cookie or not header or not secrets.compare_digest(cookie, header):
                raise HTTPException(status_code=403, detail="CSRF validation failed")
        return authenticated

    return actor


def require_mutation_role(
    authenticated: Actor, path: str, method: str = "POST"
) -> None:
    if authenticated.role not in MUTATION_ROLES[(method, path)]:
        raise HTTPException(status_code=403, detail="insufficient role")
