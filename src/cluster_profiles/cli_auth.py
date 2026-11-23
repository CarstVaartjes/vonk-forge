"""``vonkctl auth``: log in once, check the login, or forget it.

Login types the admin password into a no-echo prompt (or stdin), uses it for
one browser-style session, mints the usual CLI token through the Controller's
own routes, ends the session, and keeps only the token. The password is never
stored, put in argv or environment, or printed; neither is the token.
"""

from __future__ import annotations

import getpass
import http.cookies
import json
import os
import sys
import urllib.parse
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from enum import StrEnum

from .control_client.client import ControlClient
from .control_client.connection import (
    TOKEN_FILE_ENV,
    URL_ENV,
    StoredConnection,
    connection_path,
    days_until,
    format_expiry,
    login_token_path,
    parse_expiry,
    read_connection_or_none,
    resolve_credential,
    validated_origin,
    write_private,
)
from .control_client.errors import (
    _MAX_TOKEN,
    ControlClientError,
    ControlForbidden,
    ControlHTTPError,
    ControlUnauthorized,
    LocalInputError,
)
from .control_client.transport import _read_control_response
from .control_transport import open_https

_TIMEOUT_SECONDS = 15
_SESSION_COOKIE = "vonk_session"
_CSRF_COOKIE = "vonk_csrf"


class AuthAction(StrEnum):
    LOGIN = "login"
    STATUS = "status"
    LOGOUT = "logout"


class _Unknown(StrEnum):
    EXPIRY = "unknown"


@dataclass(frozen=True)
class LoginResult:
    controller_url: str
    token_expires_at: str
    days_left: int
    shadowed_by: str | None

    def document(self) -> dict[str, str]:
        found = {
            "controller_url": self.controller_url,
            "token_expires_at": self.token_expires_at,
        }
        if self.shadowed_by:
            found["shadowed_by"] = self.shadowed_by
        return found


@dataclass(frozen=True)
class StatusResult:
    controller_url: str
    credential_source: str
    token_expires_at: str | None
    days_left: int | None
    check_error: str | None

    @property
    def check(self) -> str:
        return "ok" if self.check_error is None else f"failed: {self.check_error}"

    @property
    def expiry_text(self) -> str:
        if self.token_expires_at is None:
            return (
                f"{_Unknown.EXPIRY} ({self.credential_source}); run vonkctl auth login"
            )
        return f"{self.token_expires_at} (in {self.days_left} days)"

    def document(self) -> dict[str, str]:
        found = {
            "controller_url": self.controller_url,
            "credential_source": self.credential_source,
            "check": self.check,
        }
        if self.token_expires_at:
            found["token_expires_at"] = self.token_expires_at
        return found


@dataclass(frozen=True)
class LogoutResult:
    def document(self) -> dict[str, str]:
        return {}


class LoginFailed(LocalInputError):
    recovery = ("vonkctl auth login",)


def _origin_header(url: str) -> str:
    """The Origin a browser on this Controller would send (default port omitted)."""

    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


def _post(
    url: str, path: str, headers: dict[str, str], body: bytes | None = None
) -> tuple[int, bytes, Message]:
    request = urllib.request.Request(
        url + path, data=body, headers=headers, method="POST"
    )
    return _read_control_response(open_https, request, _TIMEOUT_SECONDS)


def _session_cookies(headers: Message) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for line in headers.get_all("set-cookie") or []:
        parsed = http.cookies.SimpleCookie()
        try:
            parsed.load(line)
        except http.cookies.CookieError:
            continue
        for name in (_SESSION_COOKIE, _CSRF_COOKIE):
            if name in parsed:
                cookies[name] = parsed[name].value
    return cookies


def _read_password(from_stdin: bool) -> str:
    try:
        if from_stdin:
            password = sys.stdin.readline().rstrip("\r\n")
        elif sys.stdin.isatty():
            password = getpass.getpass("Admin password: ")
        else:
            failure = LoginFailed(
                "no terminal to ask for the password; use --password-stdin"
            )
            failure.recovery = (
                (
                    "run vonkctl auth login in an interactive terminal, "
                    "or pipe the password with --password-stdin"
                ),
            )
            raise failure
    except EOFError:
        raise LoginFailed("no password was entered") from None
    except UnicodeDecodeError:
        raise LoginFailed("the password is not valid UTF-8") from None
    if not password:
        raise LoginFailed("the admin password is empty")
    return password


