"""Gateway client keys: the Controller drives LiteLLM's key admin API."""

import json
import stat

import httpx2
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from vonk_control.auth import Actor
from vonk_control.gateway_keys import GatewayKeyService, install_gateway_key_routes

MASTER = "sk-master-" + "m" * 32


class FakeLiteLlm:
    """LiteLLM's /key/* routes, keyed by alias, requiring the master key."""

    def __init__(self) -> None:
        self.keys: dict[str, dict] = {}
        self.counter = 0

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

    assert client.post("/api/key/ci/revoke").json() == {
        "name": "ci",
        "revoked": True,
    }
    assert set(litellm.keys) == {"laptop"}
    assert client.post("/api/key/ci/revoke").status_code == 404


def test_only_administrators_create_or_revoke_keys():
    client = _client(_service(FakeLiteLlm()), role="operator")
    assert client.post("/api/key", json={"name": "x"}).status_code == 403
    assert client.post("/api/key/x/revoke").status_code == 403
    assert client.get("/api/key").status_code == 200


def test_unreachable_litellm_is_a_bounded_503():
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    service = GatewayKeyService(
        base_url="http://litellm.test",
        master_key=lambda: MASTER,
        transport=httpx2.MockTransport(refuse),
    )
    response = _client(service).get("/api/key")
    assert response.status_code == 503
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
