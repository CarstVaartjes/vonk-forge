"""Inference gateway client keys: LiteLLM virtual keys managed by the Controller.

Clients authenticate to the gateway (`/v1`) with a LiteLLM virtual key, never
the Controller token. The Controller creates, lists and revokes those keys
through Caddy's key-only relay to LiteLLM's admin API, authenticated with the
LiteLLM master key. A key is returned once, at creation; listing never shows a
secret. A key without a model list may use every gateway model, so it keeps
working as profiles change.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import logging
import os
import re
import secrets
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import httpx2
from fastapi import FastAPI, HTTPException, Response, status
from fastapi import Path as PathParameter
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from vonk_agent_protocol import (
    ErrorCategory,
    InvalidRequestReason,
    SecurityRefusalReason,
    UnknownError,
    UnknownOutcomeError,
    WaitReason,
)

from .auth import MUTATION_ROLES, Actor
from .operation_api import bounded_error_responses
from .settings import SECRETS_ROOT
from .strict_json import StrictJSONModel

_LOGGER = logging.getLogger(__name__)

# Caddy relays only LiteLLM's key routes on this internal listener.
LITELLM_KEY_ADMIN_URL = "http://caddy:8087"
MASTER_KEY_FILE = SECRETS_ROOT / "gateway-litellm-master-key"
# A host bind mount: the operator finds the default key in secrets/gateway/.
DEFAULT_KEY_FILE = Path("/gateway-secrets/client-key")
DEFAULT_KEY_NAME = "default"

_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$"
_MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,119}$"
_DURATION_PATTERN = r"^[1-9][0-9]{0,5}[smhd]$"
_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{16,256}\Z")
_PAGE_SIZE = 100
_MAX_PAGES = 100
_KEY_ROUTES = ["openai_routes"]
_KEY_PATH = "/api/key"
_REVOKE_PATH = "/api/key/{name}/revoke"
_ROLL_PATH = "/api/key/{name}/roll"


class GatewayKeyError(UnknownOutcomeError, RuntimeError):
    """LiteLLM could not complete a key request; the detail is secret-free.

    Read boundaries retry with bounded backoff; mutation boundaries return the
    shared typed uncertainty result. Default maintenance observes its durable
    secret again on the next bounded-rate pass.
    """


def _observe_gateway[T](
    action: Callable[[], T], *, attempts: int = 1
) -> T | UnknownError:
    """Bound retries; mutation retries reconcile their retained exact secret first."""
    last_unknown: UnknownError | None = None
    for attempt in range(attempts):
        try:
            result = action()
            if not isinstance(result, UnknownError):
                return result
            last_unknown = result
        except HTTPException as error:
            if error.status_code in (401, 403) or (
                error.detail == SecurityRefusalReason.AGENT_IDENTITY_MISMATCH
            ):
                raise
            if error.status_code == 422:
                raise
        except (GatewayKeyError, OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(0.05 * (2**attempt))
    return last_unknown or UnknownError(
        category=ErrorCategory.UNKNOWN, reason=WaitReason.OBSERVATION_UNAVAILABLE
    )


class GatewayKeyCreateRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(pattern=_NAME_PATTERN, max_length=63)
    models: list[Annotated[str, Field(pattern=_MODEL_PATTERN)]] = Field(
        default_factory=list, max_length=64
    )
    expires: str | None = Field(default=None, pattern=_DURATION_PATTERN)


class GatewayKeyView(StrictJSONModel):
    """One gateway client key without its secret. Empty `models` means all."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=256)
    models: list[str]
    created_at: str | None = None
    expires_at: str | None = None
    last_used_at: str | None = None


class GatewayKeyCreated(GatewayKeyView):
    """The retained key for this mutation; the exact receipt can be replayed."""

    key: str = Field(min_length=1, max_length=512)


