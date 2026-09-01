from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import stat
import sys
import urllib.error
import urllib.parse
from pathlib import Path
from types import ModuleType
from typing import Any, Self

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/tailscale-acceptance-tailnet"
CHILD_POLICY = {
    "acls": [],
    "autoApprovers": {
        "services": {
            "svc:hermes-api": ["tag:vonk-gateway"],
            "svc:hermes-dashboard": ["tag:vonk-gateway"],
            "svc:vonk-forge": ["tag:vonk-gateway"],
        }
    },
    "grants": [
        {
            "dst": [
                "svc:vonk-forge",
                "svc:hermes-api",
                "svc:hermes-dashboard",
            ],
            "ip": ["tcp:443"],
            "src": ["tag:vonk-gateway"],
        }
    ],
    "tagOwners": {"tag:vonk-gateway": ["autogroup:admin"]},
}


@pytest.fixture
def lifecycle() -> ModuleType:
    name = "tailscale_acceptance_tailnet"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


class _Headers:
    def __init__(self, etag: str | None = None) -> None:
        self.etag = etag

    def get(self, name: str) -> str | None:
        return self.etag if name.lower() == "etag" else None


class _Response:
    def __init__(self, document: object, *, etag: str | None = None) -> None:
        self.payload = json.dumps(document).encode()
        self.headers = _Headers(etag)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class _Urlopen:
    def __init__(self, responses: list[object]) -> None:
        self.responses = iter(responses)
        self.requests: list[Any] = []

    def __call__(self, request: Any, *, timeout: int) -> _Response:
        assert timeout == 30
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, _Response)
        return response


def _request_body(request: Any) -> object:
    content_type = request.get_header("Content-type")
    assert request.data is not None
    if content_type == "application/json":
        return json.loads(request.data)
    assert content_type == "application/x-www-form-urlencoded"
    return urllib.parse.parse_qs(request.data.decode())


def _new_tailnet(*, dns_name: str = "tail-ci123.ts.net") -> dict[str, object]:
    return {
        "alreadyExists": False,
        "dnsName": dns_name,
        "id": "tailnet_ci_123",
        "oauthClient": {
            "id": "child_client_123",
            "secret": "child-secret-value",
        },
    }


def _success_create_responses() -> list[_Response]:
    services = [
        {"name": service, "ports": ["tcp:443"]}
        for service in (
            "svc:vonk-forge",
            "svc:hermes-api",
            "svc:hermes-dashboard",
        )
    ]
    return [
        _Response({"access_token": "factory-access-token"}),
        _Response({"tailnets": []}),
        _Response(_new_tailnet()),
        _Response({"access_token": "child-config-token"}),
        *[_Response(service) for service in services],
        _Response({"vipServices": services}),
        _Response({}, etag='"child-policy-etag"'),
        _Response({}),
        _Response(CHILD_POLICY),
        _Response(
            {
                "id": "gateway_client_123",
                "key": "gateway-secret-value",
                "keyType": "client",
                "scopes": ["auth_keys"],
                "tags": ["tag:vonk-gateway"],
            }
        ),
        _Response(
            {
                "id": "gateway_client_123",
                "keyType": "client",
                "scopes": ["auth_keys"],
                "tags": ["tag:vonk-gateway"],
            }
        ),
    ]


def _factory_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_ID", "factory-client"
    )
    monkeypatch.setenv(
        "VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_SECRET", "factory-secret"
    )


def _install_urlopen(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[object],
) -> _Urlopen:
    urlopen = _Urlopen(responses)
    monkeypatch.setattr(lifecycle.urllib.request, "urlopen", urlopen)
    return urlopen


def _paths(requests: list[Any]) -> list[str]:
    return [urllib.parse.urlsplit(request.full_url).path for request in requests]


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.tailscale.com/api/v2/tailnet/tailnet_ci_123",
        status,
        "test failure",
        {},
        None,
    )


