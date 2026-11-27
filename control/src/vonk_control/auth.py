"""Authentication core: actors, agent identity scope, bearer and cursor contracts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from starlette.responses import Response
from starlette.types import Scope

from vonk_agent_protocol.outcome import InvalidRequestError, SecurityRefusalError
from vonk_control.capabilities import CapabilityUnavailableError
from vonk_control.cursor_contract import MAX_CURSOR_LENGTH

# The one definition of the administrator role. Browser sessions and their
# response contract both refer to it, so the set cannot drift.
AdministratorRole = Literal["administrator"]
ADMIN_ROLE: Final[AdministratorRole] = "administrator"
_ROLES = frozenset({"viewer", "operator", "administrator"})
_AGENT_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_AGENT_IDENTITY_SCOPE_KEY = "vonk.agent_identity"
_AGENT_SOURCE_SCOPE_KEY = "vonk.agent_source"
_AGENT_VALIDATED_SCOPE_KEY = "vonk.agent_validated"
_CURSOR_DOMAIN = b"vonk-forge/control-cursor/v1\0"
_CURSOR_SIGNATURE_LENGTH = len(
    base64.urlsafe_b64encode(hashlib.sha256().digest()).rstrip(b"=")
)
_MAX_CURSOR_BODY_LENGTH = MAX_CURSOR_LENGTH - len("v1..") - _CURSOR_SIGNATURE_LENGTH
_MAX_CURSOR_PAYLOAD_LENGTH = _MAX_CURSOR_BODY_LENGTH * 3 // 4
_CURSOR_TOKEN = re.compile(
    rf"v1\.[A-Za-z0-9_-]{{1,{_MAX_CURSOR_BODY_LENGTH}}}\."
    rf"[A-Za-z0-9_-]{{{_CURSOR_SIGNATURE_LENGTH}}}\Z"
)

MUTATION_ROLES = {
    ("POST", "/api/fleet/enroll"): frozenset({"administrator"}),
    ("POST", "/api/fleet/{selector}/re-enroll"): frozenset({"administrator"}),
    ("POST", "/api/fleet/enrollments/{grant_id}/revoke"): frozenset({"administrator"}),
    ("POST", "/api/fleet/{selector}/remove"): frozenset({"administrator"}),
    ("POST", "/api/fleet/{selector}/rename"): frozenset({"administrator", "operator"}),
    ("POST", "/api/fleet/upgrade"): frozenset({"administrator"}),
    ("POST", "/api/model/{selector:path}/download"): frozenset(
        {"administrator", "operator"}
    ),
    ("POST", "/api/model/{selector:path}/remove"): frozenset(
        {"administrator", "operator"}
    ),
    ("POST", "/api/model/operations/{operation_id}/resume-access"): frozenset(
        {"administrator", "operator"}
    ),
    ("POST", "/api/model/operations/{operation_id}/cancel"): frozenset(
        {"administrator", "operator"}
    ),
    ("POST", "/api/recipe/{selector:path}/download"): frozenset(
        {"administrator", "operator"}
    ),
    ("POST", "/api/recipe/{selector:path}/remove"): frozenset(
        {"administrator", "operator"}
    ),
    ("POST", "/api/recipe/update"): frozenset({"administrator", "operator"}),
    ("POST", "/api/recipe/operations/{operation_id}/retry"): frozenset(
        {"administrator", "operator"}
    ),
    ("POST", "/api/recipe/operations/{operation_id}/cancel"): frozenset(
        {"administrator", "operator"}
    ),
    ("POST", "/api/jobs/{job_id}/resume"): frozenset({"administrator", "operator"}),
    ("PUT", "/api/profile/{number}"): frozenset({"administrator"}),
    ("POST", "/api/profile/{number}/preview"): frozenset({"administrator"}),
    ("POST", "/api/profile/{number}/delete"): frozenset({"administrator"}),
    ("POST", "/api/profile/{number}/load"): frozenset({"administrator"}),
    (
        "POST",
        "/api/recipe/installations/{installation_id}/reconcile",
    ): frozenset({"administrator"}),
    (
        "POST",
        "/api/recipe/installations/{installation_id}/reconcile/preview",
    ): frozenset({"administrator"}),
    ("POST", "/api/profile/applications/{application_id}/cancel"): frozenset(
        {"administrator"}
    ),
    ("POST", "/api/key"): frozenset({"administrator"}),
    ("POST", "/api/key/{name}/revoke"): frozenset({"administrator"}),
    ("POST", "/api/key/{name}/roll"): frozenset({"administrator"}),
    ("POST", "/api/recipe/runs/{run_id}/artifact-jobs"): frozenset(
        {"administrator", "operator"}
    ),
    ("PUT", "/api/artifact-jobs/{job_id}/inputs/{name}"): frozenset(
        {"administrator", "operator"}
    ),
    ("POST", "/api/artifact-jobs/{job_id}/finalize"): frozenset(
        {"administrator", "operator"}
    ),
    ("POST", "/api/artifact-jobs/{job_id}/submit"): frozenset(
        {"administrator", "operator"}
    ),
    ("POST", "/api/artifact-jobs/{job_id}/cancel"): frozenset(
        {"administrator", "operator"}
    ),
}


class AuthError(SecurityRefusalError, ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    subject: str
    role: str

    def __post_init__(self) -> None:
        if not self.subject.strip() or self.role not in _ROLES:
            raise AuthError("invalid authenticated actor")


_CAPABILITY_ROLES: dict[str, frozenset[str]] = {
    "fleet:enroll": frozenset({"administrator"}),
    "fleet:review": frozenset({"administrator"}),
    "fleet:revoke": frozenset({"administrator"}),
}


def has_capability(actor: Actor | str, capability: str) -> bool:
    """Return whether an authenticated actor may perform a capability."""
    role = actor.role if isinstance(actor, Actor) else actor
    return role in _CAPABILITY_ROLES.get(capability, frozenset())


def require_capability(actor: Actor | str, capability: str) -> None:
    """Fail closed when an actor lacks the requested capability."""
    if not has_capability(actor, capability):
        raise AuthError(f"missing capability: {capability}")


@dataclass(frozen=True)
class AgentIdentity:
    """An identity attested by the private TLS-terminating proxy."""

    node_id: str
    certificate_serial: str
    certificate_fingerprint: str
    verified: bool

    def __post_init__(self) -> None:
        if (
            _AGENT_NODE_ID.fullmatch(self.node_id) is None
            or not self.certificate_serial.strip()
            or not self.certificate_fingerprint.strip()
            or self.verified is not True
        ):
            raise AuthError("invalid verified agent identity")


@dataclass(frozen=True)
class AgentSource:
    """A proxy-observed management address bound to one verified identity."""

    identity: AgentIdentity
    management_address: str

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.management_address)
        except ValueError as error:
            raise AuthError("invalid proxy-observed agent source") from error
        if str(address) != self.management_address:
            raise AuthError("invalid proxy-observed agent source")


def agent_identity_from_scope(scope: dict[str, Any]) -> AgentIdentity | None:
    """Return only a typed, verification-marked proxy identity from a scope."""
    identity = scope.get(_AGENT_IDENTITY_SCOPE_KEY)
    if not isinstance(identity, AgentIdentity) or identity.verified is not True:
        return None
    return identity


def validated_agent_identity_from_scope(
    scope: Scope,
) -> AgentIdentity | None:
    """Return the identity the middleware already validated against the database."""
    identity = agent_identity_from_scope(dict(scope))
    if identity is None or scope.get(_AGENT_VALIDATED_SCOPE_KEY) != identity:
        return None
    return identity


def agent_source_from_scope(scope: dict[str, Any]) -> AgentSource | None:
    """Return only a typed source bound to the scope's verified identity."""
    source = scope.get(_AGENT_SOURCE_SCOPE_KEY)
    if not isinstance(source, AgentSource):
        return None
    identity = agent_identity_from_scope(scope)
    if identity is None or source.identity != identity:
        return None
    return source


