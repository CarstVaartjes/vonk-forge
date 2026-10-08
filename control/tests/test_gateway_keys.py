"""Gateway client keys: the Controller drives LiteLLM's key admin API."""

import json
import stat

import httpx2
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from vonk_agent_protocol import ErrorCategory, UnknownError
from vonk_control.auth import Actor
from vonk_control.gateway_keys import GatewayKeyService, install_gateway_key_routes


@pytest.fixture(autouse=True)
def private_mutation_records(tmp_path, monkeypatch):
    original = GatewayKeyService.__init__

    def initialize(self, **kwargs):
        kwargs.setdefault("intent_root", tmp_path / "mutations")
        original(self, **kwargs)

    monkeypatch.setattr(GatewayKeyService, "__init__", initialize)


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
    assert isinstance(
        UnknownError.model_validate(client.post("/api/key/missing/roll").json()),
        UnknownError,
    )
    assert client.post("/api/key", json={"name": "missing"}).status_code == 201
    assert client.post("/api/key/missing/roll").status_code == 200


def test_roll_that_fails_to_create_keeps_the_old_key_working():
    litellm = FakeLiteLlm()
    client = _client(_service(litellm))
    first = client.post("/api/key", json={"name": "ci", "models": ["qwen"]}).json()
    litellm.fail_generate_after = litellm.counter

    assert isinstance(
        UnknownError.model_validate(client.post("/api/key/ci/roll").json()),
        UnknownError,
    )
    assert list(litellm.keys) == ["ci"]
    assert litellm.keys["ci"]["key"] == first["key"]
    litellm.fail_generate_after = None
    recovered = client.post("/api/key/ci/roll")
    assert recovered.status_code == 200
    assert recovered.json()["key"] != first["key"]
    assert list(litellm.keys) == ["ci"]


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
    assert isinstance(
        UnknownError.model_validate(client.post("/api/key/ci/revoke").json()),
        UnknownError,
    )
    assert client.post("/api/key", json={"name": "ci"}).status_code == 201


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
    """Catches discarding unresolved identity or leaving a gate after exhaustion."""
    from vonk_control.gateway_keys import GatewayKeyCreated

    litellm = FakeLiteLlm()
    litellm.fail_generate_after = 0
    service = _service(litellm)
    result = service.create("first")
    assert isinstance(result, UnknownError)
    assert not litellm.keys
    retained = json.loads(service._intent_path("first").read_text())
    litellm.fail_generate_after = None
    created = service.create("first")
    assert isinstance(created, GatewayKeyCreated)
    assert created.key == retained["key"] == litellm.keys["first"]["key"]
    assert not isinstance(service.list_keys(), UnknownError)


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


@pytest.mark.parametrize("mutation", ["create", "rename-before", "rename-after"])
def test_remote_effect_and_observation_loss_recovers_after_service_restart(
    tmp_path, mutation
):
    """Catches losing the sole secret when both the effect reply and its observation disappear."""
    from vonk_control.gateway_keys import GatewayKeyCreated

    litellm = FakeLiteLlm()
    if mutation != "create":
        assert isinstance(
            _service(litellm).create("client", models=["qwen"]), GatewayKeyCreated
        )
    outage = [False]
    effects = []
    root = tmp_path / "retained"

    def transport(request):
        if outage[0]:
            raise httpx2.ReadError("observation unavailable", request=request)
        path = request.url.path
        target = "/key/generate" if mutation == "create" else "/key/update"
        if path == target:
            records = list(root.glob("*.json"))
            assert len(records) == 1
            retained = json.loads(records[0].read_text())
            assert stat.S_IMODE(records[0].stat().st_mode) == 0o600
            body = json.loads(request.content)
            assert retained["key"] == body["key"]
            effects.append(body["key"])
            if mutation != "rename-before":
                litellm.handle(request)
            outage[0] = True
            raise httpx2.ReadError("effect reply lost", request=request)
        return litellm.handle(request)

    def service(handler):
        return GatewayKeyService(
            master_key=lambda: MASTER,
            transport=httpx2.MockTransport(handler),
            intent_root=root,
        )

    first = service(transport)
    result = first.create("client") if mutation == "create" else first.roll("client")
    assert isinstance(result, UnknownError)
    assert len(effects) == 1
    recovered = service(litellm.handle)
    result = (
        recovered.create("client") if mutation == "create" else recovered.roll("client")
    )
    assert isinstance(result, GatewayKeyCreated)
    assert result.key == effects[0] == litellm.keys["client"]["key"]
    assert list(litellm.keys) == ["client"]
    # A completed reconciliation leaves no gate on the next rotation.
    next_key = recovered.roll("client")
    assert isinstance(next_key, GatewayKeyCreated)
    assert next_key.key != result.key