def test_create_configures_only_the_child_and_delete_uses_its_exact_id(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _factory_environment(monkeypatch)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        _success_create_responses()
        + [
            _Response({"access_token": "child-delete-token"}),
            _Response({}),
        ],
    )
    github_env = tmp_path / "github.env"
    state = tmp_path / "state.json"

    lifecycle.create(
        display_name="Vonk Forge CI 123 attempt 1",
        github_env=github_env,
        state=state,
    )

    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert json.loads(state.read_text()) == {
        "client_id": "child_client_123",
        "client_secret": "child-secret-value",
        "tailnet_id": "tailnet_ci_123",
    }
    assert github_env.read_text().splitlines() == [
        "VONK_ACCEPTANCE_TAILNET_DNS_SUFFIX=tail-ci123.ts.net",
        "VONK_ACCEPTANCE_TAILNET_KIND=isolated-disposable-test",
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID=gateway_client_123",
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET=gateway-secret-value",
    ]

    paths = _paths(urlopen.requests)
    assert paths == [
        "/api/v2/oauth/token",
        "/api/v2/organizations/-/tailnets",
        "/api/v2/organizations/-/tailnets",
        "/api/v2/oauth/token",
        "/api/v2/tailnet/tailnet_ci_123/services/svc%3Avonk-forge",
        "/api/v2/tailnet/tailnet_ci_123/services/svc%3Ahermes-api",
        "/api/v2/tailnet/tailnet_ci_123/services/svc%3Ahermes-dashboard",
        "/api/v2/tailnet/tailnet_ci_123/services",
        "/api/v2/tailnet/tailnet_ci_123/acl",
        "/api/v2/tailnet/tailnet_ci_123/acl",
        "/api/v2/tailnet/tailnet_ci_123/acl",
        "/api/v2/tailnet/tailnet_ci_123/keys",
        "/api/v2/tailnet/tailnet_ci_123/keys/gateway_client_123",
    ]
    assert _request_body(urlopen.requests[0]) == {
        "client_id": ["factory-client"],
        "client_secret": ["factory-secret"],
        "scope": ["tailnets"],
    }
    assert urlopen.requests[1].method == "GET"
    assert urlopen.requests[1].full_url.endswith("/organizations/-/tailnets?limit=100")
    assert _request_body(urlopen.requests[2]) == {
        "displayName": "Vonk Forge CI 123 attempt 1"
    }
    for request, service in zip(
        urlopen.requests[4:7],
        ("svc:vonk-forge", "svc:hermes-api", "svc:hermes-dashboard"),
        strict=True,
    ):
        body = _request_body(request)
        assert body == {
            "comment": "Ephemeral Vonk Forge installer acceptance",
            "displayName": {
                "svc:vonk-forge": "Vonk Forge",
                "svc:hermes-api": "Hermes API",
                "svc:hermes-dashboard": "Hermes dashboard",
            }[service],
            "name": service,
            "ports": ["tcp:443"],
        }
    assert urlopen.requests[9].get_header("If-match") == '"child-policy-etag"'
    assert _request_body(urlopen.requests[9]) == CHILD_POLICY
    assert _request_body(urlopen.requests[11]) == {
        "description": "Vonk Forge CI gateway acceptance",
        "keyType": "client",
        "scopes": ["auth_keys"],
        "tags": ["tag:vonk-gateway"],
    }
    assert urlopen.requests[12].method == "GET"

    lifecycle.delete(state=state)

    assert not state.exists()
    assert _paths(urlopen.requests)[-2:] == [
        "/api/v2/oauth/token",
        "/api/v2/tailnet/tailnet_ci_123",
    ]
    assert urlopen.requests[-1].method == "DELETE"
    assert _request_body(urlopen.requests[-2]) == {
        "client_id": ["child_client_123"],
        "client_secret": ["child-secret-value"],
        "scope": ["all"],
    }
    output = capsys.readouterr().out
    assert "::add-mask::factory-access-token" in output
    assert "::add-mask::child-secret-value" in output
    assert "::add-mask::gateway-secret-value" in output
    assert "::add-mask::child-delete-token" in output


def test_malformed_post_create_identity_deletes_exact_child_before_failing(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _factory_environment(monkeypatch)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-token"}),
            _Response({"tailnets": []}),
            _Response(_new_tailnet(dns_name="production.example.com")),
            _Response({"access_token": "child-delete-token"}),
            _Response({}),
        ],
    )
    state = tmp_path / "state.json"

    with pytest.raises(lifecycle.LifecycleError, match="new-tailnet identity"):
        lifecycle.create(
            display_name="Vonk Forge CI 123 attempt 1",
            github_env=tmp_path / "github.env",
            state=state,
        )

    assert not state.exists()
    assert _paths(urlopen.requests) == [
        "/api/v2/oauth/token",
        "/api/v2/organizations/-/tailnets",
        "/api/v2/organizations/-/tailnets",
        "/api/v2/oauth/token",
        "/api/v2/tailnet/tailnet_ci_123",
    ]
    assert urlopen.requests[-1].method == "DELETE"


