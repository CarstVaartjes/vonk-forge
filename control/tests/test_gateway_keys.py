"""Gateway client keys: the Controller drives LiteLLM's key admin API."""

import hashlib
import json
import stat

import httpx2
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from vonk_agent_protocol import UnknownError
from vonk_control.auth import Actor
from vonk_control.gateway_keys import (
    GatewayKeyCreated,
    GatewayKeyService,
    install_gateway_key_routes,
)


@pytest.fixture(autouse=True)
def private_mutation_records(tmp_path, monkeypatch):
    original = GatewayKeyService.__init__

    def initialize(self, **kwargs):
        kwargs.setdefault("intent_root", tmp_path / "mutations")
        original(self, **kwargs)

    monkeypatch.setattr(GatewayKeyService, "__init__", initialize)


MASTER = "sk-master-" + "m" * 32


def _created(value: GatewayKeyCreated | UnknownError) -> GatewayKeyCreated:
    """Decode the success document; tests also verify the actual peer effect."""
    return GatewayKeyCreated.model_validate_json(value.model_dump_json())


class FakeLiteLlm:
    """LiteLLM's /key/* routes, keyed by alias, requiring the master key."""

    def __init__(self) -> None:
        self.keys: dict[str, dict] = {}
        self.counter = 0
        self.fail_generate_after: int | None = None

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/v1/models":
            secret = request.headers["authorization"].removeprefix("Bearer ")
            return httpx2.Response(
                200
                if any(item["key"] == secret for item in self.keys.values())
                else 401
            )
        assert request.headers["authorization"] == f"Bearer {MASTER}"
        if path == "/key/list":
            page = int(request.url.params["page"])
            items = [
                {
                    **{k: v for k, v in item.items() if k != "key"},
                    "token": hashlib.sha256(item["key"].encode()).hexdigest(),
                }
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
                if (
                    item["key"] == key
                    or hashlib.sha256(item["key"].encode()).hexdigest() == key
                ):
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
            aliases = body.get("key_aliases", [])
            for alias, item in list(self.keys.items()):
                if (
                    alias in aliases
                    or item["key"] in body.get("keys", [])
                    or hashlib.sha256(item["key"].encode()).hexdigest()
                    in body.get("keys", [])
                ):
                    del self.keys[alias]
            return httpx2.Response(200, json={"deleted_keys": aliases})
        return httpx2.Response(404)


def _authorizes(peer: FakeLiteLlm, secret: str) -> bool:
    return (
        peer.handle(
            httpx2.Request(
                "GET",
                "http://litellm.test/v1/models",
                headers={"authorization": f"Bearer {secret}"},
            )
        ).status_code
        == 200
    )


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
    client.post("/api/key/missing/roll")
    assert client.post("/api/key", json={"name": "missing"}).status_code == 201
    assert client.post("/api/key/missing/roll").status_code == 200


def test_roll_that_fails_to_create_keeps_the_old_key_working():
    litellm = FakeLiteLlm()
    client = _client(_service(litellm))
    first = client.post("/api/key", json={"name": "ci", "models": ["qwen"]}).json()
    litellm.fail_generate_after = litellm.counter

    client.post("/api/key/ci/roll")
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

    replacement = client.post("/api/key", json={"name": "ci"})
    assert replacement.json()["key"] != scoped.json()["key"]
    assert litellm.keys["ci"]["models"] == []

    listed = client.get("/api/key")
    assert listed.status_code == 200
    assert [key["name"] for key in listed.json()["keys"]] == ["ci", "laptop"]
    assert secret not in listed.text

    assert client.post("/api/key/ci/revoke").json() == {"name": "ci"}
    assert set(litellm.keys) == {"laptop"}
    client.post("/api/key/ci/revoke")
    assert set(litellm.keys) == {"laptop"}
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

    created = _created(service.create("first"))
    assert created.key == litellm.keys["first"]["key"]
    assert len(calls) == 1
    service.list_keys()


def test_unknown_create_ends_without_gate_and_a_fresh_create_is_admitted():
    """Catches discarding unresolved identity or leaving a gate after exhaustion."""

    litellm = FakeLiteLlm()
    litellm.fail_generate_after = 0
    service = _service(litellm)
    service.create("first")
    assert not litellm.keys
    retained = json.loads(service._intent_path("first").read_text())
    litellm.fail_generate_after = None
    created = _created(service.create("first"))
    assert created.key == retained["body"]["key"] == litellm.keys["first"]["key"]
    service.list_keys()


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

    litellm = FakeLiteLlm()
    if mutation != "create":
        _service(litellm).create("client", models=["qwen"])
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
            assert retained["body"]["key"] == body["key"]
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
    assert len(effects) == 1
    recovered = service(litellm.handle)
    result = (
        recovered.create("client") if mutation == "create" else recovered.roll("client")
    )
    result = _created(result)
    assert result.key == effects[0] == litellm.keys["client"]["key"]
    assert list(litellm.keys) == ["client"]
    # A completed reconciliation leaves no gate on the next rotation.
    next_key = _created(recovered.roll("client"))
    assert next_key.key != result.key


@pytest.mark.parametrize("damaged", [True, False])
def test_new_create_content_supersedes_unresolved_or_damaged_intent(tmp_path, damaged):
    """Catches replaying stale scope or refusing a fresh request on damaged local metadata."""

    litellm = FakeLiteLlm()
    litellm.fail_generate_after = 0
    service = _service(litellm)
    service.create("client", models=["old"])
    if damaged:
        service._intent_path("client").write_text("broken")
    litellm.fail_generate_after = None
    created = _created(service.create("client", models=["new"]))
    assert created.models == litellm.keys["client"]["models"] == ["new"]


def test_busy_mutation_ends_without_effect_and_fresh_request_enters():
    """Catches concurrent mutation writers replacing the only retained secret."""

    litellm = FakeLiteLlm()
    service = _service(litellm)
    with service._mutation_claim("client") as acquired:
        assert acquired
        _service(litellm).create("client")
        assert not litellm.keys
    service.create("client")


@pytest.mark.parametrize("replacement", ["roll", "create"])
def test_damaged_roll_after_original_deletion_does_not_gate_new_mutation(replacement):
    """Catches an orphan temporary alias requiring manual revocation to escape."""

    litellm = FakeLiteLlm()
    service = _service(litellm)
    service.create("client", models=["old"])
    original_request = service._gateway_request

    def unavailable_rename(method, path, **kwargs):
        if path == "/key/update":
            from vonk_control.gateway_keys import GatewayKeyError

            raise GatewayKeyError("injected rename loss")
        return original_request(method, path, **kwargs)

    service._gateway_request = unavailable_rename
    service.roll("client")
    assert list(litellm.keys) == ["client.rolling"]
    service._intent_path("client.rolling").write_text("broken")
    service._gateway_request = original_request
    result = (
        service.roll("client")
        if replacement == "roll"
        else service.create("client", models=["new"])
    )
    assert list(litellm.keys) == ["client"]
    result = _created(result)
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
@pytest.mark.slow(45)
def test_process_death_preserves_create_secret_and_releases_mutation_claim(tmp_path):
    """Catches a memory-only secret or a claim surviving executor death."""
    import multiprocessing

    root = tmp_path / "process-records"
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    litellm = FakeLiteLlm()

    process = context.Process(target=_create_crash_executor, args=(root, sender))
    process.start()
    sender.close()
    try:
        assert receiver.poll(30)
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
        recovered = _created(service.create("client"))
        assert recovered.key == json.loads(body)["key"]
        service.roll("client")
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)


