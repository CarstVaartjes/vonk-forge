from __future__ import annotations

import email.message
import importlib.machinery
import importlib.util
import json
import socket
import stat
import sys
import urllib.error
import urllib.parse
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
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
    def __init__(
        self,
        document: object,
        *,
        etag: str | None = None,
        raw: bytes | None = None,
    ) -> None:
        self.payload = json.dumps(document).encode() if raw is None else raw
        self.headers = _Headers(etag)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class _Urlopen:
    def __init__(self, responses: Sequence[object]) -> None:
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
    responses: Sequence[object],
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
        email.message.Message(),
        None,
    )


def _freeze_now(lifecycle: ModuleType, monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pin the script's clock so the stale boundary is exact, not flaky.

    Cleanup decides staleness by comparing the listing's `createdAt` against the
    wall clock.  A test that reads the real clock drifts across `STALE_CHILD_AGE`
    while it runs; this fixes one instant and derives both sides of the boundary
    from it.
    """
    now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return now

    monkeypatch.setattr(lifecycle, "datetime", _FrozenDatetime)
    return now


def _created_at(*, minutes_old: float, now: datetime) -> str:
    return (now - timedelta(minutes=minutes_old)).isoformat()


def _organization_child(
    *, child_id: str, display_name: str, created_at: str
) -> dict[str, object]:
    return {
        "createdAt": created_at,
        "displayName": display_name,
        "id": child_id,
    }


def _listing_response(
    *children: dict[str, object],
) -> _Response:
    return _Response({"tailnets": list(children)})


def _child_delete_responses(child_id: str) -> list[_Response]:
    """The tailnet-scoped token exchange and DELETE for exactly one child."""
    return [
        _Response({"access_token": f"delete-token-{child_id}"}),
        _Response({}),
    ]


def _cleanup_requests(urlopen: _Urlopen) -> list[Any]:
    return [request for request in urlopen.requests if request.method == "DELETE"]


def _deleted_ids(urlopen: _Urlopen) -> list[str]:
    return [
        urllib.parse.unquote(
            urllib.parse.urlsplit(request.full_url).path.rsplit("/", 1)[-1]
        )
        for request in _cleanup_requests(urlopen)
    ]


def test_stale_well_formed_ci_child_is_selected_and_deleted(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _factory_environment(monkeypatch)
    now = _freeze_now(lifecycle, monkeypatch)
    child = _organization_child(
        child_id="tailnet_stale_999",
        display_name="Vonk Forge CI 35232305043 attempt 1",
        created_at=_created_at(minutes_old=90, now=now),
    )
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [_Response({"access_token": "factory-list-token"}), _listing_response(child)]
        + _child_delete_responses("tailnet_stale_999"),
    )

    lifecycle.cleanup()

    assert _deleted_ids(urlopen) == ["tailnet_stale_999"]
    assert (
        urlopen.requests[-1].get_header("Authorization")
        == "Bearer delete-token-tailnet_stale_999"
    )
    output = capsys.readouterr().out
    assert (
        "Deleted stale disposable CI tailnet tailnet_stale_999 "
        "(Vonk Forge CI 35232305043 attempt 1)" in output
    )
    assert "deleted 1 of 1" in output


def test_two_stale_children_are_both_deleted_by_their_own_exact_ids(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _factory_environment(monkeypatch)
    now = _freeze_now(lifecycle, monkeypatch)
    oldest = _organization_child(
        child_id="tailnet_stale_111",
        display_name="Vonk Forge CI 35232305043 attempt 1",
        created_at=_created_at(minutes_old=180, now=now),
    )
    newer = _organization_child(
        child_id="tailnet_stale_222",
        display_name="Vonk Forge CI 35266396689 attempt 1",
        created_at=_created_at(minutes_old=90, now=now),
    )
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-list-token"}),
            _listing_response(oldest, newer),
        ]
        + _child_delete_responses("tailnet_stale_111")
        + _child_delete_responses("tailnet_stale_222"),
    )

    lifecycle.cleanup()

    assert _deleted_ids(urlopen) == ["tailnet_stale_111", "tailnet_stale_222"]
    token_bodies = [
        _request_body(request)
        for request in urlopen.requests
        if urllib.parse.urlsplit(request.full_url).path.endswith("/oauth/token")
    ]
    # The creating credential reaches an existing API-only child only by naming
    # that child in the token request; without `tailnet` the exchange mints an
    # organization token and the DELETE would not address this child.
    assert token_bodies[0] == {
        "client_id": ["factory-client"],
        "client_secret": ["factory-secret"],
        "scope": ["tailnets"],
    }
    assert token_bodies[1:] == [
        {
            "client_id": ["factory-client"],
            "client_secret": ["factory-secret"],
            "scope": ["all"],
            "tailnet": ["tailnet_stale_111"],
        },
        {
            "client_id": ["factory-client"],
            "client_secret": ["factory-secret"],
            "scope": ["all"],
            "tailnet": ["tailnet_stale_222"],
        },
    ]


def test_young_ci_child_is_refused_not_deleted(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse a live run's child. Deleting this would break running acceptance.

    A cleanup that treated every CI-named child as disposable would delete the
    child of a concurrent, healthy run mid-acceptance. The age threshold is what
    keeps a second dispatch from destroying live work.
    """
    _factory_environment(monkeypatch)
    now = _freeze_now(lifecycle, monkeypatch)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-list-token"}),
            _listing_response(
                _organization_child(
                    child_id="tailnet_live_456",
                    display_name="Vonk Forge CI 35266396689 attempt 1",
                    created_at=_created_at(minutes_old=5, now=now),
                )
            ),
        ],
    )

    lifecycle.cleanup()

    assert _cleanup_requests(urlopen) == []