def _mint_token(url: str, password: str) -> tuple[str, datetime]:
    """Log in, mint the CLI token, log out; nothing is written here."""

    origin = _origin_header(url)
    body = json.dumps({"subject": "admin", "password": password}).encode()
    try:
        status, _content, headers = _post(
            url,
            "/api/auth/login",
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": origin,
            },
            body,
        )
    except ControlUnauthorized:
        raise LoginFailed("the Controller rejected the admin password") from None
    except ControlForbidden:
        raise LoginFailed(
            "the Controller refused the login request (origin check); "
            "check the Controller URL"
        ) from None
    if status == 429:
        wait = headers.get("retry-after")
        raise LoginFailed(
            "too many login attempts"
            + (f"; try again in {wait} seconds" if wait else "; try again later")
        )
    cookies = _session_cookies(headers)
    if status != 200 or len(cookies) != 2:
        raise LoginFailed(f"the Controller login answered {status} without a session")
    session = {
        "Cookie": f"{_SESSION_COOKIE}={cookies[_SESSION_COOKIE]}; "
        f"{_CSRF_COOKIE}={cookies[_CSRF_COOKIE]}",
        "X-CSRF-Token": cookies[_CSRF_COOKIE],
        "Origin": origin,
    }
    try:
        status, content, headers = _post(url, "/api/auth/cli-token", session)
        expiry = headers.get("x-vonk-token-expires-at")
        token = content.decode().strip() if status == 200 else ""
        if (
            not token
            or len(token) > _MAX_TOKEN
            or any(character.isspace() for character in token)
            or not expiry
        ):
            raise LoginFailed(f"the Controller token request answered {status}")
        expires_at = parse_expiry(expiry)
    except (UnicodeDecodeError, ValueError):
        raise LoginFailed("the Controller sent an unreadable token") from None
    finally:
        # The browser session expires by itself; the token is what we keep.
        with suppress(ControlClientError, OSError):
            _post(url, "/api/auth/logout", session)
    return token, expires_at


def _login(url_argument: str | None, *, password_stdin: bool) -> LoginResult:
    stored = None if url_argument else read_connection_or_none()
    chosen = (
        url_argument
        or (stored.controller_url if stored else None)
        or os.environ.get(URL_ENV)
    )
    if not chosen:
        raise LoginFailed(
            "give the Controller address: vonkctl auth login https://<controller>"
        )
    url = validated_origin(chosen)
    token, expires_at = _mint_token(url, _read_password(password_stdin))
    write_private(login_token_path(), f"{token}\n".encode())
    write_private(connection_path(), StoredConnection(url, expires_at).document())
    # Prove the stored credential the way every other command will use it.
    try:
        ControlClient(url, login_token_path()).request("GET", "/api/fleet")
    except ControlClientError as error:
        # Keep the Controller's own code and retry fields; only say what is stored.
        print("the login was stored, but the Fleet check failed", file=sys.stderr)
        if isinstance(error, ControlHTTPError):
            error.recovery = tuple(
                step for step in error.recovery if "auth login" not in step
            )
        raise
    shadowed = [name for name in (URL_ENV, TOKEN_FILE_ENV) if os.environ.get(name)]
    return LoginResult(
        controller_url=url,
        token_expires_at=format_expiry(expires_at),
        days_left=days_until(expires_at, datetime.now(UTC)),
        shadowed_by=" and ".join(shadowed) or None,
    )


def _status() -> StatusResult:
    credential = resolve_credential()
    expires = credential.expires_at
    try:
        ControlClient(credential.url, credential.token_file).request(
            "GET", "/api/fleet"
        )
        check_error = None
    except (ControlClientError, LocalInputError, OSError) as error:
        check_error = str(error)
    if credential.token_from_environment:
        source = f"{TOKEN_FILE_ENV} ({credential.token_file})"
    else:
        source = f"vonkctl login ({credential.token_file})"
    return StatusResult(
        controller_url=credential.url,
        credential_source=source,
        token_expires_at=format_expiry(expires) if expires else None,
        days_left=days_until(expires, datetime.now(UTC)) if expires else None,
        check_error=check_error,
    )


def _logout() -> LogoutResult:
    for path in (login_token_path(), connection_path()):
        path.unlink(missing_ok=True)
    return LogoutResult()


def run_auth(
    action: str | None, *, url: str | None, password_stdin: bool
) -> LoginResult | StatusResult | LogoutResult:
    if action == AuthAction.LOGIN:
        return _login(url, password_stdin=password_stdin)
    if action == AuthAction.STATUS:
        return _status()
    if action == AuthAction.LOGOUT:
        return _logout()
    raise ValueError(f"unknown auth action {action!r}")


def render(result: LoginResult | StatusResult | LogoutResult) -> str:
    if isinstance(result, LoginResult):
        text = (
            f"Logged in to {result.controller_url}; token expires "
            f"{result.token_expires_at[:10]} (in {result.days_left} days)."
        )
        if result.shadowed_by:
            text += (
                f"\nwarning: {result.shadowed_by} take precedence over this "
                "login; remove them from your shell profile"
            )
        return text
    if isinstance(result, StatusResult):
        return "\n".join(
            (
                f"Controller: {result.controller_url}",
                f"Credential: {result.credential_source}",
                f"Expires: {result.expiry_text}",
                f"Check: GET /api/fleet {result.check}",
            )
        )
    return (
        "Removed the local login. The token itself stays valid until it "
        "expires; the Controller has no revocation."
    )