def test_configuration_failure_deletes_exact_child(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _factory_environment(monkeypatch)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-token"}),
            _Response({"tailnets": []}),
            _Response(_new_tailnet()),
            _Response({"access_token": "child-config-token"}),
            _Response({"name": "svc:wrong", "ports": ["tcp:443"]}),
            _Response({"access_token": "child-delete-token"}),
            _Response({}),
        ],
    )
    state = tmp_path / "state.json"

    with pytest.raises(lifecycle.LifecycleError, match="exact Service"):
        lifecycle.create(
            display_name="Vonk Forge CI 123 attempt 1",
            github_env=tmp_path / "github.env",
            state=state,
        )

    assert not state.exists()
    assert _paths(urlopen.requests)[-2:] == [
        "/api/v2/oauth/token",
        "/api/v2/tailnet/tailnet_ci_123",
    ]


@pytest.mark.parametrize(
    "listed_services",
    [
        [
            {"name": "svc:vonk-forge", "ports": ["tcp:443"]},
            {"name": "svc:vonk-forge", "ports": ["tcp:443"]},
            {"name": "svc:hermes-dashboard", "ports": ["tcp:443"]},
        ],
        [
            {"name": "svc:vonk-forge", "ports": ["tcp:443"]},
            {"name": "svc:hermes-api", "ports": ["tcp:443"]},
            {"name": "svc:hermes-dashboard", "ports": ["tcp:443"]},
            "unexpected",
        ],
    ],
)
def test_inexact_service_readback_deletes_exact_child(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    listed_services: list[object],
) -> None:
    _factory_environment(monkeypatch)
    responses = _success_create_responses()
    responses[7] = _Response({"vipServices": listed_services})
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        responses[:8]
        + [
            _Response({"access_token": "child-delete-token"}),
            _Response({}),
        ],
    )
    state = tmp_path / "state.json"

    with pytest.raises(lifecycle.LifecycleError, match="Service readback"):
        lifecycle.create(
            display_name="Vonk Forge CI 123 attempt 1",
            github_env=tmp_path / "github.env",
            state=state,
        )

    assert not state.exists()
    assert _paths(urlopen.requests)[-1] == "/api/v2/tailnet/tailnet_ci_123"


def test_environment_write_failure_deletes_exact_child(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _factory_environment(monkeypatch)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        _success_create_responses()
        + [
            _Response({"access_token": "child-delete-token"}),
            _Response({}),
        ],
    )
    monkeypatch.setattr(
        lifecycle,
        "_append_github_environment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    state = tmp_path / "state.json"

    with pytest.raises(OSError, match="disk full"):
        lifecycle.create(
            display_name="Vonk Forge CI 123 attempt 1",
            github_env=tmp_path / "github.env",
            state=state,
        )

    assert not state.exists()
    assert _paths(urlopen.requests)[-1] == "/api/v2/tailnet/tailnet_ci_123"
    assert urlopen.requests[-1].method == "DELETE"


def test_state_write_failure_deletes_created_child_from_memory(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _factory_environment(monkeypatch)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-token"}),
            _Response({"tailnets": []}),
            _Response(_new_tailnet()),
            _Response({"access_token": "child-delete-token"}),
            _Response({}),
        ],
    )
    monkeypatch.setattr(
        lifecycle,
        "_write_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read only")),
    )

    with pytest.raises(OSError, match="read only"):
        lifecycle.create(
            display_name="Vonk Forge CI 123 attempt 1",
            github_env=tmp_path / "github.env",
            state=tmp_path / "state.json",
        )

    assert _paths(urlopen.requests)[-2:] == [
        "/api/v2/oauth/token",
        "/api/v2/tailnet/tailnet_ci_123",
    ]