@pytest.mark.parametrize("reply", ["malformed", "unavailable"])
def test_unknown_exact_info_preserves_working_key_until_restart(tmp_path, reply):
    """A lost success followed by unreadable observation must never delete or generate twice."""
    peer = FakeLiteLlm()
    effects = []
    broken = [False]

    def transport(request):
        if request.url.path in {"/key/generate", "/key/delete"}:
            effects.append(request.url.path)
        response = peer.handle(request)
        if request.url.path == "/key/generate":
            broken[0] = True
            raise httpx2.ReadError("lost reply", request=request)
        if request.url.path == "/key/info" and broken[0]:
            return (
                httpx2.Response(200, content=b"invalid")
                if reply == "malformed"
                else httpx2.Response(503, json={})
            )
        return response

    service = GatewayKeyService(
        master_key=lambda: MASTER, transport=httpx2.MockTransport(transport)
    )
    service.create("client", request_id="create-one")
    secret = peer.keys["client"]["key"]
    assert effects == ["/key/generate"]
    restarted = _service(peer)
    assert _created(restarted.create("client", request_id="create-one")).key == secret
    assert _created(restarted.create("client", request_id="create-two")).key != secret


@pytest.mark.parametrize("after", [False, True])
def test_revoke_supersedes_interrupted_rotation_and_releases_fresh_create(after):
    """Revoke must reconcile both owned aliases after process restart."""
    peer = FakeLiteLlm()
    service = _service(peer)
    old = _created(service.create("client")).key
    outage = [False]

    def transport(request):
        if outage[0]:
            raise httpx2.ReadError("offline", request=request)
        if request.url.path == "/key/update":
            if after:
                peer.handle(request)
            outage[0] = True
            raise httpx2.ReadError("rename interrupted", request=request)
        return peer.handle(request)

    interrupted = GatewayKeyService(
        master_key=lambda: MASTER, transport=httpx2.MockTransport(transport)
    )
    interrupted.roll("client", request_id="rotation-one")
    secrets = {entry["key"] for entry in peer.keys.values()} | {old}
    restarted = _service(peer)
    restarted.revoke("client")
    assert not peer.keys
    fresh = _created(restarted.create("client"))
    assert fresh.key not in secrets
    assert set(peer.keys) == {"client"}


