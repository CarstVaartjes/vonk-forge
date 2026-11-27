"""``vonkctl auth``: log in once, check the login, list or revoke tokens, or log out.

Login types the admin password into a no-echo prompt (or stdin), uses it for
one browser-style session, mints the usual CLI token through the Controller's
own routes, ends the session, and keeps only the token. The password is never
stored, put in argv or environment, or printed; neither is the token. Every
CLI token is a revocable Controller row: ``logout`` revokes this login's token,
``tokens`` lists them and revokes one by id or all of them (browser sessions
included).
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
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.message import Message
from enum import StrEnum

from cluster_profiles.control_client.client import ControlClient
from cluster_profiles.control_client.connection import (
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
from cluster_profiles.control_client.errors import (
    _MAX_TOKEN,
    ControlClientError,
    ControlForbidden,
    ControlHTTPError,
    ControlUnauthorized,
    LocalInputError,
)
from cluster_profiles.control_client.transport import _read_control_response
from cluster_profiles.control_transport import open_https

_TIMEOUT_SECONDS = 15
_SESSION_COOKIE = "vonk_session"
_CSRF_COOKIE = "vonk_csrf"


class AuthAction(StrEnum):
    LOGIN = "login"
    STATUS = "status"
    LOGOUT = "logout"
    TOKENS = "tokens"


class TokenRevocation(StrEnum):
    """What logout learned about this login's token on the Controller."""

    #: The Controller revoked it.
    REVOKED = "revoked"
    #: The Controller answered 401: the token was already revoked or expired.
    NOT_ACCEPTED = "not-accepted"
    #: No answer that settles it (unreachable, or another failure).
    UNCONFIRMED = "unconfirmed"
    #: There was no stored login token to revoke.
    NONE = "none"


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
    token_revocation: TokenRevocation

    def document(self) -> dict[str, str]:
        return {"token_revocation": self.token_revocation.value}


@dataclass(frozen=True)
class TokenEntry:
    id: str
    expires_at: str
    revoked: bool
    #: The token this vonkctl login presented.
    presented: bool


@dataclass(frozen=True)
class TokensResult:
    tokens: tuple[TokenEntry, ...]

    def document(self) -> dict[str, list[dict[str, str | bool]]]:
        return {"tokens": [asdict(entry) for entry in self.tokens]}


@dataclass(frozen=True)
class RevokeResult:
    #: The revoked token id, or ``all``.
    revoked_token: str

    def document(self) -> dict[str, str]:
        return asdict(self)


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


def _read_password(*, from_stdin: bool) -> str:
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
    except UnicodeDecodeError, ValueError:
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
    token, expires_at = _mint_token(url, _read_password(from_stdin=password_stdin))
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


def _stored_login_client() -> ControlClient:
    credential = resolve_credential()
    return ControlClient(credential.url, credential.token_file)


def _list_tokens(client: ControlClient) -> TokensResult:
    document = client.request("GET", "/api/auth/cli-tokens")
    tokens = document.get("tokens")
    if not isinstance(tokens, list):
        raise LoginFailed("the Controller sent an unreadable token list")
    return TokensResult(
        tuple(
            TokenEntry(
                id=str(item["id"]),
                expires_at=str(item["expires_at"]),
                revoked=item["revoked"] is True,
                presented=item["presented"] is True,
            )
            for item in tokens
            if isinstance(item, dict)
        )
    )


def _revoke(client: ControlClient, token_id: str | None) -> RevokeResult:
    if token_id is None:
        client.request("POST", "/api/auth/cli-tokens/revoke-all", retry=False)
        return RevokeResult("all")
    quoted = urllib.parse.quote(token_id, safe="")
    client.request("POST", f"/api/auth/cli-tokens/{quoted}/revoke", retry=False)
    return RevokeResult(token_id)


def _revoke_own_token() -> TokenRevocation:
    try:
        credential = resolve_credential()
    except LocalInputError, ControlClientError, OSError:
        return TokenRevocation.NONE
    if credential.token_from_environment:
        # The environment's token file is not this login; leave it alone.
        return TokenRevocation.NONE
    try:
        client = ControlClient(credential.url, credential.token_file)
        own = next(
            (entry for entry in _list_tokens(client).tokens if entry.presented), None
        )
        if own is None:
            return TokenRevocation.UNCONFIRMED
        _revoke(client, own.id)
    except ControlUnauthorized:
        return TokenRevocation.NOT_ACCEPTED
    except LocalInputError, ControlClientError, OSError:
        return TokenRevocation.UNCONFIRMED
    return TokenRevocation.REVOKED


def _logout() -> LogoutResult:
    # The local files go regardless; a Controller that answers also revokes
    # this login's token, so a copied token file stops working.
    revocation = _revoke_own_token()
    for path in (login_token_path(), connection_path()):
        path.unlink(missing_ok=True)
    return LogoutResult(revocation)


def run_auth(
    action: str | None,
    *,
    url: str | None,
    password_stdin: bool,
    token_id: str | None = None,
    revoke_all: bool = False,
) -> LoginResult | StatusResult | LogoutResult | TokensResult | RevokeResult:
    if action == AuthAction.LOGIN:
        return _login(url, password_stdin=password_stdin)
    if action == AuthAction.STATUS:
        return _status()
    if action == AuthAction.LOGOUT:
        return _logout()
    if action == AuthAction.TOKENS:
        client = _stored_login_client()
        if token_id is None and not revoke_all:
            return _list_tokens(client)
        return _revoke(client, None if revoke_all else token_id)
    raise ValueError(f"unknown auth action {action!r}")


def render(
    result: LoginResult | StatusResult | LogoutResult | TokensResult | RevokeResult,
) -> str:
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
    if isinstance(result, TokensResult):
        if not result.tokens:
            return "No CLI tokens."
        return "\n".join(
            f"{entry.id}  expires {entry.expires_at[:10]}"
            + ("  revoked" if entry.revoked else "")
            + ("  (this login)" if entry.presented else "")
            for entry in result.tokens
        )
    if isinstance(result, RevokeResult):
        if result.revoked_token == "all":
            return "Revoked every CLI token and browser session."
        return f"Revoked CLI token {result.revoked_token}."
    return {
        TokenRevocation.REVOKED: (
            "Revoked this login's token on the Controller and removed the local login."
        ),
        TokenRevocation.NOT_ACCEPTED: (
            "Removed the local login. The Controller no longer accepted its token "
            "(already revoked or expired)."
        ),
        TokenRevocation.UNCONFIRMED: (
            "Removed the local login. The Controller did not confirm the "
            "revocation, so the token stays valid until it expires or another "
            "login revokes it."
        ),
        TokenRevocation.NONE: "Removed the local login.",
    }[result.token_revocation]