@pytest.mark.parametrize("minutes_old", [0, 59])
def test_ci_child_inside_the_stale_threshold_is_never_deleted(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    minutes_old: int,
) -> None:
    _factory_environment(monkeypatch)
    now = _freeze_now(lifecycle, monkeypatch)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-list-token"}),
            _listing_response(
                _organization_child(
                    child_id="tailnet_recent_456",
                    display_name="Vonk Forge CI 35266396689 attempt 1",
                    created_at=_created_at(minutes_old=minutes_old, now=now),
                )
            ),
        ],
    )

    lifecycle.cleanup()

    assert _cleanup_requests(urlopen) == []


def test_ci_child_at_the_stale_boundary_is_deleted(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _factory_environment(monkeypatch)
    now = _freeze_now(lifecycle, monkeypatch)
    child = _organization_child(
        child_id="tailnet_boundary_456",
        display_name="Vonk Forge CI 35266396689 attempt 1",
        created_at=_created_at(
            minutes_old=lifecycle.STALE_CHILD_AGE.total_seconds() / 60, now=now
        ),
    )
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [_Response({"access_token": "factory-list-token"}), _listing_response(child)]
        + _child_delete_responses("tailnet_boundary_456"),
    )

    lifecycle.cleanup()

    assert _deleted_ids(urlopen) == ["tailnet_boundary_456"]


def test_young_explicit_child_id_is_refused_rather_than_deleted(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _factory_environment(monkeypatch)
    now = _freeze_now(lifecycle, monkeypatch)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-list-token"}),
            _listing_response(
                _organization_child(
                    child_id="tailnet_live_456",
                    display_name="Vonk Forge CI 35266396689 attempt 1",
                    created_at=_created_at(minutes_old=5, now=now),
                )
            ),
        ],
    )

    with pytest.raises(lifecycle.LifecycleError, match="tailnet_live_456"):
        lifecycle.cleanup(child_id="tailnet_live_456")

    assert _cleanup_requests(urlopen) == []


def test_tailnet_without_the_ci_name_pattern_is_refused(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _factory_environment(monkeypatch)
    now = _freeze_now(lifecycle, monkeypatch)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-list-token"}),
            _listing_response(
                _organization_child(
                    child_id="tailnet_production_789",
                    display_name="Production",
                    created_at=_created_at(minutes_old=100000, now=now),
                )
            ),
        ],
    )

    lifecycle.cleanup()

    assert _cleanup_requests(urlopen) == []


def test_explicit_non_ci_child_id_is_refused_by_name(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _factory_environment(monkeypatch)
    now = _freeze_now(lifecycle, monkeypatch)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-list-token"}),
            _listing_response(
                _organization_child(
                    child_id="tailnet_production_789",
                    display_name="Production",
                    created_at=_created_at(minutes_old=100000, now=now),
                )
            ),
        ],
    )

    with pytest.raises(lifecycle.LifecycleError, match="tailnet_production_789"):
        lifecycle.cleanup(child_id="tailnet_production_789")

    assert _cleanup_requests(urlopen) == []