@pytest.mark.parametrize("release_during_request", [False, True])
def test_busy_mutation_observes_unlock_or_ends_without_retained_lock(
    monkeypatch, release_during_request
):
    import fcntl

    from vonk_control import gateway_keys

    peer = FakeLiteLlm()
    service = _service(peer)
    pauses = []
    with service._mutation_claim("client") as acquired:
        assert acquired
        # Release the actual flock through its owning context between attempts.
        # A second independently opened descriptor cannot release that lock.
        lock_path = service._intent_path("client").with_suffix(".lock")
        assert lock_path.exists()
    descriptor = lock_path.open("r+")
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def pause(seconds):
        pauses.append(seconds)
        if release_during_request:
            fcntl.flock(descriptor, fcntl.LOCK_UN)

    monkeypatch.setattr(gateway_keys.time, "sleep", pause)
    try:
        service.create("client")
        assert len(pauses) <= 2
        assert bool(peer.keys) == release_during_request
    finally:
        descriptor.close()
    service.create("client")
    assert set(peer.keys) == {"client"}


def test_request_receipts_survive_retirement_and_new_rotation_changes_secret():
    peer = FakeLiteLlm()
    service = _service(peer)
    service.create("client", request_id="create")
    first = _created(service.roll("client", request_id="roll-one")).key
    restarted = _service(peer)
    assert _created(restarted.roll("client", request_id="roll-one")).key == first
    second = _created(restarted.roll("client", request_id="roll-two")).key
    assert second != first
    assert peer.keys["client"]["key"] == second


def test_completed_archive_prevents_failed_retirement_from_reusing_rotation(
    monkeypatch,
):
    from vonk_control import gateway_keys

    peer = FakeLiteLlm()
    service = _service(peer)
    service.create("client")
    original = gateway_keys._write_private
    rolling_path = service._intent_path("client.rolling")

    def interrupted(path, content):
        receipt = gateway_keys.GatewayMutationReceipt.model_validate_json(content)
        if path == rolling_path and receipt.completed:
            raise OSError("retirement unavailable")
        original(path, content)

    monkeypatch.setattr(gateway_keys, "_write_private", interrupted)
    first = _created(service.roll("client", request_id="first-rotation")).key
    assert peer.keys["client"]["key"] == first
    monkeypatch.setattr(gateway_keys, "_write_private", original)
    restarted = _service(peer)
    second = _created(restarted.roll("client", request_id="second-rotation")).key
    assert first != second
    assert _created(restarted.roll("client", request_id="first-rotation")).key == first
    assert peer.keys["client"]["key"] == second


@pytest.mark.parametrize("reply", ["malformed", "unavailable"])
def test_rename_reply_loss_is_observed_without_duplicate_generation(reply):
    peer = FakeLiteLlm()
    service = _service(peer)
    old = _created(service.create("client")).key
    updates = []

    def transport(request):
        response = peer.handle(request)
        if request.url.path == "/key/update":
            updates.append(request.url.path)
            return (
                httpx2.Response(200, content=b"bad")
                if reply == "malformed"
                else httpx2.Response(503, json={})
            )
        return response

    interrupted = GatewayKeyService(
        master_key=lambda: MASTER, transport=httpx2.MockTransport(transport)
    )
    interrupted.roll("client", request_id="rename")
    restarted = _service(peer)
    current = _created(restarted.roll("client", request_id="rename")).key
    assert current != old
    assert updates == ["/key/update"]
    assert set(peer.keys) == {"client"}
    assert _created(restarted.roll("client", request_id="new-rename")).key != current