class TrustedProxyAgentIdentityMiddleware:
    """Convert forwarded mTLS metadata from configured private peers only.

    It deliberately removes every incoming ``X-Vonk-Agent-*`` header before
    invoking the application.  Consequently downstream code can only consume
    the typed ASGI scope value, never a client supplied header.
    """

    def __init__(
        self,
        app: Any,
        *,
        trusted_proxy_auth: bytes | Callable[[], bytes] = b"",
        agent_identity_validator: Callable[[AgentIdentity], bool] | None = None,
        activation_identity_validator: Callable[[AgentIdentity], bool] | None = None,
    ) -> None:
        self.app = app
        self._trusted_proxy_auth = trusted_proxy_auth
        self._agent_identity_validator = agent_identity_validator
        self._activation_identity_validator = activation_identity_validator

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        raw_headers = scope.get("headers", ())
        forwarded: dict[str, str] = {}
        duplicate_forwarded_headers = False
        for key, value in raw_headers:
            if not key.lower().startswith(b"x-vonk-agent-"):
                continue
            name = key.decode("latin-1").lower()
            if name in forwarded:
                duplicate_forwarded_headers = True
            forwarded[name] = value.decode("latin-1")
        sanitized = tuple(
            (key, value)
            for key, value in raw_headers
            if not key.lower().startswith(b"x-vonk-agent-")
        )
        safe_scope = dict(scope)
        safe_scope.pop(_AGENT_IDENTITY_SCOPE_KEY, None)
        safe_scope.pop(_AGENT_SOURCE_SCOPE_KEY, None)
        safe_scope.pop(_AGENT_VALIDATED_SCOPE_KEY, None)
        safe_scope["headers"] = sanitized
        supplied_proxy_auth = forwarded.get("x-vonk-agent-proxy-auth", "").encode()
        proxy_auth = self._trusted_proxy_auth
        if callable(proxy_auth):
            # Ordinary admin reads do not depend on the agent ingress secret.
            if supplied_proxy_auth:
                try:
                    proxy_auth = await asyncio.to_thread(proxy_auth)
                except CapabilityUnavailableError as error:
                    response = Response(
                        error.reply.model_dump_json(),
                        status_code=503,
                        media_type="application/json",
                    )
                    await response(scope, receive, send)
                    return
            else:
                proxy_auth = b""
        if (
            proxy_auth
            and hmac.compare_digest(supplied_proxy_auth, proxy_auth)
            and not duplicate_forwarded_headers
        ):
            try:
                identity = AgentIdentity(
                    node_id=forwarded["x-vonk-agent-node"],
                    certificate_serial=forwarded["x-vonk-agent-serial"],
                    certificate_fingerprint=forwarded["x-vonk-agent-fingerprint"],
                    verified=forwarded["x-vonk-agent-verified"] == "1",
                )
                source = AgentSource(
                    identity=identity,
                    management_address=forwarded["x-vonk-agent-source"],
                )
                safe_scope[_AGENT_IDENTITY_SCOPE_KEY] = identity
                safe_scope[_AGENT_SOURCE_SCOPE_KEY] = source
            except AuthError, KeyError:
                pass
        path = safe_scope.get("path")
        scoped_identity = agent_identity_from_scope(safe_scope)
        validator = (
            self._activation_identity_validator
            if path == "/agent/renew/activate"
            else self._agent_identity_validator
        )
        if (
            isinstance(path, str)
            and path.startswith("/agent/")
            and path
            not in {"/agent/bootstrap", "/agent/enroll", "/agent/renew/expired"}
        ):
            if (
                scoped_identity is None
                or validator is None
                or not await asyncio.to_thread(validator, scoped_identity)
            ):
                await Response(status_code=401)(safe_scope, receive, send)
                return
            # Validated once here; handlers reuse it instead of a second query.
            safe_scope[_AGENT_VALIDATED_SCOPE_KEY] = scoped_identity
        await self.app(safe_scope, receive, send)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class BearerTokens(Protocol):
    """Verify a presented bearer token and derive the cursor signer.

    Production resolves a bearer to a revocable CLI-token session row
    (``SessionBearerTokens``); nothing verifies a self-contained token.
    """

    def verify(self, token: str, *, now: int) -> Actor: ...

    def cursor_codec(self) -> CursorCodec: ...