def test_missing_policy_etag_deletes_exact_child(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _factory_environment(monkeypatch)
    responses = _success_create_responses()
    responses[8] = _Response({})
    responses = responses[:9] + [
        _Response({"access_token": "child-delete-token"}),
        _Response({}),
    ]
    urlopen = _install_urlopen(lifecycle, monkeypatch, responses)
    state = tmp_path / "state.json"

    with pytest.raises(lifecycle.LifecycleError, match="child policy ETag"):
        lifecycle.create(
            display_name="Vonk Forge CI 123 attempt 1",
            github_env=tmp_path / "github.env",
            state=state,
        )

    assert not state.exists()
    assert _paths(urlopen.requests)[-1] == "/api/v2/tailnet/tailnet_ci_123"
    assert urlopen.requests[-1].method == "DELETE"


def test_inexact_policy_readback_deletes_exact_child(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _factory_environment(monkeypatch)
    responses = _success_create_responses()
    responses[10] = _Response(CHILD_POLICY | {"ssh": []})
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        responses[:11]
        + [
            _Response({"access_token": "child-delete-token"}),
            _Response({}),
        ],
    )
    state = tmp_path / "state.json"

    with pytest.raises(lifecycle.LifecycleError, match="policy readback"):
        lifecycle.create(
            display_name="Vonk Forge CI 123 attempt 1",
            github_env=tmp_path / "github.env",
            state=state,
        )

    assert not state.exists()
    assert _paths(urlopen.requests)[-1] == "/api/v2/tailnet/tailnet_ci_123"


def test_invalid_gateway_client_deletes_exact_child_without_export(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _factory_environment(monkeypatch)
    responses = _success_create_responses()
    responses[11] = _Response(
        {
            "id": "gateway_client_123",
            "key": "gateway-secret-value",
            "keyType": "client",
            "scopes": ["all"],
            "tags": ["tag:vonk-gateway"],
        }
    )
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        responses[:12]
        + [
            _Response({"access_token": "child-delete-token"}),
            _Response({}),
        ],
    )
    state = tmp_path / "state.json"

    with pytest.raises(lifecycle.LifecycleError, match="invalid gateway client"):
        lifecycle.create(
            display_name="Vonk Forge CI 123 attempt 1",
            github_env=tmp_path / "github.env",
            state=state,
        )

    assert not state.exists()
    assert not (tmp_path / "github.env").exists()
    assert _paths(urlopen.requests)[-1] == "/api/v2/tailnet/tailnet_ci_123"


def test_inexact_gateway_client_readback_deletes_exact_child_without_export(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _factory_environment(monkeypatch)
    responses = _success_create_responses()
    responses[12] = _Response(
        {
            "id": "gateway_client_123",
            "keyType": "client",
            "scopes": ["auth_keys"],
            "tags": ["tag:wrong"],
        }
    )
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        responses
        + [
            _Response({"access_token": "child-delete-token"}),
            _Response({}),
        ],
    )
    state = tmp_path / "state.json"

    with pytest.raises(lifecycle.LifecycleError, match="client readback"):
        lifecycle.create(
            display_name="Vonk Forge CI 123 attempt 1",
            github_env=tmp_path / "github.env",
            state=state,
        )

    assert not state.exists()
    assert not (tmp_path / "github.env").exists()
    assert _paths(urlopen.requests)[-1] == "/api/v2/tailnet/tailnet_ci_123"


def test_stale_child_preflight_blocks_creation_without_mutation(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _factory_environment(monkeypatch)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-token"}),
            _Response(
                {
                    "tailnets": [
                        {
                            "createdAt": "2020-01-01T00:00:00Z",
                            "displayName": "Vonk Forge CI 999 attempt 1",
                            "id": "tailnet_stale_999",
                        }
                    ]
                }
            ),
        ],
    )

    with pytest.raises(lifecycle.LifecycleError) as failure:
        lifecycle.create(
            display_name="Vonk Forge CI 123 attempt 1",
            github_env=tmp_path / "github.env",
            state=tmp_path / "state.json",
        )

    assert "tailnet_stale_999 (Vonk Forge CI 999 attempt 1)" in str(failure.value)
    assert [request.method for request in urlopen.requests] == ["POST", "GET"]