def test_repeated_request_binds_content_before_effect_and_new_request_enters():
    peer = FakeLiteLlm()
    service = _service(peer)
    first = _created(service.create("client", models=["one"], request_id="bound"))
    from .observed_actions import observe_action

    observe_action(lambda: service.create("client", models=["two"], request_id="bound"))
    assert peer.keys["client"]["key"] == first.key
    assert peer.keys["client"]["models"] == ["one"]
    fresh = _created(service.create("client", models=["two"], request_id="new"))
    assert fresh.key != first.key
    assert peer.keys["client"]["models"] == ["two"]


def test_unavailable_capability_read_ends_without_http_refusal_and_recovers():
    from fastapi import HTTPException

    peer = FakeLiteLlm()

    class Capability(GatewayKeyService):
        def __init__(self):
            super().__init__(
                master_key=lambda: MASTER, transport=httpx2.MockTransport(peer.handle)
            )
            self.unavailable = True

        def list_keys(self):
            if self.unavailable:
                raise HTTPException(503)
            return super().list_keys()

    capability = Capability()
    client = _client(capability)
    response = client.get("/api/key")
    assert response.status_code == 200
    assert not peer.keys
    capability.unavailable = False
    assert client.get("/api/key").json() == {"keys": []}
    client.post("/api/key", json={"name": "fresh", "request_id": "fresh"})
    assert set(peer.keys) == {"fresh"}


@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.parametrize("http_status", [500, 502, 503, 504])
def test_default_key_http_unavailable_reobserves_and_fresh_attempt_enters(
    exhausted, http_status
):
    """Catches treating HTTP 503 as a security denial or retaining a busy gate."""
    import asyncio

    from fastapi import HTTPException
    from vonk_control.gateway_keys import keep_default_key

    class Service:
        def __init__(self):
            self.attempts = 0
            self.unavailable = True
            self.effects = 0

        def ensure_default(self, path):
            self.attempts += 1
            if self.unavailable:
                if not exhausted and self.attempts == 3:
                    self.unavailable = False
                else:
                    raise HTTPException(http_status)
            self.effects += 1
            return True

    service = Service()

    async def observe():
        await asyncio.wait_for(
            keep_default_key(
                service,  # type: ignore[arg-type]
                asyncio.Event(),
                first_delay=0.001,
                maximum_delay=0.002,
            ),
            timeout=1,
        )

    asyncio.run(observe())
    assert service.attempts == (6 if exhausted else 3)
    assert service.effects == (0 if exhausted else 1)
    service.unavailable = False
    asyncio.run(observe())
    assert service.effects == (1 if exhausted else 2)


@pytest.mark.parametrize("damage", ["io", "encoding"])
def test_unreadable_default_secret_preserves_file_and_remote_effect(
    tmp_path, monkeypatch, damage
):
    """Catches replacing a working secret because local storage is temporarily unreadable."""
    from pathlib import Path

    peer = FakeLiteLlm()
    service = _service(peer)
    path = tmp_path / "client-key"
    service.ensure_default(path)
    secret = path.read_bytes()
    read = Path.read_text

    def unavailable(self, *args, **kwargs):
        if self == path:
            if damage == "io":
                raise PermissionError("storage unavailable")
            raise UnicodeError("encoding unavailable")
        return read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unavailable)
    service.ensure_default(path)
    assert path.read_bytes() == secret
    assert peer.keys["default"]["key"] == secret.decode().strip()
    assert _authorizes(peer, secret.decode().strip())
    monkeypatch.setattr(Path, "read_text", read)
    service.ensure_default(path)
    service.create("fresh")
    assert set(peer.keys) == {"default", "fresh"}


@pytest.mark.parametrize("damage", ["missing", "null", "mixed"])
def test_roll_reobserves_exact_scope_before_any_replacement(damage):
    """A plausible list is not authority for a replacement key's model scope."""
    peer = FakeLiteLlm()
    original = _created(_service(peer).create("client", models=["qwen"]))
    broken = [True]
    effects = []

    def handle(request):
        if request.method == "POST":
            effects.append(request.url.path)
        response = peer.handle(request)
        if broken[0] and request.url.path == "/key/info":
            body = response.json()
            if damage == "missing":
                body["info"].pop("models")
            else:
                body["info"]["models"] = None if damage == "null" else ["qwen", 7]
            return httpx2.Response(200, json=body)
        return response

    service = GatewayKeyService(
        master_key=lambda: MASTER, transport=httpx2.MockTransport(handle)
    )
    service.roll("client")
    assert effects == []
    assert peer.keys["client"]["key"] == original.key
    assert _authorizes(peer, original.key)
    broken[0] = False
    replacement = _created(service.roll("client"))
    assert replacement.models == ["qwen"]
    assert replacement.key != original.key
    assert _created(service.roll("client", request_id="fresh")).key != replacement.key