@pytest.mark.parametrize("damaged", [True, False])
def test_new_create_content_supersedes_unresolved_or_damaged_intent(tmp_path, damaged):
    """Catches replaying stale scope or refusing a fresh request on damaged local metadata."""
    from vonk_control.gateway_keys import GatewayKeyCreated

    litellm = FakeLiteLlm()
    litellm.fail_generate_after = 0
    service = _service(litellm)
    assert isinstance(service.create("client", models=["old"]), UnknownError)
    if damaged:
        service._intent_path("client").write_text("broken")
    litellm.fail_generate_after = None
    created = service.create("client", models=["new"])
    assert isinstance(created, GatewayKeyCreated)
    assert created.models == litellm.keys["client"]["models"] == ["new"]


def test_busy_mutation_ends_without_effect_and_fresh_request_enters():
    """Catches concurrent mutation writers replacing the only retained secret."""
    from vonk_control.gateway_keys import GatewayKeyCreated

    litellm = FakeLiteLlm()
    service = _service(litellm)
    with service._mutation_claim("client") as acquired:
        assert acquired
        result = _service(litellm).create("client")
        assert isinstance(result, UnknownError)
        assert not litellm.keys
    assert isinstance(service.create("client"), GatewayKeyCreated)


@pytest.mark.parametrize("replacement", ["roll", "create"])
def test_damaged_roll_after_original_deletion_does_not_gate_new_mutation(replacement):
    """Catches an orphan temporary alias requiring manual revocation to escape."""
    from vonk_control.gateway_keys import GatewayKeyCreated

    litellm = FakeLiteLlm()
    service = _service(litellm)
    assert isinstance(service.create("client", models=["old"]), GatewayKeyCreated)
    original_request = service._gateway_request

    def unavailable_rename(method, path, **kwargs):
        if path == "/key/update":
            from vonk_control.gateway_keys import GatewayKeyError

            raise GatewayKeyError("injected rename loss")
        return original_request(method, path, **kwargs)

    service._gateway_request = unavailable_rename
    assert isinstance(service.roll("client"), UnknownError)
    assert list(litellm.keys) == ["client.rolling"]
    service._intent_path("client.rolling").write_text("broken")
    service._gateway_request = original_request
    result = (
        service.roll("client")
        if replacement == "roll"
        else service.create("client", models=["new"])
    )
    assert isinstance(result, GatewayKeyCreated)
    assert list(litellm.keys) == ["client"]
    assert result.key == litellm.keys["client"]["key"]
    assert result.models == (["old"] if replacement == "roll" else ["new"])


def _create_crash_executor(root, sender):
    import os

    litellm = FakeLiteLlm()

    def handle(request):
        if request.url.path == "/key/generate":
            sender.send_bytes(request.content)
            os._exit(0)
        return litellm.handle(request)

    GatewayKeyService(
        master_key=lambda: MASTER,
        transport=httpx2.MockTransport(handle),
        intent_root=root,
    ).create("client")


# A fresh interpreter avoids inheriting HTTP-client threads while exercising death.
@pytest.mark.slow(15)
def test_process_death_preserves_create_secret_and_releases_mutation_claim(tmp_path):
    """Catches a memory-only secret or a claim surviving executor death."""
    import multiprocessing

    from vonk_control.gateway_keys import GatewayKeyCreated

    root = tmp_path / "process-records"
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    litellm = FakeLiteLlm()

    process = context.Process(target=_create_crash_executor, args=(root, sender))
    process.start()
    sender.close()
    try:
        assert receiver.poll(10)
        body = receiver.recv_bytes()
        # The external service retains its effect independently of the dead
        # executor; reconciliation consumes the actual persisted request bytes.
        litellm.handle(
            httpx2.Request(
                "POST",
                "http://litellm.test/key/generate",
                headers={"authorization": f"Bearer {MASTER}"},
                content=body,
            )
        )
        process.join(timeout=3)
        assert not process.is_alive()
        assert process.exitcode == 0
        service = GatewayKeyService(
            master_key=lambda: MASTER,
            transport=httpx2.MockTransport(litellm.handle),
            intent_root=root,
        )
        recovered = service.create("client")
        assert isinstance(recovered, GatewayKeyCreated)
        assert recovered.key == json.loads(body)["key"]
        assert litellm.counter == 1
        assert isinstance(service.roll("client"), GatewayKeyCreated)
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
