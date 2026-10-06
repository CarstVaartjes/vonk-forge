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
import logging
import os
import re
import secrets
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx2
from fastapi import FastAPI, HTTPException, status
from fastapi import Path as PathParameter
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from vonk_agent_protocol import UnknownOutcomeError

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

    An unknown outcome, never a refusal of the caller: the gateway may simply not
    be up yet.  ``keep_default_key`` retries the startup path on a doubling
    delay, and an operator request that meets it is answered 503 to be asked
    again.
    """


class GatewayKeyConflict(ValueError):
    pass


class GatewayKeyCreateRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

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
    """The only response that carries the key. It is not shown again."""

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


class _KeyDeleteRequest(_LiteLlmRequest):
    key_aliases: list[str]


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


def _loose_strings(value: object) -> list[str]:
    return (
        [item for item in value if isinstance(item, str)]
        if isinstance(value, list)
        else []
    )


def _loose_keys(value: object) -> list[object]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _loose_count(value: object) -> int | None:
    return value if type(value) is int else None


# LiteLLM is a foreign service: a field of the wrong type reads as absent, never
# as a reason to refuse the whole answer.
_LooseText = Annotated[str | None, BeforeValidator(_loose_text)]
_LooseStrings = Annotated[list[str], BeforeValidator(_loose_strings)]
_LooseCount = Annotated[int | None, BeforeValidator(_loose_count)]


class _LiteLlmKey(_LiteLlmReply):
    """One virtual key as LiteLLM describes it; any field may be absent or null."""

    key_alias: _LooseText = None
    key_name: _LooseText = None
    models: _LooseStrings = Field(default_factory=list)
    created_at: _LooseText = None
    expires: _LooseText = None
    last_active: _LooseText = None


class _KeyListReply(_LiteLlmReply):
    keys: list[_LiteLlmKey] | None = Field(default=None)
    total_pages: _LooseCount = None

    @field_validator("keys", mode="before")
    @classmethod
    def _only_objects(cls, value: object) -> object:
        return _loose_keys(value) if isinstance(value, list) else None


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
    except ValueError:
        return None
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

    def __init__(
        self,
        *,
        base_url: str = LITELLM_KEY_ADMIN_URL,
        master_key: Callable[[], str] | None = None,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._master_key = master_key or (
            lambda: MASTER_KEY_FILE.read_text(encoding="utf-8").strip()
        )
        self._client = httpx2.Client(
            base_url=base_url, transport=transport, timeout=10.0
        )

    def _request(
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
        try:
            payload: object = response.json() if response.content else {}
        except ValueError:
            payload = {}
        return response.status_code, payload if isinstance(payload, dict) else {}

    def _raw_keys(self) -> list[_LiteLlmKey]:
        keys: list[_LiteLlmKey] = []
        for page in range(1, _MAX_PAGES + 1):
            code, payload = self._request(
                "GET",
                "/key/list",
                params=_KeyListParams(page=page, size=_PAGE_SIZE),
            )
            if code != 200:
                raise GatewayKeyError(f"LiteLLM refused to list keys (HTTP {code})")
            reply = _KeyListReply.model_validate(payload)
            if reply.keys is None:
                raise GatewayKeyError("LiteLLM returned a malformed key list")
            keys.extend(reply.keys)
            if reply.total_pages is None or page >= reply.total_pages:
                break
        return keys

    def list_keys(self) -> GatewayKeyList:
        views = (_view(item) for item in self._raw_keys())
        return GatewayKeyList(
            keys=sorted(
                (view for view in views if view is not None), key=lambda v: v.name
            )
        )

    def _exists(self, name: str) -> bool:
        return any(item.key_alias == name for item in self._raw_keys())

    def create(
        self,
        name: str,
        *,
        models: list[str] | None = None,
        expires: str | None = None,
        key: str | None = None,
    ) -> GatewayKeyCreated:
        if self._exists(name):
            raise GatewayKeyConflict(
                f"key {name} already exists; revoke it first or choose another name"
            )
        return self._create_under(name, models=models, expires=expires, key=key)

    def _create_under(
        self,
        name: str,
        *,
        models: list[str] | None = None,
        expires: str | None = None,
        key: str | None = None,
    ) -> GatewayKeyCreated:
        body = _KeyGenerateRequest(
            key_alias=name,
            models=list(models or []),
            allowed_routes=list(_KEY_ROUTES),
            metadata=_KeyMetadata(),
            duration=expires,
            key=key,
        )
        code, payload = self._request("POST", "/key/generate", json=body)
        reply = _KeyGenerateReply.model_validate(payload)
        secret = reply.key
        if code not in (200, 201) or secret is None:
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

    def _delete_alias(self, name: str) -> None:
        code, _ = self._request(
            "POST", "/key/delete", json=_KeyDeleteRequest(key_aliases=[name])
        )
        if code != 200:
            raise GatewayKeyError(f"LiteLLM refused to revoke the key (HTTP {code})")

    def revoke(self, name: str) -> GatewayKeyRevoked:
        if not self._exists(name):
            raise KeyError(name)
        self._delete_alias(name)
        return GatewayKeyRevoked(name=name)

    def roll(self, name: str) -> GatewayKeyCreated:
        """Replace one key's secret, keeping its name and model list.

        LiteLLM cannot rotate a secret in place, so the new key is created
        first under a temporary alias; only once it exists is the old key
        deleted and the new one renamed to `name`. A failed create leaves the
        old key untouched and working. An unexpired key keeps its remaining
        lifetime.
        """
        current = next(
            (item for item in self._raw_keys() if item.key_alias == name), None
        )
        if current is None:
            raise KeyError(name)
        view = _view(current)
        models = view.models if view is not None else []
        temporary = _rolling_alias(name)
        if self._exists(temporary):
            # An earlier roll stopped between its steps; its leftover is not
            # in use by anyone, so it is replaced.
            self._delete_alias(temporary)
        created = self._create_under(
            temporary,
            models=models,
            expires=_remaining(current.expires),
        )
        self._delete_alias(name)
        code, _ = self._request(
            "POST",
            "/key/update",
            json=_KeyUpdateRequest(key=created.key, key_alias=name),
        )
        if code != 200:
            # The new key works; it keeps the temporary alias until the next
            # roll instead of losing the only copy of its secret.
            _LOGGER.warning(
                "rolled gateway key %s could not be renamed (HTTP %s); "
                "it is listed as %s",
                name,
                code,
                temporary,
            )
            return created
        return created.model_copy(update={"name": name})

    def ensure_default(self, path: Path = DEFAULT_KEY_FILE) -> bool:
        """Keep a working `default` key whose secret is in `path`.

        The file is written first, so an interrupted attempt registers the same
        key next time. A `default` key LiteLLM holds but the file does not
        match is replaced. Returns whether anything changed.
        """
        key = _read_key(path)
        if key is None:
            key = "sk-" + secrets.token_urlsafe(32)
            _write_private(path, key + "\n")
        code, payload = self._request(
            "GET", "/key/info", params=_KeyInfoParams(key=key)
        )
        info = _KeyInfoReply.model_validate(payload).info
        if code == 200 and info is not None and info.key_alias == DEFAULT_KEY_NAME:
            return False
        if self._exists(DEFAULT_KEY_NAME):
            self.revoke(DEFAULT_KEY_NAME)
        self.create(DEFAULT_KEY_NAME, key=key)
        return True


async def keep_default_key(
    service: GatewayKeyService,
    stop: asyncio.Event,
    *,
    path: Path = DEFAULT_KEY_FILE,
    first_delay: float = 5.0,
    maximum_delay: float = 300.0,
) -> None:
    """Retry until the default client key exists and matches its secrets file.

    LiteLLM starts after the API, so the first attempts may find no gateway.  An
    attempt that cannot reach it or settle with it is unknown, not failed: the
    delay doubles up to ``maximum_delay`` and the key is asked for again until it
    is ready or ``stop`` is set.
    """

    delay = first_delay
    while not stop.is_set():
        try:
            if await asyncio.to_thread(lambda: service.ensure_default(path)):
                _LOGGER.info("default gateway client key is ready")
            return
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
    except (OSError, UnicodeError):
        return None
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

    def available() -> GatewayKeyService:
        if service is None:
            raise HTTPException(status_code=503, detail="Gateway keys unavailable")
        return service

    def authorize(actor: Actor, path: str) -> None:
        if actor.role not in MUTATION_ROLES[("POST", path)]:
            raise HTTPException(status_code=403, detail="insufficient role")

    def unavailable(error: GatewayKeyError) -> HTTPException:
        return HTTPException(status_code=503, detail=str(error))

    @app.get(
        _KEY_PATH,
        response_model=GatewayKeyList,
        responses=bounded_error_responses(401, 403, 503),
        operation_id="listGatewayKeys",
    )
    def list_gateway_keys(actor: Actor = actor_dependency) -> GatewayKeyList:
        try:
            return available().list_keys()
        except GatewayKeyError as error:
            raise unavailable(error) from None

    @app.post(
        _KEY_PATH,
        response_model=GatewayKeyCreated,
        responses=bounded_error_responses(401, 403, 409, 422, 503),
        status_code=status.HTTP_201_CREATED,
        operation_id="createGatewayKey",
    )
    def create_gateway_key(
        body: GatewayKeyCreateRequest, actor: Actor = actor_dependency
    ) -> GatewayKeyCreated:
        authorize(actor, _KEY_PATH)
        try:
            return available().create(
                body.name, models=body.models, expires=body.expires
            )
        except GatewayKeyConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except GatewayKeyError as error:
            raise unavailable(error) from None

    @app.post(
        _REVOKE_PATH,
        response_model=GatewayKeyRevoked,
        responses=bounded_error_responses(401, 403, 404, 503),
        operation_id="revokeGatewayKey",
    )
    def revoke_gateway_key(
        name: str = PathParameter(pattern=_NAME_PATTERN, max_length=63),
        actor: Actor = actor_dependency,
    ) -> GatewayKeyRevoked:
        authorize(actor, _REVOKE_PATH)
        try:
            return available().revoke(name)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"no gateway key named {name}"
            ) from None
        except GatewayKeyError as error:
            raise unavailable(error) from None

    @app.post(
        _ROLL_PATH,
        response_model=GatewayKeyCreated,
        responses=bounded_error_responses(401, 403, 404, 503),
        operation_id="rollGatewayKey",
    )
    def roll_gateway_key(
        name: str = PathParameter(pattern=_NAME_PATTERN, max_length=63),
        actor: Actor = actor_dependency,
    ) -> GatewayKeyCreated:
        authorize(actor, _ROLL_PATH)
        try:
            return available().roll(name)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"no gateway key named {name}"
            ) from None
        except GatewayKeyError as error:
            raise unavailable(error) from None