def test_new_roll_settles_previous_replacement_before_generate_failure():
    """A newer failed roll must not delete the sole surviving earlier replacement."""
    peer = FakeLiteLlm()
    service = _service(peer)
    service.create("client", models=["qwen"])
    offline = [False]

    def interrupted(request):
        if offline[0] or request.url.path == "/key/update":
            offline[0] = True
            raise httpx2.ReadError("rename unavailable", request=request)
        return peer.handle(request)

    first = GatewayKeyService(
        master_key=lambda: MASTER, transport=httpx2.MockTransport(interrupted)
    )
    first.roll("client", request_id="first")
    assert set(peer.keys) == {"client.rolling"}
    surviving = peer.keys["client.rolling"]["key"]
    peer.fail_generate_after = peer.counter
    restarted = _service(peer)
    restarted.roll("client", request_id="second")
    assert set(peer.keys) == {"client"}
    assert peer.keys["client"]["key"] == surviving
    assert _created(restarted.roll("client", request_id="first")).key == surviving
    peer.fail_generate_after = None
    replacement = _created(restarted.roll("client", request_id="second"))
    assert replacement.key != surviving
    assert set(peer.keys) == {"client"}
    assert _authorizes(peer, replacement.key)
    assert not _authorizes(peer, surviving)
    assert _created(restarted.roll("client", request_id="fresh")).key != replacement.key


@pytest.mark.parametrize("reply", ["unavailable", "unparseable", "missing"])
def test_unknown_default_info_has_bounded_observation_and_no_destructive_effect(
    tmp_path, reply
):
    """Catches revoking the default on a peer outage or unreadable success body."""
    peer = FakeLiteLlm()
    path = tmp_path / "client-key"
    _service(peer).ensure_default(path)
    secret = path.read_bytes()
    broken = [True]
    effects = []

    def handle(request):
        if request.method == "POST":
            effects.append(request.url.path)
        if request.url.path == "/key/info" and broken[0]:
            if reply == "unavailable":
                return httpx2.Response(503, json={})
            if reply == "unparseable":
                return httpx2.Response(200, content=b"broken")
            return httpx2.Response(200, json={"info": {}})
        return peer.handle(request)

    service = GatewayKeyService(
        master_key=lambda: MASTER, transport=httpx2.MockTransport(handle)
    )
    service.ensure_default(path)
    assert effects == []
    assert path.read_bytes() == secret
    assert peer.keys["default"]["key"] == secret.decode().strip()
    assert _authorizes(peer, secret.decode().strip())
    broken[0] = False
    service.ensure_default(path)
    assert effects == []
    service.create("fresh")
    assert set(peer.keys) == {"default", "fresh"}


@pytest.mark.parametrize(
    "entry",
    [
        7,
        {},
        {"key_alias": "foreign", "models": None},
        {"key_alias": "foreign", "models": [], "expires": 7},
    ],
)
def test_foreign_broken_entry_is_ignored(entry):
    """A damaged unrelated entry must not block listing or rolling our key."""
    peer = FakeLiteLlm()
    original = _created(_service(peer).create("client", models=["qwen"]))

    def handle(request):
        response = peer.handle(request)
        if request.url.path == "/key/list":
            body = response.json()
            body["keys"].append(entry)
            return httpx2.Response(200, json=body)
        return response

    service = GatewayKeyService(
        master_key=lambda: MASTER, transport=httpx2.MockTransport(handle)
    )
    assert _client(service).get("/api/key").json()["keys"][0]["name"] == "client"
    replacement = _created(service.roll("client"))
    assert replacement.key != original.key
    assert set(peer.keys) == {"client"}
    assert peer.keys["client"]["key"] == replacement.key
    assert _authorizes(peer, replacement.key)
    assert not _authorizes(peer, original.key)