@pytest.mark.parametrize(
    ("display_name", "child_id", "created_at", "reason"),
    [
        (
            "Vonk Forge CI 1 attempt 1\u0000",
            "tailnet_bad_name",
            "stale",
            "unsafe CI child identity",
        ),
        (
            "Vonk Forge CI " + "x" * 80,
            "tailnet_long_name",
            "stale",
            "unsafe CI child identity",
        ),
        (
            "Vonk Forge CI 1 attempt 1",
            "tailnet\u0000bad",
            "stale",
            "unsafe CI child identity",
        ),
        (
            "Vonk Forge CI 1 attempt 1",
            "tailnet_ok",
            "not-a-timestamp",
            "invalid CI child timestamp",
        ),
        (
            "Vonk Forge CI 1 attempt 1",
            "tailnet_ok",
            "2020-01-01T00:00:00",
            "invalid CI child timestamp",
        ),
    ],
)
def test_ci_child_with_unvalidatable_identity_raises_instead_of_being_skipped(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    display_name: str,
    child_id: str,
    created_at: str,
    reason: str,
) -> None:
    """A malformed CI child is an explicit blocker, never a silent skip.

    Skipping it would leave the wedge in place while reporting success, which is
    the failure this whole path exists to remove; guessing it is stale would
    delete a network nothing here can attribute to a dead run.
    """
    _factory_environment(monkeypatch)
    now = _freeze_now(lifecycle, monkeypatch)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-list-token"}),
            _listing_response(
                _organization_child(
                    child_id=child_id,
                    display_name=display_name,
                    created_at=(
                        _created_at(minutes_old=90, now=now)
                        if created_at == "stale"
                        else created_at
                    ),
                )
            ),
        ],
    )

    with pytest.raises(lifecycle.LifecycleError, match=reason):
        lifecycle.cleanup()

    assert _cleanup_requests(urlopen) == []


def test_invalid_explicit_child_id_is_refused_before_the_api(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _factory_environment(monkeypatch)
    urlopen = _install_urlopen(lifecycle, monkeypatch, [])

    with pytest.raises(lifecycle.LifecycleError, match="ID is invalid"):
        lifecycle.cleanup(child_id="bad id")

    assert urlopen.requests == []


def test_absent_explicit_child_id_is_refused(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _factory_environment(monkeypatch)
    now = _freeze_now(lifecycle, monkeypatch)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-list-token"}),
            _listing_response(
                _organization_child(
                    child_id="tailnet_stale_999",
                    display_name="Vonk Forge CI 1 attempt 1",
                    created_at=_created_at(minutes_old=90, now=now),
                )
            ),
        ],
    )

    with pytest.raises(lifecycle.LifecycleError, match="tailnet_absent_555"):
        lifecycle.cleanup(child_id="tailnet_absent_555")

    assert _cleanup_requests(urlopen) == []


def test_cleanup_refuses_a_backlog_larger_than_the_bound(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _factory_environment(monkeypatch)
    now = _freeze_now(lifecycle, monkeypatch)
    monkeypatch.setattr(lifecycle, "MAX_CLEANUP_CHILDREN", 2)
    children = [
        _organization_child(
            child_id=f"tailnet_stale_{index}",
            display_name=f"Vonk Forge CI 1000{index} attempt 1",
            created_at=_created_at(minutes_old=90, now=now),
        )
        for index in range(3)
    ]
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-list-token"}),
            _listing_response(*children),
        ],
    )

    with pytest.raises(lifecycle.LifecycleError, match="bounded cleanup limit of 2"):
        lifecycle.cleanup()

    assert _cleanup_requests(urlopen) == []


def test_second_cleanup_run_with_nothing_stale_succeeds_and_deletes_nothing(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _factory_environment(monkeypatch)
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-list-token"}),
            _listing_response(),
        ],
    )

    lifecycle.cleanup()

    assert _cleanup_requests(urlopen) == []
    assert "deleted 0 of 0" in capsys.readouterr().out