def test_recent_child_does_not_block_an_independent_run(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _factory_environment(monkeypatch)
    responses = _success_create_responses()
    responses[1] = _Response(
        {
            "tailnets": [
                {
                    "createdAt": "2099-01-01T00:00:00Z",
                    "displayName": "Vonk Forge CI 456 attempt 1",
                    "id": "tailnet_active_456",
                },
                {
                    "createdAt": "2020-01-01T00:00:00Z",
                    "displayName": "Production",
                    "id": "tailnet_production_789",
                },
            ]
        }
    )
    _install_urlopen(lifecycle, monkeypatch, responses)

    lifecycle.create(
        display_name="Vonk Forge CI 123 attempt 1",
        github_env=tmp_path / "github.env",
        state=tmp_path / "state.json",
    )


def test_delete_failure_preserves_protected_state(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "client_id": "child_client_123",
                "client_secret": "child-secret-value",
                "tailnet_id": "tailnet_ci_123",
            }
        )
    )
    state.chmod(0o600)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _delay: None)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            item
            for _ in range(4)
            for item in (
                _Response({"access_token": "child-delete-token"}),
                _http_error(503),
            )
        ],
    )

    with pytest.raises(lifecycle.LifecycleError, match="HTTP 503"):
        lifecycle.delete(state=state)

    assert state.exists()
    assert _paths(urlopen.requests)[-1] == "/api/v2/tailnet/tailnet_ci_123"


def test_delete_retries_transient_failure_then_removes_state(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "client_id": "child_client_123",
                "client_secret": "child-secret-value",
                "tailnet_id": "tailnet_ci_123",
            }
        )
    )
    state.chmod(0o600)
    sleeps: list[int] = []
    monkeypatch.setattr(lifecycle.time, "sleep", sleeps.append)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "first-token"}),
            _http_error(429),
            _Response({"access_token": "second-token"}),
            _Response({}),
        ],
    )

    lifecycle.delete(state=state)

    assert not state.exists()
    assert sleeps == [1]
    assert [request.method for request in urlopen.requests] == [
        "POST",
        "DELETE",
        "POST",
        "DELETE",
    ]


def test_delete_404_is_idempotent_success(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "client_id": "child_client_123",
                "client_secret": "child-secret-value",
                "tailnet_id": "tailnet_ci_123",
            }
        )
    )
    state.chmod(0o600)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [_Response({"access_token": "child-delete-token"}), _http_error(404)],
    )

    lifecycle.delete(state=state)

    assert not state.exists()
    assert len(urlopen.requests) == 2


def test_nonretryable_delete_auth_failure_retains_state(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "client_id": "child_client_123",
                "client_secret": "child-secret-value",
                "tailnet_id": "tailnet_ci_123",
            }
        )
    )
    state.chmod(0o600)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [_Response({"access_token": "child-delete-token"}), _http_error(401)],
    )

    with pytest.raises(lifecycle.LifecycleError, match="HTTP 401"):
        lifecycle.delete(state=state)

    assert state.exists()
    assert len(urlopen.requests) == 2


def test_absent_or_unsafe_state_never_calls_the_api(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    urlopen = _install_urlopen(lifecycle, monkeypatch, [])
    lifecycle.delete(state=tmp_path / "absent.json")
    assert urlopen.requests == []

    unsafe = tmp_path / "unsafe.json"
    unsafe.write_text("{}")
    unsafe.chmod(0o644)
    with pytest.raises(lifecycle.LifecycleError, match="state is unsafe"):
        lifecycle.delete(state=unsafe)
    assert urlopen.requests == []

    target = tmp_path / "target.json"
    target.write_text("{}")
    target.chmod(0o600)
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(target)
    with pytest.raises(lifecycle.LifecycleError, match="state is unsafe"):
        lifecycle.delete(state=symlink)
    assert urlopen.requests == []


def test_state_swap_during_cleanup_is_detected_and_replacement_is_not_unlinked(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "client_id": "child_client_123",
                "client_secret": "child-secret-value",
                "tailnet_id": "tailnet_ci_123",
            }
        )
    )
    state.chmod(0o600)
    replacement = tmp_path / "replacement.json"
    replacement.write_text("replacement")
    replacement.chmod(0o600)

    def swap_state(*_args: object) -> None:
        state.unlink()
        replacement.rename(state)

    monkeypatch.setattr(lifecycle, "_delete_tailnet", swap_state)

    with pytest.raises(lifecycle.LifecycleError, match="changed during cleanup"):
        lifecycle.delete(state=state)

    assert state.read_text() == "replacement"


def test_missing_factory_secrets_fail_before_any_api_request(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv(
        "VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_SECRET", raising=False
    )
    urlopen = _install_urlopen(lifecycle, monkeypatch, [])

    with pytest.raises(lifecycle.LifecycleError, match="FACTORY_OAUTH_CLIENT_ID"):
        lifecycle.create(
            display_name="Vonk Forge CI 123 attempt 1",
            github_env=tmp_path / "github.env",
            state=tmp_path / "state.json",
        )

    assert urlopen.requests == []