@pytest.mark.parametrize("token_available", [False, True])
@pytest.mark.parametrize("receipt_available", [False, True])
def test_revoke_succeeds_without_info_or_receipt(token_available, receipt_available):
    """Revocation must not require a readable secret or an extra info lookup."""
    peer = FakeLiteLlm()
    original = _created(_service(peer).create("client"))

    def handle(request):
        if request.url.path == "/key/info":
            return httpx2.Response(503, json={})
        response = peer.handle(request)
        if request.url.path == "/key/list" and not token_available:
            body = response.json()
            for item in body["keys"]:
                item.pop("token", None)
            return httpx2.Response(200, json=body)
        return response

    service = GatewayKeyService(
        master_key=lambda: MASTER, transport=httpx2.MockTransport(handle)
    )
    if not receipt_available:
        for path in service._intent_root.glob("*.json"):
            path.unlink()
    assert _client(service).post("/api/key/client/revoke").json() == {"name": "client"}
    assert not peer.keys
    assert not _authorizes(peer, original.key)


def test_revoke_succeeds_when_receipt_secret_is_already_absent():
    """A stale receipt must not permanently block an already completed revoke."""
    peer = FakeLiteLlm()
    service = _service(peer)
    original = _created(service.create("client"))
    peer.keys.clear()

    def handle(request):
        if request.url.path == "/key/delete" and json.loads(request.content).get(
            "keys"
        ):
            return httpx2.Response(400, json={"error": "not found"})
        return peer.handle(request)

    restarted = GatewayKeyService(
        master_key=lambda: MASTER, transport=httpx2.MockTransport(handle)
    )
    assert _client(restarted).post("/api/key/client/revoke").json() == {
        "name": "client"
    }
    assert restarted._pending_receipt("client") is None
    assert not _authorizes(peer, original.key)


@pytest.mark.parametrize("damage", [{"models": None}, {"expires": 7}])
def test_unreadable_target_list_entry_preserves_working_key(damage):
    """A damaged target is unknown, rather than an absent alias we may replace."""
    peer = FakeLiteLlm()
    original = _created(_service(peer).create("client", models=["qwen"]))

    def handle(request):
        response = peer.handle(request)
        if request.url.path == "/key/list":
            body = response.json()
            for item in body["keys"]:
                item.update(damage)
            return httpx2.Response(200, json=body)
        return response

    service = GatewayKeyService(
        master_key=lambda: MASTER, transport=httpx2.MockTransport(handle)
    )
    assert isinstance(service.roll("client"), UnknownError)
    assert set(peer.keys) == {"client"}
    assert _authorizes(peer, original.key)


@pytest.mark.parametrize("http_status", [500, 502, 503, 504])
def test_gateway_dependency_recovers_after_prolonged_outage(tmp_path, http_status):
    """Catches a healthy relay staying unavailable despite recurring observations."""
    from datetime import UTC, datetime, timedelta

    from fastapi import HTTPException
    from vonk_control.capabilities import CapabilityRegistry
    from vonk_control.capability_contract import (
        CapabilityAvailability,
        ControllerCapability,
    )

    now = datetime(2026, 10, 10, tzinfo=UTC)
    restored_at = now + timedelta(minutes=10)
    peer = FakeLiteLlm()

    def relay(request):
        # The production listener rejects health routes even when LiteLLM is up.
        if not request.url.path.startswith("/key/"):
            return httpx2.Response(404)
        if now < restored_at:
            return httpx2.Response(http_status, json={"error": "not ready"})
        return peer.handle(request)

    client = GatewayKeyService(
        master_key=lambda: MASTER,
        transport=httpx2.MockTransport(relay),
    )
    capabilities = CapabilityRegistry(clock=lambda: now)
    service = capabilities.guard(
        ControllerCapability.GATEWAY_KEYS,
        GatewayKeyService,
        lambda: client,
        check=lambda value: value.check_health(),
    )
    previous_delay = None
    # Drive the production capability owner at its actual scheduled deadlines.
    # The ten-minute outage outlasts the separate finite startup observation.
    while now < restored_at:
        capabilities.retry_due()
        unavailable = capabilities.statuses()[0]
        assert unavailable.availability == CapabilityAvailability.UNAVAILABLE
        with pytest.raises(HTTPException) as error:
            service.ensure_default(tmp_path / "client-key")
        assert error.value.status_code == 503
        next_attempt = unavailable.next_attempt_at
        assert next_attempt is not None
        delay = (next_attempt - now).total_seconds()
        assert 0 < delay <= 60
        if previous_delay is not None:
            assert delay == min(previous_delay * 2, 60)
        previous_delay = delay
        now = next_attempt

    capabilities.retry_due()
    assert capabilities.statuses()[0].availability == CapabilityAvailability.AVAILABLE
    path = tmp_path / "client-key"
    assert service.ensure_default(path) is True
    assert _authorizes(peer, path.read_text().strip())
    assert service.ensure_default(path) is False
    client.close()