class GatewayKeyList(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    keys: list[GatewayKeyView]


class GatewayKeyRevoked(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=63)


class _LiteLlmRequest(BaseModel):
    """What the Controller sends to LiteLLM's key admin API (fields we choose)."""

    model_config = ConfigDict(extra="forbid")


class _KeyMetadata(_LiteLlmRequest):
    managed_by: str = "vonk-forge"


class _KeyGenerateRequest(_LiteLlmRequest):
    key_alias: str
    models: list[str]
    allowed_routes: list[str]
    metadata: _KeyMetadata
    duration: str | None = None
    key: str | None = None


class GatewayMutationReceipt(StrictJSONModel):
    request_id: str
    body: _KeyGenerateRequest
    completed: bool = False
    superseded: bool = False


class _KeyDeleteRequest(_LiteLlmRequest):
    key_aliases: list[str] = Field(default_factory=list)
    keys: list[str] | None = None


class _KeyUpdateRequest(_LiteLlmRequest):
    key: str
    key_alias: str


class _KeyListParams(_LiteLlmRequest):
    return_full_object: str = "true"
    page: int
    size: int


class _KeyInfoParams(_LiteLlmRequest):
    key: str


class _LiteLlmReply(BaseModel):
    """A LiteLLM answer: only the fields the Controller reads, the rest ignored."""

    model_config = ConfigDict(extra="ignore")


def _loose_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _loose_count(value: object) -> int | None:
    return value if type(value) is int else None


# LiteLLM is a foreign service. Ignore damaged foreign entries; unreadable
# target scope remains unknown and never becomes unrestricted access.
_LooseText = Annotated[str | None, BeforeValidator(_loose_text)]
_LooseCount = Annotated[int | None, BeforeValidator(_loose_count)]


class _LiteLlmKey(_LiteLlmReply):
    """One virtual key with readable scope; unrelated damaged entries are skipped."""

    key_alias: _LooseText = None
    key_name: _LooseText = None
    models: list[str]
    token: _LooseText = None
    created_at: _LooseText = None
    expires: str | None = None
    last_active: _LooseText = None


class _KeyListReply(_LiteLlmReply):
    keys: list[object] | None = Field(default=None)
    total_pages: _LooseCount = None


class _KeyGenerateReply(_LiteLlmKey):
    key: _LooseText = None


class _KeyInfoReply(_LiteLlmReply):
    info: _LiteLlmKey | None = None

    @field_validator("info", mode="before")
    @classmethod
    def _only_an_object(cls, value: object) -> object:
        return value if isinstance(value, dict) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _remaining(expires: object) -> str | None:
    """LiteLLM's `duration` for the time left until an ISO `expires` stamp."""
    text = _text(expires)
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise GatewayKeyError(
            "gateway key expiry observation is unavailable"
        ) from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = int((moment - datetime.now(UTC)).total_seconds())
    return f"{max(seconds, 1)}s"


def _rolling_alias(name: str) -> str:
    """The alias a rolled key holds until the old key is gone."""
    return f"{name}.rolling"


def _view(item: _LiteLlmKey) -> GatewayKeyView | None:
    name = item.key_alias or item.key_name
    if name is None:
        return None
    return GatewayKeyView(
        name=name[:256],
        models=item.models,
        created_at=item.created_at,
        expires_at=item.expires,
        last_used_at=item.last_active,
    )


class GatewayKeyService:
    """Create, list and revoke LiteLLM virtual keys with the master key."""

    _request: ContextVar[str | None] = ContextVar("gateway_request", default=None)

    def __init__(
        self,
        *,
        base_url: str = LITELLM_KEY_ADMIN_URL,
        master_key: Callable[[], str] | None = None,
        transport: httpx2.BaseTransport | None = None,
        intent_root: Path = DEFAULT_KEY_FILE.parent / "mutations",
    ) -> None:
        self._intent_root = intent_root
        self._master_key = master_key or (
            lambda: MASTER_KEY_FILE.read_text(encoding="utf-8").strip()
        )
        self._client = httpx2.Client(
            base_url=base_url, transport=transport, timeout=10.0
        )

    def check_health(self) -> bool:
        observed = _observe_gateway(self._gateway_check_health, attempts=3)
        return observed if isinstance(observed, bool) else False

    def list_keys(self) -> GatewayKeyList | UnknownError:
        return _observe_gateway(self._gateway_list_keys, attempts=3)

    def create(
        self,
        name: str,
        *,
        models: list[str] | None = None,
        expires: str | None = None,
        key: str | None = None,
        request_id: str | None = None,
    ) -> GatewayKeyCreated | UnknownError:
        def create_once() -> GatewayKeyCreated | UnknownError:
            with self._mutation_claim(name) as acquired:
                if not acquired:
                    return UnknownError(
                        category=ErrorCategory.UNKNOWN,
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                self._restore_request_intent(name)
                return self._gateway_create(
                    name, models=models, expires=expires, key=key
                )

        if request_id is not None:
            receipt = self._read_receipt(self._receipt_path(name, request_id))
            if receipt is not None and (
                receipt.body.models != list(models or [])
                or receipt.body.duration != expires
                or (key is not None and receipt.body.key != key)
            ):
                raise HTTPException(
                    status_code=422, detail=InvalidRequestReason.MALFORMED
                )
        retained = self._pending_receipt(name)
        if request_id is None and retained is not None and not retained.completed:
            body = retained.body
            request_id = (
                retained.request_id
                if (
                    body.models == list(models or [])
                    and body.duration == expires
                    and (key is None or key == body.key)
                )
                else str(uuid4())
            )
        return self._mutation_request(name, request_id, create_once)

    def revoke(self, name: str) -> GatewayKeyRevoked | UnknownError:
        def revoke_once() -> GatewayKeyRevoked | UnknownError:
            with self._mutation_claim(name) as acquired:
                if not acquired:
                    raise GatewayKeyError("gateway mutation claim is busy")
                # Newest revoke includes the exact temporary alias of an
                # interrupted rotation. Keep its secret until both are absent.
                for alias in (name, _rolling_alias(name)):
                    self._gateway_delete_alias(alias)
                for alias in (name, _rolling_alias(name)):
                    receipt = self._read_receipt(self._intent_path(alias))
                    if receipt is not None and receipt.body.key is not None:
                        secret = receipt.body.key
                        code, _ = self._gateway_request(
                            "POST", "/key/delete", json=_KeyDeleteRequest(keys=[secret])
                        )
                        if code != 200 and self._gateway_key_info(secret) is not None:
                            raise GatewayKeyError(
                                "gateway revocation effect is unconfirmed"
                            )
                self._forget_intent(name, superseded=True)
                self._forget_intent(_rolling_alias(name), superseded=True)
                return GatewayKeyRevoked(name=name)

        return _observe_gateway(revoke_once, attempts=3)

    def roll(
        self, name: str, *, request_id: str | None = None
    ) -> GatewayKeyCreated | UnknownError:
        def roll_once() -> GatewayKeyCreated | UnknownError:
            with self._mutation_claim(name) as acquired:
                if not acquired:
                    return UnknownError(
                        category=ErrorCategory.UNKNOWN,
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                temporary = _rolling_alias(name)
                pending = self._pending_receipt(temporary)
                if pending is not None and pending.request_id != self._request.get():
                    # Settle the older exact replacement before staging a newer
                    # roll. Its temporary alias may be the only working key.
                    token = self._request.set(pending.request_id)
                    try:
                        self._restore_request_intent(temporary)
                        settled = self._gateway_roll(name)
                        if isinstance(settled, UnknownError):
                            return settled
                    finally:
                        self._request.reset(token)
                self._restore_request_intent(temporary)
                return self._gateway_roll(name)

        return self._mutation_request(_rolling_alias(name), request_id, roll_once)

    def _mutation_request(
        self,
        alias: str,
        request_id: str | None,
        action: Callable[[], GatewayKeyCreated | UnknownError],
    ) -> GatewayKeyCreated | UnknownError:
        # Receipt identity outlives alias-history cleanup. A new request never
        # adopts a completed operation's secret, even if cleanup failed.
        retained = self._pending_receipt(alias)
        identity = request_id or (
            retained.request_id
            if retained is not None and not retained.completed
            else str(uuid4())
        )
        token = self._request.set(identity)
        try:
            completed = self._read_receipt(self._receipt_path(alias, identity))
            if completed is not None and completed.superseded:
                return UnknownError(
                    category=ErrorCategory.UNKNOWN, reason=WaitReason.SCOPE_CHANGED
                )
            if (
                completed is not None
                and completed.completed
                and completed.body.key is not None
            ):
                return GatewayKeyCreated(
                    name=alias.removesuffix(".rolling"),
                    models=completed.body.models,
                    key=completed.body.key,
                )

            def reconcile() -> GatewayKeyCreated | UnknownError:
                receipt = self._read_receipt(self._receipt_path(alias, identity))
                if receipt is not None and receipt.superseded:
                    return UnknownError(
                        category=ErrorCategory.UNKNOWN, reason=WaitReason.SCOPE_CHANGED
                    )
                if (
                    receipt is not None
                    and receipt.completed
                    and receipt.body.key is not None
                ):
                    return GatewayKeyCreated(
                        name=alias.removesuffix(".rolling"),
                        models=receipt.body.models,
                        key=receipt.body.key,
                    )
                return action()

            return _observe_gateway(reconcile, attempts=3)
        finally:
            self._request.reset(token)

    def ensure_default(self, path: Path = DEFAULT_KEY_FILE) -> bool | UnknownError:
        def ensure_once() -> bool:
            with self._mutation_claim(DEFAULT_KEY_NAME) as acquired:
                if not acquired:
                    raise GatewayKeyError("gateway mutation claim is busy")
                return self._gateway_ensure_default(path)

        return _observe_gateway(ensure_once, attempts=3)

    def _gateway_check_health(self) -> bool:
        # The key-only Caddy relay deliberately rejects /health/readiness.
        # Probe the authenticated dependency actually used by this service.
        code, _payload = self._gateway_request(
            "GET", "/key/list", params=_KeyListParams(page=1, size=1)
        )
        return code == 200

    def close(self) -> None:
        self._client.close()

    def _gateway_request(
        self,
        method: str,
        path: str,
        *,
        json: _LiteLlmRequest | None = None,
        params: _LiteLlmRequest | None = None,
    ) -> tuple[int, object]:
        try:
            master_key = self._master_key()
        except OSError as error:
            raise GatewayKeyError("LiteLLM master key is unavailable") from error
        try:
            response = self._client.request(
                method,
                path,
                json=json.model_dump(exclude_none=True) if json is not None else None,
                params=(
                    params.model_dump(exclude_none=True) if params is not None else None
                ),
                headers={"Authorization": f"Bearer {master_key}"},
            )
        except httpx2.HTTPError as error:
            raise GatewayKeyError("LiteLLM gateway is unavailable") from error
        if response.status_code in (401, 403):
            raise HTTPException(
                status_code=response.status_code,
                detail=(
                    SecurityRefusalReason.HTTP_401
                    if response.status_code == 401
                    else SecurityRefusalReason.HTTP_403
                ),
            )
        try:
            payload: object = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            # HTTP 404 is the peer's documented absence observation. A blank
            # or unreadable success/failure reply never becomes an empty fact.
            if response.status_code == 404:
                return response.status_code, {}
            raise GatewayKeyError("gateway reply is unreadable")
        return response.status_code, payload

    def _gateway_raw_keys(self, target: str | None = None) -> list[_LiteLlmKey]:
        keys: list[_LiteLlmKey] = []
        for page in range(1, _MAX_PAGES + 1):
            code, payload = self._gateway_request(
                "GET", "/key/list", params=_KeyListParams(page=page, size=_PAGE_SIZE)
            )
            if code != 200:
                raise GatewayKeyError(f"LiteLLM refused to list keys (HTTP {code})")
            reply = _KeyListReply.model_validate(payload)
            if reply.keys is None:
                raise GatewayKeyError("LiteLLM returned a malformed key list")
            for raw in reply.keys:
                if not isinstance(raw, dict) or not raw.get("key_alias"):
                    continue
                try:
                    item = _LiteLlmKey.model_validate(raw)
                    _remaining(item.expires)
                except (ValueError, GatewayKeyError) as error:
                    if raw.get("key_alias") == target:
                        raise GatewayKeyError(
                            "gateway target alias observation is unavailable"
                        ) from error
                    continue
                keys.append(item)
            if reply.total_pages is None or page >= reply.total_pages:
                break
        return keys

    def _gateway_list_keys(self) -> GatewayKeyList:
        views = (_view(item) for item in self._gateway_raw_keys())
        return GatewayKeyList(
            keys=sorted(
                (view for view in views if view is not None), key=lambda v: v.name
            )
        )

    def _gateway_alias_info(self, name: str) -> _LiteLlmKey | None:
        item = next(
            (item for item in self._gateway_raw_keys(name) if item.key_alias == name),
            None,
        )
        if item is None:
            return None
        receipt = self._read_receipt(self._intent_path(name))
        secret = item.token or (receipt.body.key if receipt is not None else None)
        if secret is None:
            raise GatewayKeyError("gateway exact alias identity is unavailable")
        info = self._gateway_key_info(secret)
        if info is None or info.key_alias != name:
            raise GatewayKeyError("gateway alias observation changed")
        if "expires" not in info.model_fields_set:
            raise GatewayKeyError("gateway key expiry observation is unavailable")
        return info.model_copy(update={"token": secret})

    def _gateway_exists(self, name: str) -> bool:
        return any(item.key_alias == name for item in self._gateway_raw_keys(name))

    def _gateway_create(
        self,
        name: str,
        *,
        models: list[str] | None = None,
        expires: str | None = None,
        key: str | None = None,
    ) -> GatewayKeyCreated:
        created = self._gateway_create_under(
            name, models=models, expires=expires, key=key
        )
        rolling = _rolling_alias(name)
        if self._intent_path(rolling).exists():
            # The newer create supersedes the unfinished rotation. Retain its
            # own receipt until the obsolete remote alias is reconciled.
            if self._gateway_exists(rolling):
                self._gateway_delete_alias(rolling)
            self._forget_intent(rolling, superseded=True)
        self._forget_intent(name)
        return created

    def _gateway_create_under(
        self,
        name: str,
        *,
        models: list[str] | None = None,
        expires: str | None = None,
        key: str | None = None,
    ) -> GatewayKeyCreated:
        body = self._read_intent(name)
        replacement = body is not None and (
            body.models != list(models or [])
            or body.duration != expires
            or (key is not None and key != body.key)
        )
        if replacement:
            body = None
        if body is not None:
            observed = self._gateway_observe_created(body)
            if observed is not None:
                return observed
        else:
            body = _KeyGenerateRequest(
                key_alias=name,
                models=list(models or []),
                allowed_routes=list(_KEY_ROUTES),
                metadata=_KeyMetadata(),
                duration=expires,
                key=key if key is not None else "sk-" + secrets.token_urlsafe(32),
            )
            try:
                self._intent_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._intent_root.chmod(0o700)
                identity = self._request.get() or str(uuid4())
                prior = self._read_receipt(self._intent_path(name))
                if (
                    prior is not None
                    and prior.request_id != identity
                    and not prior.completed
                ):
                    self._forget_intent(name, superseded=True)
                receipt = GatewayMutationReceipt(request_id=identity, body=body)
                _write_private(
                    self._receipt_path(name, identity), receipt.model_dump_json()
                )
                _write_private(self._intent_path(name), receipt.model_dump_json())
            except OSError as error:
                raise GatewayKeyError(
                    "gateway mutation persistence is unavailable"
                ) from error
        # A persisted unresolved create authorizes replacing an alias whose
        # secret differs from this exact request. Same-key effects returned
        # above are adopted, never deleted or regenerated.
        if self._gateway_exists(name):
            self._gateway_delete_alias(name)
        try:
            code, payload = self._gateway_request("POST", "/key/generate", json=body)
        except GatewayKeyError:
            observed = self._gateway_observe_created(body)
            if observed is not None:
                return observed
            raise GatewayKeyError(
                "gateway key creation effect is unconfirmed"
            ) from None
        reply = _KeyGenerateReply.model_validate(payload)
        secret = reply.key
        if secret is not None and secret != body.key:
            raise HTTPException(
                status_code=502, detail=SecurityRefusalReason.AGENT_IDENTITY_MISMATCH
            )
        if code not in (200, 201) or secret is None or reply.models != body.models:
            observed = self._gateway_observe_created(body)
            if observed is not None:
                return observed
            raise GatewayKeyError(f"LiteLLM refused to create the key (HTTP {code})")
        # The request's own facts fill what LiteLLM's answer leaves out.
        described = _LiteLlmKey(
            key_alias=reply.key_alias or body.key_alias,
            key_name=reply.key_name,
            models=reply.models if "models" in reply.model_fields_set else body.models,
            created_at=reply.created_at,
            expires=reply.expires,
            last_active=reply.last_active,
        )
        view = _view(described) or GatewayKeyView(name=name, models=[])
        return GatewayKeyCreated(**view.model_dump(), key=secret)

    def _gateway_key_info(self, secret: str) -> _LiteLlmKey | None:
        code, payload = self._gateway_request(
            "GET", "/key/info", params=_KeyInfoParams(key=secret)
        )
        if code == 404:
            return None
        info = _KeyInfoReply.model_validate(payload).info
        if code != 200 or info is None or info.key_alias is None:
            raise GatewayKeyError("gateway exact key observation is unavailable")
        return info

    def _gateway_observe_created(
        self, body: _KeyGenerateRequest
    ) -> GatewayKeyCreated | None:
        if body.key is None:
            raise GatewayKeyError("gateway retained secret observation is unavailable")
        info = self._gateway_key_info(body.key)
        if info is None or info.key_alias != body.key_alias:
            return None
        if info.models != body.models:
            raise GatewayKeyError("gateway key scope observation differs")
        view = _view(info)
        if view is None:
            raise GatewayKeyError("gateway key description observation is unavailable")
        return GatewayKeyCreated(**view.model_dump(), key=body.key)

    def _gateway_delete_alias(self, name: str) -> None:
        try:
            code, _ = self._gateway_request(
                "POST", "/key/delete", json=_KeyDeleteRequest(key_aliases=[name])
            )
        except GatewayKeyError:
            if not self._gateway_exists(name):
                return
            raise
        if self._gateway_exists(name):
            raise GatewayKeyError(f"LiteLLM refused to revoke the key (HTTP {code})")

    def _gateway_revoke(self, name: str) -> GatewayKeyRevoked:
        if self._gateway_exists(name):
            self._gateway_delete_alias(name)
        return GatewayKeyRevoked(name=name)

    @contextmanager
    def _mutation_claim(self, name: str) -> Iterator[bool]:
        descriptor: int | None = None
        acquired = False
        try:
            if self._intent_root.is_symlink():
                yield False
                return
            self._intent_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._intent_root.chmod(0o700)
            descriptor = os.open(
                self._intent_path(name).with_suffix(".lock"),
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _intent_path(self, name: str) -> Path:
        return self._intent_root / (hashlib.sha256(name.encode()).hexdigest() + ".json")

    def _receipt_path(self, name: str, request_id: str) -> Path:
        return self._intent_root / (
            hashlib.sha256((name + ":" + request_id).encode()).hexdigest() + ".receipt"
        )

    @staticmethod
    def _read_receipt(path: Path) -> GatewayMutationReceipt | None:
        try:
            if path.is_symlink():
                return None
            receipt = GatewayMutationReceipt.model_validate_json(path.read_bytes())
            body = receipt.body
            if (
                body.key is None
                or _KEY_PATTERN.fullmatch(body.key) is None
                or body.allowed_routes != _KEY_ROUTES
                or body.metadata != _KeyMetadata()
            ):
                return None
            return receipt
        except (OSError, ValueError):
            return None

    def _pending_receipt(self, alias: str) -> GatewayMutationReceipt | None:
        receipt = self._read_receipt(self._intent_path(alias))
        if receipt is None or receipt.completed or receipt.superseded:
            return None
        archived = self._read_receipt(self._receipt_path(alias, receipt.request_id))
        return (
            None
            if archived is not None and (archived.completed or archived.superseded)
            else receipt
        )

    def _restore_request_intent(self, name: str) -> None:
        identity = self._request.get()
        if identity is None:
            return
        retained = self._read_receipt(self._receipt_path(name, identity))
        if retained is not None and not retained.completed and not retained.superseded:
            _write_private(self._intent_path(name), retained.model_dump_json())

    def _forget_intent(self, name: str, *, superseded: bool = False) -> None:
        receipt = self._read_receipt(self._intent_path(name))
        if receipt is None:
            return
        completed = GatewayMutationReceipt(
            request_id=receipt.request_id,
            body=receipt.body,
            completed=True,
            superseded=superseded,
        )
        # Atomic replace plus directory fsync retires the active receipt before
        # any optional cleanup. An unlink failure cannot resurrect its effect.
        archived = self._read_receipt(self._receipt_path(name, receipt.request_id))
        if archived is None or not archived.completed:
            _write_private(
                self._receipt_path(name, receipt.request_id),
                completed.model_dump_json(),
            )
        _write_private(self._intent_path(name), completed.model_dump_json())

    def _read_intent(self, name: str) -> _KeyGenerateRequest | None:
        receipt = self._read_receipt(self._intent_path(name))
        if receipt is None or receipt.completed:
            return None
        if (
            self._request.get() is not None
            and receipt.request_id != self._request.get()
        ):
            return None
        body = receipt.body
        if (
            body.key_alias != name
            or body.key is None
            or _KEY_PATTERN.fullmatch(body.key) is None
            or body.allowed_routes != _KEY_ROUTES
            or body.metadata != _KeyMetadata()
        ):
            return None
        return body

    def _gateway_roll(self, name: str) -> GatewayKeyCreated | UnknownError:
        """Resume the exact replacement secret before observing mutable alias state."""
        temporary = _rolling_alias(name)
        pending = self._read_intent(temporary)
        if pending is not None:
            renamed = self._gateway_observe_created(
                pending.model_copy(update={"key_alias": name})
            )
            if renamed is not None:
                self._forget_intent(temporary)
                return renamed
        current = self._gateway_alias_info(name)
        if pending is None and current is None:
            # Damaged local recovery metadata is a miss. A surviving temporary
            # key supplies observable scope for a fresh authorized replacement.
            current = self._gateway_alias_info(temporary)
        if pending is None and current is None:
            return UnknownError(
                category=ErrorCategory.UNKNOWN, reason=WaitReason.SCOPE_CHANGED
            )
        created = self._gateway_create_under(
            temporary,
            models=pending.models
            if pending is not None
            else current.models
            if current is not None
            else None,
            expires=pending.duration
            if pending is not None
            else _remaining(current.expires)
            if current is not None
            else None,
        )
        self._forget_intent(name, superseded=True)
        if self._gateway_exists(name):
            self._gateway_delete_alias(name)
        _code, _ = self._gateway_request(
            "POST",
            "/key/update",
            json=_KeyUpdateRequest(key=created.key, key_alias=name),
        )
        observed = self._gateway_observe_created(
            _KeyGenerateRequest(
                key_alias=name,
                key=created.key,
                models=created.models,
                allowed_routes=list(_KEY_ROUTES),
                metadata=_KeyMetadata(),
            )
        )
        if observed is None:
            raise GatewayKeyError("gateway rename effect is unconfirmed")
        self._forget_intent(temporary)
        return created.model_copy(update={"name": name})

    def _gateway_ensure_default(self, path: Path = DEFAULT_KEY_FILE) -> bool:
        """Keep a working `default` key whose secret is in `path`.

        The file is written first, so an interrupted attempt registers the same
        key next time. A `default` key LiteLLM holds but the file does not
        match is replaced. Returns whether anything changed.
        """
        key = _read_key(path)
        if key is None:
            key = "sk-" + secrets.token_urlsafe(32)
            _write_private(path, key + "\n")
        code, payload = self._gateway_request(
            "GET", "/key/info", params=_KeyInfoParams(key=key)
        )
        info = _KeyInfoReply.model_validate(payload).info
        if code == 200 and info is not None and info.key_alias == DEFAULT_KEY_NAME:
            return False
        if code != 404:
            raise GatewayKeyError("default gateway key observation is unavailable")
        if self._gateway_exists(DEFAULT_KEY_NAME):
            self._gateway_revoke(DEFAULT_KEY_NAME)
        self._gateway_create(DEFAULT_KEY_NAME, key=key)
        return True


async def keep_default_key(
    service: GatewayKeyService,
    stop: asyncio.Event,
    *,
    path: Path = DEFAULT_KEY_FILE,
    first_delay: float = 5.0,
    maximum_delay: float = 300.0,
) -> None:
    """End this startup observation after six attempts.

    Periodic production reconciliation starts independent bounded observations
    of the standing default-key intent after this startup attempt ends.
    """

    delay = first_delay
    for _attempt in range(6):
        if stop.is_set():
            return
        try:
            observed = await asyncio.to_thread(lambda: service.ensure_default(path))
            if not isinstance(observed, UnknownError):
                if observed:
                    _LOGGER.info("default gateway client key is ready")
                return
            _LOGGER.warning(
                "default gateway client key observation: %s", observed.reason
            )
        except HTTPException as error:
            if error.status_code in (
                status.HTTP_401_UNAUTHORIZED,
                status.HTTP_403_FORBIDDEN,
            ):
                _LOGGER.error(
                    "gateway key authority refused (HTTP %s)", error.status_code
                )
                return
            _LOGGER.warning("gateway key dependency is unavailable")
        except (GatewayKeyError, OSError) as error:
            _LOGGER.warning(
                "default gateway client key not ready: %s; retrying in %s seconds",
                error,
                delay,
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            delay = min(delay * 2, maximum_delay)


def _read_key(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise GatewayKeyError("default secret observation is unavailable") from error
    return value if _KEY_PATTERN.fullmatch(value) else None


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".client-key-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install_gateway_key_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    service: GatewayKeyService | None,
) -> None:
    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS[("get", _KEY_PATH)] = "listGatewayKeys"
    _ADMIN_OPERATION_IDS[("post", _KEY_PATH)] = "createGatewayKey"
    _ADMIN_OPERATION_IDS[("post", _REVOKE_PATH)] = "revokeGatewayKey"
    _ADMIN_OPERATION_IDS[("post", _ROLL_PATH)] = "rollGatewayKey"

    def authorize(actor: Actor, path: str) -> None:
        if actor.role not in MUTATION_ROLES[("POST", path)]:
            raise HTTPException(status_code=403, detail=SecurityRefusalReason.FORBIDDEN)

    @app.get(
        _KEY_PATH,
        response_model=GatewayKeyList | UnknownError,
        responses=bounded_error_responses(401, 403, 503),
        operation_id="listGatewayKeys",
    )
    def list_gateway_keys(
        actor: Actor = actor_dependency,
    ) -> GatewayKeyList | UnknownError:
        if service is None:
            return UnknownError(
                category=ErrorCategory.UNKNOWN,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        return _observe_gateway(service.list_keys)

    @app.post(
        _KEY_PATH,
        response_model=GatewayKeyCreated | UnknownError,
        responses={
            202: {"model": UnknownError},
            **bounded_error_responses(401, 403, 422, 502, 503),
        },
        status_code=status.HTTP_201_CREATED,
        operation_id="createGatewayKey",
    )
    def create_gateway_key(
        body: GatewayKeyCreateRequest,
        response: Response,
        actor: Actor = actor_dependency,
    ) -> GatewayKeyCreated | UnknownError:
        authorize(actor, _KEY_PATH)
        if service is None:
            response.status_code = status.HTTP_202_ACCEPTED
            return UnknownError(
                category=ErrorCategory.UNKNOWN,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        available: GatewayKeyService = service
        result = _observe_gateway(
            lambda: available.create(
                body.name,
                models=body.models,
                expires=body.expires,
                request_id=body.request_id,
            )
        )
        if isinstance(result, UnknownError):
            response.status_code = status.HTTP_202_ACCEPTED
        return result

    @app.post(
        _REVOKE_PATH,
        response_model=GatewayKeyRevoked | UnknownError,
        responses=bounded_error_responses(401, 403, 502, 503),
        operation_id="revokeGatewayKey",
    )
    def revoke_gateway_key(
        name: str = PathParameter(pattern=_NAME_PATTERN, max_length=63),
        actor: Actor = actor_dependency,
    ) -> GatewayKeyRevoked | UnknownError:
        authorize(actor, _REVOKE_PATH)
        if service is None:
            return UnknownError(
                category=ErrorCategory.UNKNOWN,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        available: GatewayKeyService = service
        return _observe_gateway(lambda: available.revoke(name))

    @app.post(
        _ROLL_PATH,
        response_model=GatewayKeyCreated | UnknownError,
        responses=bounded_error_responses(401, 403, 502, 503),
        operation_id="rollGatewayKey",
    )
    def roll_gateway_key(
        name: str = PathParameter(pattern=_NAME_PATTERN, max_length=63),
        request_id: str | None = None,
        actor: Actor = actor_dependency,
    ) -> GatewayKeyCreated | UnknownError:
        authorize(actor, _ROLL_PATH)
        if service is None:
            return UnknownError(
                category=ErrorCategory.UNKNOWN,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        available: GatewayKeyService = service
        return _observe_gateway(lambda: available.roll(name, request_id=request_id))
