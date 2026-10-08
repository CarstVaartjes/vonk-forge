"""Gateway client keys: the Controller drives LiteLLM's key admin API."""

import json
import stat

import httpx2
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from vonk_agent_protocol import ErrorCategory, UnknownError, WaitReason
from vonk_control.auth import Actor
from vonk_control.gateway_keys import GatewayKeyService, install_gateway_key_routes

MASTER = "sk-master-" + "m" * 32


class FakeLiteLlm:
    """LiteLLM's /key/* routes, keyed by alias, requiring the master key."""

    def __init__(self) -> None:
        self.keys: dict[str, dict] = {}
        self.counter = 0
        self.fail_generate_after: int | None = None

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == f"Bearer {MASTER}"
        path = request.url.path
        if path == "/key/list":
            page = int(request.url.params["page"])
            items = [
                {k: v for k, v in item.items() if k != "key"}
                for item in self.keys.values()
            ]
            # One key per page exercises pagination.
            return httpx2.Response(
                200,
                json={
                    "keys": items[page - 1 : page],
                    "total_pages": max(1, len(items)),
                },
            )
        if path == "/key/info":
            key = request.url.params["key"]
            for item in self.keys.values():
                if item["key"] == key:
                    return httpx2.Response(200, json={"key": key, "info": item})
            return httpx2.Response(404, json={"error": "not found"})
        body = json.loads(request.content)
        if path == "/key/generate":
            if (
                self.fail_generate_after is not None
                and self.counter >= self.fail_generate_after
            ):
                return httpx2.Response(500, json={"error": "database unavailable"})
            self.counter += 1
            key = body.get("key") or f"sk-generated-{self.counter:032d}"
            self.keys[body["key_alias"]] = {
                "key": key,
                "key_alias": body["key_alias"],
                "models": body["models"],
                "allowed_routes": body["allowed_routes"],
                "expires": "2026-10-28T00:00:00Z" if body.get("duration") else None,
                "created_at": "2026-09-28T00:00:00Z",
            }
            return httpx2.Response(200, json=self.keys[body["key_alias"]])
        if path == "/key/update":
            for alias, item in list(self.keys.items()):
                if item["key"] == body["key"]:
                    del self.keys[alias]
                    self.keys[body["key_alias"]] = {
                        **item,
                        "key_alias": body["key_alias"],
                    }
                    return httpx2.Response(200, json={"key": body["key"]})
            return httpx2.Response(404)
        if path == "/key/delete":
            for alias in body["key_aliases"]:
                del self.keys[alias]
            return httpx2.Response(200, json={"deleted_keys": body["key_aliases"]})
        return httpx2.Response(404)


def _service(litellm: FakeLiteLlm) -> GatewayKeyService:
    return GatewayKeyService(
        base_url="http://litellm.test",
        master_key=lambda: MASTER,
        transport=httpx2.MockTransport(litellm.handle),
    )


def _client(service, role="administrator") -> TestClient:
    app = FastAPI()
    install_gateway_key_routes(
        app,
        actor_dependency=Depends(lambda: Actor("test", role)),
        service=service,
    )
    return TestClient(app)


def test_roll_replaces_the_secret_and_keeps_name_and_models():
    litellm = FakeLiteLlm()
    client = _client(_service(litellm))
    first = client.post("/api/key", json={"name": "ci", "models": ["qwen"]}).json()
    rolled = client.post("/api/key/ci/roll")
    assert rolled.status_code == 200
    body = rolled.json()
    assert body["name"] == "ci" and body["models"] == ["qwen"]
    assert body["key"] != first["key"]
    assert [item["name"] for item in client.get("/api/key").json()["keys"]] == ["ci"]
    assert (
        UnknownError.model_validate(client.post("/api/key/missing/roll").json()).reason
        is WaitReason.SCOPE_CHANGED
    )


def test_roll_that_fails_to_create_keeps_the_old_key_working():
    litellm = FakeLiteLlm()
    client = _client(_service(litellm))
    first = client.post("/api/key", json={"name": "ci", "models": ["qwen"]}).json()
    litellm.fail_generate_after = litellm.counter

    assert (
        UnknownError.model_validate(client.post("/api/key/ci/roll").json()).reason
        is WaitReason.OBSERVATION_UNAVAILABLE
    )

    assert list(litellm.keys) == ["ci"]
    assert litellm.keys["ci"]["key"] == first["key"]


def test_create_list_and_revoke_client_keys():
    litellm = FakeLiteLlm()
    client = _client(_service(litellm))

    created = client.post("/api/key", json={"name": "laptop"})
    assert created.status_code == 201, created.text
    secret = created.json()["key"]
    assert secret == litellm.keys["laptop"]["key"]
    # No model list means every gateway model, and only inference routes.
    assert litellm.keys["laptop"]["models"] == []
    assert litellm.keys["laptop"]["allowed_routes"] == ["openai_routes"]
    assert created.json()["expires_at"] is None

    scoped = client.post(
        "/api/key", json={"name": "ci", "models": ["qwen"], "expires": "30d"}
    )
    assert scoped.status_code == 201, scoped.text
    assert scoped.json()["models"] == ["qwen"]
    assert scoped.json()["expires_at"] == "2026-10-28T00:00:00Z"

    assert client.post("/api/key", json={"name": "ci"}).status_code == 409

    listed = client.get("/api/key")
    assert listed.status_code == 200
    assert [key["name"] for key in listed.json()["keys"]] == ["ci", "laptop"]
    assert secret not in listed.text

    assert client.post("/api/key/ci/revoke").json() == {"name": "ci"}
    assert set(litellm.keys) == {"laptop"}
    assert (
        UnknownError.model_validate(client.post("/api/key/ci/revoke").json()).reason
        is WaitReason.SCOPE_CHANGED
    )