def test_cleanup_never_prints_the_credential_or_an_access_token(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _factory_environment(monkeypatch)
    now = _freeze_now(lifecycle, monkeypatch)
    child = _organization_child(
        child_id="tailnet_stale_999",
        display_name="Vonk Forge CI 35232305043 attempt 1",
        created_at=_created_at(minutes_old=90, now=now),
    )
    _install_urlopen(
        lifecycle,
        monkeypatch,
        [_Response({"access_token": "factory-list-token"}), _listing_response(child)]
        + _child_delete_responses("tailnet_stale_999"),
    )

    lifecycle.cleanup()

    output = capsys.readouterr().out
    assert "factory-secret" not in output
    # The workflow command that registers a secret is the one place a token may
    # appear; every other line must be free of it.
    for secret in ("factory-list-token", "delete-token-tailnet_stale_999"):
        assert f"::add-mask::{secret}" in output
        assert [line for line in output.splitlines() if secret in line] == [
            f"::add-mask::{secret}"
        ]


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


@pytest.mark.parametrize("document", [None, "tailnet deleted"])
@pytest.mark.parametrize("raw", [None, b""])
def test_successful_delete_accepts_empty_null_or_scalar_response(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    document: object,
    raw: bytes | None,
) -> None:
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [_Response(document, raw=raw)],
    )

    response = lifecycle._request("DELETE", "/tailnet/tailnet_ci_123")

    assert response.document == {}
    assert len(urlopen.requests) == 1


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH"])
@pytest.mark.parametrize("document", [None, "request completed"])
def test_non_delete_scalar_response_remains_invalid(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    document: object,
) -> None:
    _install_urlopen(lifecycle, monkeypatch, [_Response(document)])

    with pytest.raises(lifecycle.LifecycleError, match="invalid document"):
        lifecycle._request(method, "/tailnet/tailnet_ci_123")


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


@pytest.mark.parametrize(
    ("failure", "cause"),
    (
        (
            urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
            "ConnectionRefusedError",
        ),
        (
            urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
            "gaierror",
        ),
        (urllib.error.URLError(TimeoutError("timed out")), "TimeoutError"),
        (TimeoutError("timed out"), "TimeoutError"),
    ),
)
def test_transport_failure_names_the_underlying_cause(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: BaseException,
    cause: str,
) -> None:
    """A "was unavailable" line has to say which transport failure it was.

    The disposable-tailnet flake reported only "was unavailable", which cannot
    distinguish a DNS failure from a refused connection from the 30-second read
    timeout that the step's ~31s duration implies. Retrying was deliberately not
    added, so the message is the only thing that makes the next occurrence
    diagnosable.
    """
    _factory_environment(monkeypatch)
    _install_urlopen(lifecycle, monkeypatch, [failure])

    with pytest.raises(lifecycle.ApiRequestError) as caught:
        lifecycle.create(
            display_name="Vonk Forge CI 123 attempt 1",
            github_env=tmp_path / "github.env",
            state=tmp_path / "state.json",
        )

    message = str(caught.value)
    assert "POST /oauth/token was unavailable" in message
    assert cause in message
    assert "\n" not in message


def test_cleanup_403_refusal_names_the_documented_scope_requirement(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 on the cleanup token exchange has to explain itself.

    `ApiRequestError` deliberately sanitizes a remote rejection to method, path
    and status because the body can carry credential material, and the API's
    403 body is the generic `{message}` document with no scope code. So the
    rendered line for the live promotion-blocking failure was only "HTTP 403":
    the operator could see that cleanup was refused but not which request failed
    or what the documented requirement is. Assert the five facts the refusal
    must carry: the failing request, the required `all` scope and its owner
    variable, the narrower scope listing uses, the admin-console check the
    operator has to make, and why this API path cannot be replaced by a console
    action.
    """
    _factory_environment(monkeypatch)
    now = _freeze_now(lifecycle, monkeypatch)
    child = _organization_child(
        child_id="tailnet_stale_999",
        display_name="Vonk Forge CI 35232305043 attempt 1",
        created_at=_created_at(minutes_old=90, now=now),
    )
    urlopen = _install_urlopen(
        lifecycle,
        monkeypatch,
        [
            _Response({"access_token": "factory-list-token"}),
            _listing_response(child),
            _http_error(403),
        ],
    )

    with pytest.raises(lifecycle.LifecycleError) as caught:
        lifecycle.cleanup()

    message = str(caught.value)
    assert "POST /oauth/token?tailnet=tailnet_stale_999" in message
    assert "HTTP 403" in message
    assert "`all` scope" in message
    assert "VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_ID" in message
    assert "`tailnets`" in message
    assert "Trust credentials" in message
    assert "admin console" in message
    # The refusal is a refusal: no second credential, endpoint or retry is
    # attempted, and the child is left for the operator rather than half-deleted.
    assert [request.method for request in urlopen.requests] == ["POST", "GET", "POST"]
    assert _cleanup_requests(urlopen) == []