class CursorError(InvalidRequestError, ValueError):
    """A presented cursor is not a valid signed cursor for this query.

    Separated from a plain ``ValueError`` so a caller can tell a rejected
    cursor apart from stored data that failed to validate.
    """


def cursor_key(boundary: object, length: int) -> tuple[str, ...]:
    """The sort key a signed cursor carries; any other shape is an invalid cursor."""

    if (
        isinstance(boundary, list)
        and len(boundary) == length
        and all(isinstance(part, str) for part in boundary)
    ):
        return tuple(boundary)
    raise CursorError("cursor is invalid")


class CursorCodec:
    """Versioned authenticated cursors bound to one resource and query context."""

    def __init__(self, derived_key: bytes) -> None:
        if len(derived_key) != hashlib.sha256().digest_size:
            raise ValueError("cursor signing key is invalid")
        self._key = derived_key

    @classmethod
    def from_signing_key(cls, signing_key: bytes) -> CursorCodec:
        """Derive the isolated cursor signer from the durable token-signing key."""

        if len(signing_key) < 32:
            raise ValueError("token signing key must be at least 32 bytes")
        return cls(
            hmac.new(signing_key, _CURSOR_DOMAIN + b"key", hashlib.sha256).digest()
        )

    def encode(
        self,
        *,
        resource: str,
        order: str,
        context: Mapping[str, object],
        boundary: object,
    ) -> str:
        document = {
            "boundary": boundary,
            "context": dict(context),
            "order": order,
            "resource": resource,
            "version": 1,
        }
        try:
            payload = json.dumps(
                document,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        except (TypeError, ValueError) as error:
            raise ValueError("cursor document is invalid") from error
        if len(payload) > _MAX_CURSOR_PAYLOAD_LENGTH:
            raise ValueError("cursor document is too large")
        body = _encode(payload)
        signature = _encode(
            hmac.new(
                self._key, _CURSOR_DOMAIN + body.encode("ascii"), hashlib.sha256
            ).digest()
        )
        token = f"v1.{body}.{signature}"
        if len(token) > MAX_CURSOR_LENGTH:
            raise ValueError("cursor document is too large")
        return token

    def decode(
        self,
        token: object,
        *,
        resource: str,
        order: str,
        context: Mapping[str, object],
    ) -> object:
        try:
            if (
                not isinstance(token, str)
                or len(token) > MAX_CURSOR_LENGTH
                or _CURSOR_TOKEN.fullmatch(token) is None
            ):
                raise ValueError
            version, body, signature = token.split(".")
            if version != "v1":
                raise ValueError
            supplied = _decode(signature)
            payload = _decode(body)
            if _encode(supplied) != signature or _encode(payload) != body:
                raise ValueError
            expected = hmac.new(
                self._key,
                _CURSOR_DOMAIN + body.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if len(supplied) != len(expected) or not hmac.compare_digest(
                supplied, expected
            ):
                raise ValueError
            document = json.loads(payload)
            if (
                not isinstance(document, dict)
                or set(document)
                != {"boundary", "context", "order", "resource", "version"}
                or document["version"] != 1
                or document["resource"] != resource
                or document["order"] != order
                or document["context"] != dict(context)
            ):
                raise ValueError
            return document["boundary"]
        except UnicodeError, ValueError, TypeError, json.JSONDecodeError:
            raise CursorError("cursor is invalid") from None