def test_only_administrators_create_or_revoke_keys():
    client = _client(_service(FakeLiteLlm()), role="operator")
    assert client.post("/api/key", json={"name": "x"}).status_code == 403
    assert client.post("/api/key/x/revoke").status_code == 403
    assert client.post("/api/key/x/roll").status_code == 403
    assert client.get("/api/key").status_code == 200


def test_unreachable_litellm_returns_bounded_typed_observation():
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    service = GatewayKeyService(
        base_url="http://litellm.test",
        master_key=lambda: MASTER,
        transport=httpx2.MockTransport(refuse),
    )
    response = _client(service).get("/api/key")
    assert response.status_code == 200
    assert (
        UnknownError.model_validate(response.json()).category is ErrorCategory.UNKNOWN
    )
    assert MASTER not in response.text


def test_default_key_is_created_once_and_heals(tmp_path):
    litellm = FakeLiteLlm()
    service = _service(litellm)
    path = tmp_path / "gateway" / "client-key"

    assert service.ensure_default(path) is True
    secret = path.read_text().strip()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert litellm.keys["default"]["key"] == secret

    assert service.ensure_default(path) is False

    # A revoked default comes back with the same secret on the next start.
    service.revoke("default")
    assert service.ensure_default(path) is True
    assert litellm.keys["default"]["key"] == secret

    # A lost file replaces the default key whose secret nobody has.
    path.unlink()
    assert service.ensure_default(path) is True
    assert litellm.keys["default"]["key"] == path.read_text().strip() != secret


def test_the_default_key_is_asked_for_again_until_the_gateway_answers() -> None:
    import asyncio

    from vonk_control.gateway_keys import GatewayKeyError, keep_default_key

    class _Service:
        def __init__(self) -> None:
            self.attempts = 0

        def ensure_default(self, path: object) -> bool:
            self.attempts += 1
            if self.attempts < 3:
                raise GatewayKeyError("LiteLLM gateway is unavailable")
            return True

    service = _Service()

    async def run() -> None:
        await asyncio.wait_for(
            keep_default_key(
                service,  # type: ignore[arg-type]
                asyncio.Event(),
                first_delay=0.001,
                maximum_delay=0.002,
            ),
            timeout=5,
        )

    asyncio.run(run())
    assert service.attempts == 3


@pytest.mark.parametrize("failure", ["lost", "invalid", "http-error"])
def test_lost_create_reply_reconciles_exact_secret_without_replaying_effect(failure):
    """Catches abandoning an executed create or generating a second secret on retry."""
    litellm = FakeLiteLlm()
    calls = []

    def lost_reply(request):
        response = litellm.handle(request)
        if request.url.path == "/key/generate":
            calls.append(request)
            if failure == "lost":
                raise httpx2.ReadError("reply lost", request=request)
            return httpx2.Response(500 if failure == "http-error" else 200, json={})
        return response

    service = GatewayKeyService(
        master_key=lambda: MASTER, transport=httpx2.MockTransport(lost_reply)
    )
    from vonk_control.gateway_keys import GatewayKeyCreated

    created = service.create("first")
    assert isinstance(created, GatewayKeyCreated)
    assert created.key == litellm.keys["first"]["key"]
    assert len(calls) == 1
    assert not isinstance(service.list_keys(), UnknownError)


def test_unknown_create_ends_without_gate_and_a_fresh_create_is_admitted():
    """Catches returning 503 or keeping a busy alias after a failed create attempt."""
    from dataclasses import replace

    from vonk_agent_protocol import AgentOperation, LifecycleState
    from vonk_control.gateway_keys import GatewayKeyCreated
    from vonk_control.lifecycle.types import Lifecycle

    from .non_blocking import assert_ended_without_blocking

    litellm = FakeLiteLlm()
    litellm.fail_generate_after = 0
    service = _service(litellm)
    observed = []

    def end(operation):
        result = service.create("first")
        assert isinstance(result, UnknownError)
        observed.append(result)
        litellm.fail_generate_after = None
        return replace(operation, state=LifecycleState.FAILED)

    def fresh(_world):
        created = service.create("first")
        assert isinstance(created, GatewayKeyCreated)
        assert created.key == litellm.keys["first"]["key"]
        return Lifecycle(
            id="fresh", kind=AgentOperation.RECIPE_START, state=LifecycleState.SUCCEEDED
        )

    def assert_released():
        assert not isinstance(service.list_keys(), UnknownError)

    def assert_reason(_operation):
        assert observed[0].reason is WaitReason.OBSERVATION_UNAVAILABLE

    assert_ended_without_blocking(
        service,
        Lifecycle(id="first", kind=AgentOperation.RECIPE_START),
        end=end,
        fresh=fresh,
        request_key=lambda operation: operation.id,
        assert_released=assert_released,
        assert_reason=assert_reason,
    )


def test_gateway_denial_is_not_softened_to_bookkeeping_unknown():
    """Catches adopting a credential refusal into the normal uncertainty result."""
    denied = [True]
    litellm = FakeLiteLlm()

    def handle(request):
        return httpx2.Response(403) if denied[0] else litellm.handle(request)

    service = GatewayKeyService(
        master_key=lambda: MASTER, transport=httpx2.MockTransport(handle)
    )
    client = _client(service)
    assert client.get("/api/key").status_code == 403
    denied[0] = False
    assert client.get("/api/key").json() == {"keys": []}
