"""Small signed-token authentication core for CLI and browser sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from starlette.responses import Response

_ROLES = frozenset({"viewer", "operator", "administrator"})
_AGENT_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_AGENT_IDENTITY_SCOPE_KEY = "vonk.agent_identity"
_AGENT_SOURCE_SCOPE_KEY = "vonk.agent_source"
_CURSOR_TOKEN = re.compile(r"v1\.[A-Za-z0-9_-]{1,384}\.[A-Za-z0-9_-]{43}\Z")
_CURSOR_DOMAIN = b"vonk-forge/control-cursor/v1\0"
_MAX_CURSOR_LENGTH = 512

MUTATION_ROLES = {
    ("PATCH", "/api/v1/nodes/{node_id}/profile"): frozenset(
        {"operator", "administrator"}
    ),
    ("POST", "/api/v1/jobs"): frozenset({"operator", "administrator"}),
    ("POST", "/api/v1/proposals"): frozenset({"operator", "administrator"}),
    ("POST", "/api/v1/changes"): frozenset({"administrator"}),
    ("POST", "/api/v1/jobs/{job_id}/resume"): frozenset({"operator", "administrator"}),
    ("POST", "/api/v1/agents/enrollments/grants"): frozenset({"administrator"}),
    ("POST", "/api/v1/agents/nodes/{node_id}/revoke"): frozenset({"administrator"}),
    ("POST", "/api/v1/agents/upgrades/preview"): frozenset({"administrator"}),
    ("POST", "/api/v1/agents/upgrades"): frozenset({"administrator"}),
    (
        "POST",
        "/api/v1/agents/compatibility-recovery/spark3542-a122",
    ): frozenset({"administrator"}),
    (
        "POST",
        "/api/v1/agents/compatibility-recovery/spark3542-a122/abandon",
    ): frozenset({"administrator"}),
    ("POST", "/api/v1/fleet-profiles"): frozenset({"administrator"}),
    ("PUT", "/api/v1/fleet-profiles/{profile_id}"): frozenset({"administrator"}),
    ("DELETE", "/api/v1/fleet-profiles/{profile_id}"): frozenset({"administrator"}),
    ("POST", "/api/v1/fleet-profiles/{profile_id}/preview"): frozenset(
        {"administrator"}
    ),
    ("POST", "/api/v1/fleet-profiles/{profile_id}/apply"): frozenset({"administrator"}),
    # Local catalog authoring and WorkloadRun imports change the controller's
    # authoritative PostgreSQL state. Keep them administrator-only and list
    # preview calls too: previews accept untrusted source documents and are
    # part of the same explicitly audited authorization surface.
    ("POST", "/api/v1/catalog/recipes"): frozenset({"administrator"}),
    ("POST", "/api/v1/catalog/entities"): frozenset({"administrator"}),
    ("PUT", "/api/v1/catalog/entities/{entity_id}/draft"): frozenset({"administrator"}),
    ("POST", "/api/v1/catalog/entities/{entity_id}/resolve"): frozenset(
        {"administrator"}
    ),
    ("PUT", "/api/v1/catalog/recipes/{recipe_id}/draft"): frozenset({"administrator"}),
    ("POST", "/api/v1/catalog/recipes/{recipe_id}/resolve"): frozenset(
        {"administrator"}
    ),
    ("POST", "/api/v1/catalog/recipes/{recipe_id}/fork"): frozenset({"administrator"}),
    ("POST", "/api/v1/catalog/imports/workload_run/preview"): frozenset(
        {"administrator"}
    ),
    ("POST", "/api/v1/catalog/imports/workload_run"): frozenset({"administrator"}),
    ("POST", "/api/v1/catalog/imports/global/preview"): frozenset({"administrator"}),
    ("POST", "/api/v1/catalog/imports/global"): frozenset({"administrator"}),
    ("POST", "/api/v1/catalog/imports/public/preview"): frozenset({"administrator"}),
    ("POST", "/api/v1/catalog/imports/public"): frozenset({"administrator"}),
    ("POST", "/api/v1/catalog/imports/recipe-library"): frozenset({"administrator"}),
    ("PUT", "/api/v1/catalog/recipes/{recipe_id}/publication-report"): frozenset(
        {"administrator"}
    ),
    ("POST", "/api/v1/catalog/recipes/{recipe_id}/publication-export"): frozenset(
        {"administrator"}
    ),
    ("POST", "/api/v1/catalog/recipes/{recipe_id}/resolve-import"): frozenset(
        {"administrator"}
    ),
    ("PUT", "/api/v1/catalog/source-bundles/{sha256}"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/source-checks"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/build-plans/preview"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/image-distribution-plans/preview"): frozenset(
        {"administrator"}
    ),
    ("POST", "/api/v1/recipes/builds"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/mapping-plans/preview"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/mappings"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/image-distributions"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/install-plans/preview"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/installations"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/run-plans/preview"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/runs"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/job-runs"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/runs/{run_id}/artifact-jobs"): frozenset(
        {"operator", "administrator"}
    ),
    ("PUT", "/api/v1/artifact-jobs/{job_id}/inputs/{name}"): frozenset(
        {"operator", "administrator"}
    ),
    ("POST", "/api/v1/artifact-jobs/{job_id}/finalize"): frozenset(
        {"operator", "administrator"}
    ),
    ("POST", "/api/v1/artifact-jobs/{job_id}/submit"): frozenset(
        {"operator", "administrator"}
    ),
    ("POST", "/api/v1/artifact-jobs/{job_id}/cancel"): frozenset(
        {"operator", "administrator"}
    ),
    ("POST", "/api/v1/recipes/stop-plans/preview"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/uninstall-plans/preview"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/operations/{operation_id}/retry"): frozenset(
        {"administrator"}
    ),
    ("POST", "/api/v1/recipes/runs/{run_id}/stop"): frozenset({"administrator"}),
    ("POST", "/api/v1/recipes/installations/{installation_id}/uninstall"): frozenset(
        {"administrator"}
    ),
}


class AuthError(ValueError):
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
        if not isinstance(self.identity, AgentIdentity):
            raise AuthError("invalid proxy-observed agent source")
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
        trusted_proxy_auth: bytes = b"",
        agent_identity_validator: Callable[[AgentIdentity], bool] | None = None,
        activation_identity_validator: Callable[[AgentIdentity], bool] | None = None,
    ) -> None:
        self.app = app
        self._trusted_proxy_auth = trusted_proxy_auth
        self._agent_identity_validator = agent_identity_validator
        self._activation_identity_validator = activation_identity_validator

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
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
        safe_scope["headers"] = sanitized
        supplied_proxy_auth = forwarded.get("x-vonk-agent-proxy-auth", "").encode()
        if (
            self._trusted_proxy_auth
            and hmac.compare_digest(supplied_proxy_auth, self._trusted_proxy_auth)
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
            except (AuthError, KeyError):
                pass
        path = safe_scope.get("path")
        validator = (
            self._activation_identity_validator
            if path == "/agent/v1/renew/activate"
            else self._agent_identity_validator
        )
        if (
            isinstance(path, str)
            and path.startswith("/agent/v1/")
            and path not in {"/agent/v1/bootstrap", "/agent/v1/enroll"}
            and (
                agent_identity_from_scope(safe_scope) is None
                or validator is None
                or not validator(agent_identity_from_scope(safe_scope))
            )
        ):
            await Response(status_code=401)(safe_scope, receive, send)
            return
        await self.app(safe_scope, receive, send)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class TokenCodec:
    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("token signing key must be at least 32 bytes")
        self._key = signing_key

    def issue(self, actor: Actor, *, ttl_seconds: int, now: int) -> str:
        if ttl_seconds <= 0:
            raise ValueError("token lifetime must be positive")
        payload = json.dumps(
            {"sub": actor.subject, "role": actor.role, "exp": now + ttl_seconds},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        body = _encode(payload)
        signature = _encode(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{signature}"

    def verify(self, token: str, *, now: int) -> Actor:
        try:
            body, signature = token.split(".", 1)
            expected = hmac.new(self._key, body.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(_decode(signature), expected):
                raise AuthError("token signature is invalid")
            payload = json.loads(_decode(body))
            if not isinstance(payload, dict):
                raise AuthError("token payload is invalid")
            if not isinstance(payload.get("exp"), int) or payload["exp"] < now:
                raise AuthError("token is expired")
            return Actor(str(payload["sub"]), str(payload["role"]))
        except AuthError:
            raise
        except Exception as error:
            raise AuthError("token is malformed") from error

    def cursor_codec(self) -> CursorCodec:
        """Derive an isolated cursor signer from the configured durable key."""

        key = hmac.new(self._key, _CURSOR_DOMAIN + b"key", hashlib.sha256).digest()
        return CursorCodec(key)


class CursorCodec:
    """Versioned authenticated cursors bound to one resource and query context."""

    def __init__(self, derived_key: bytes) -> None:
        if len(derived_key) != hashlib.sha256().digest_size:
            raise ValueError("cursor signing key is invalid")
        self._key = derived_key

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
        body = _encode(payload)
        signature = _encode(
            hmac.new(
                self._key, _CURSOR_DOMAIN + body.encode("ascii"), hashlib.sha256
            ).digest()
        )
        token = f"v1.{body}.{signature}"
        if len(body) > 384 or len(token) > _MAX_CURSOR_LENGTH:
            raise ValueError("cursor document is too large")
        return token

    def decode(
        self,
        token: str,
        *,
        resource: str,
        order: str,
        context: Mapping[str, object],
    ) -> object:
        try:
            if (
                not isinstance(token, str)
                or len(token) > _MAX_CURSOR_LENGTH
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
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            raise ValueError("cursor is invalid") from None
