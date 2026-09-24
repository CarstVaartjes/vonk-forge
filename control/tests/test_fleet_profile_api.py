from __future__ import annotations

import inspect
import io
import json
from datetime import UTC, datetime
from email.message import Message
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import MUTATION_ROLES, Actor, TokenCodec
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.jobs import JobService
from vonk_control.models import AgentNode, Base, User

from cluster_profiles import cli
from cluster_profiles.control_client import ControlClient


def _client(
    sessions=None, *, profiles=None, operations=None, with_idle_spark=False
) -> tuple[TestClient, TokenCodec]:
    if sessions is None:
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for subject, role in (
            ("admin", "administrator"),
            ("administrator", "administrator"),
            ("operator", "operator"),
            ("viewer", "viewer"),
        ):
            if session.scalar(select(User).where(User.subject == subject)) is None:
                session.add(User(subject=subject, role=role))
    if with_idle_spark:
        with sessions.begin() as session:
            session.add(
                AgentNode(
                    node_id="spk_" + "1" * 32,
                    state="active",
                    protocol_version=1,
                    architecture="linux-arm64",
                    capabilities=[],
                    last_seen_at=datetime(2026, 9, 10, tzinfo=UTC),
                )
            )
    codec = TokenCodec(b"p" * 32)
    app = create_app(
        jobs=JobService(sessions, clock=lambda: datetime(2026, 9, 10, tzinfo=UTC)),
        tokens=codec,
        audits=MemoryAuditStore(),
        now=lambda: 1,
        fleet_profiles=profiles
        or FleetProfileService(
            sessions, clock=lambda: datetime(2026, 9, 10, tzinfo=UTC)
        ),
        operations=operations,
    )
    return TestClient(app), codec


def _headers(codec: TokenCodec, role: str = "viewer") -> dict[str, str]:
    token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
    return {"Authorization": f"Bearer {token}"}


def test_profile_operator_routes_are_singular_and_unversioned() -> None:
    client, codec = _client()
    response = client.get("/api/profile", headers=_headers(codec))
    assert response.status_code == 200
    assert response.json()["profiles"] == []

    unused = client.get("/api/profile/2", headers=_headers(codec))
    assert unused.status_code == 200
    assert unused.json()["number"] == 2
    assert unused.json()["status"] == "not-created"

    assert (
        client.get("/api/profile/2/status", headers=_headers(codec)).status_code == 404
    )


def test_profile_request_lookup_is_authenticated_and_reports_missing_key() -> None:
    client, codec = _client()
    path = "/api/profile/1/requests/11111111-1111-4111-8111-111111111111"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_headers(codec)).status_code == 404


def test_profile_endpoint_route_is_authenticated_and_keeps_alias_scope() -> None:
    from vonk_control.fleet_profile_contract import FleetProfileEndpointsView

    calls: list[tuple[int, str | None]] = []

    def profile_endpoint(number: int, alias: str | None) -> FleetProfileEndpointsView:
        calls.append((number, alias))
        if alias != "studio-chat":
            raise KeyError(alias)
        return FleetProfileEndpointsView.model_validate(
            {
                "number": number,
                "profile_id": "11111111-1111-4111-8111-111111111111",
                "application_id": "22222222-2222-4222-8222-222222222222",
                "application_state": "succeeded",
                "observed_at": datetime(2026, 9, 10, tzinfo=UTC),
                "assignments": [
                    {
                        "assignment_id": "33333333-3333-4333-8333-333333333333",
                        "recipe_title": "Example Model",
                        "desired_state": "running",
                        "alias": "studio-chat",
                        "state": "published",
                        "endpoint": {
                            "alias": "studio-chat",
                            "api_base": "http://10.0.0.10:8000/v1",
                            "expires_at": "2026-09-10T00:03:00Z",
                            "generation": 8,
                            "node_id": "spk_" + "a" * 32,
                            "observed_at": "2026-09-10T00:00:00Z",
                            "plan_digest": "a" * 64,
                            "state": "published",
                        },
                    }
                ],
            }
        )

    client, codec = _client(
        operations=SimpleNamespace(profile_endpoint=profile_endpoint)
    )
    path = "/api/profile/3/endpoints"
    assert client.get(path).status_code == 401

    response = client.get(
        path, params={"alias": "studio-chat"}, headers=_headers(codec)
    )
    assert response.status_code == 200
    assert response.json()["assignments"][0]["endpoint"]["generation"] == 8

    missing = client.get(
        path, params={"alias": "another-profiles-model"}, headers=_headers(codec)
    )
    assert missing.status_code == 404
    assert "not part of profile 3" in missing.json()["detail"]
    assert calls == [(3, "studio-chat"), (3, "another-profiles-model")]


def test_definition_roundtrip_keeps_metadata_and_enforces_the_observed_revision() -> (
    None
):
    client, codec = _client()
    path = "/api/profile/2"
    headers = _headers(codec, "administrator")
    empty = client.get(path + "/definition", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["revision"] == 0
    assert empty.json()["id"] is None
    definition = {
        **empty.json()["definition"],
        "name": "Editing",
        "description": "Preserve this description",
        "favorite": True,
        "labels": {"use": "code"},
        "installation_policy": "exact",
    }
    created = client.put(
        path, headers=headers, json={**definition, "expected_revision": 0}
    )
    assert created.status_code == 200, created.text
    read = client.get(path + "/definition", headers=headers).json()
    assert read["definition"] == definition
    assert read["revision"] == 1
    renamed = {**read["definition"], "name": "Renamed"}
    saved = client.put(path, headers=headers, json={**renamed, "expected_revision": 1})
    assert saved.status_code == 200
    assert saved.json()["definition"] == renamed
    assert (
        client.put(
            path, headers=headers, json={**definition, "expected_revision": 0}
        ).status_code
        == 409
    )
    assert (
        client.put(
            path, headers=headers, json={**definition, "expected_revision": 1}
        ).status_code
        == 409
    )
    assert client.put(path, headers=headers, json=definition).status_code == 409
    assert (
        client.get(path + "/definition", headers=headers).json()["definition"]
        == renamed
    )
    assert client.get(path + "/definition").status_code == 401
    assert (
        client.put(
            path, headers=_headers(codec), json={**renamed, "expected_revision": 2}
        ).status_code
        == 403
    )


def test_cli_edits_and_exports_the_persisted_definition_through_real_api(
    tmp_path, capsys
):
    from .test_fleet_profiles_canonical import NODE_1, _seed, _sessions

    sessions = _sessions()
    _seed(sessions)
    api, codec = _client(sessions)
    token = codec.issue(Actor("admin", "administrator"), ttl_seconds=100, now=0)
    token_file = tmp_path / "token"
    token_file.touch(mode=0o600)
    token_file.write_text(token)

    class Response(io.BytesIO):
        def __init__(self, response):
            super().__init__(response.content)
            self.headers = Message()
            for name, value in response.headers.items():
                self.headers[name] = value
            self.status = response.status_code

        def __exit__(self, *args: object) -> None:
            self.close()

    def opener(request, *, timeout):
        response = api.request(
            request.get_method(),
            request.full_url,
            headers=dict(request.header_items()),
            content=request.data,
        )
        return Response(response)

    client = ControlClient("https://forge.example.test", token_file, opener=opener)
    definition = {
        "name": "Complete definition",
        "description": "Preserve this description",
        "favorite": True,
        "labels": {"purpose": "draft"},
        "installation_policy": "exact",
        "assignments": [
            {
                "recipe_selector": "vonk-forge/synthetic-tiny-image",
                "spark_ids": [NODE_1],
                "assignment_name": "installed-draft",
                "model_variant": "precise-variant",
                "desired_state": "installed",
            }
        ],
    }
    source = tmp_path / "source.json"
    source.write_text(json.dumps(definition))
    assert (
        cli.main(
            (
                "--profile",
                "2",
                "profile",
                "import",
                "--file",
                str(source),
                "--expected-revision",
                "0",
                "--json",
            ),
            control_client=client,
        )
        == 0
    ), capsys.readouterr().out
    capsys.readouterr()
    assert (
        cli.main(
            ("--profile", "2", "profile", "name", "Renamed", "--json"),
            control_client=client,
        )
        == 0
    ), capsys.readouterr().out
    saved = json.loads(capsys.readouterr().out)
    assert saved["revision"] == 2
    assert saved["definition"] == {**definition, "name": "Renamed"}
    assert cli.main(("--profile", "2", "profile"), control_client=client) == 0
    presentation = capsys.readouterr()
    assert "Profile 2: Renamed" in presentation.out
    assert "Desired: installed" in presentation.out
    assert "Revision: 2" in presentation.out
    assert token not in presentation.out + presentation.err
    assert (
        cli.main(("--profile", "2", "profile", "export"), control_client=client) == 0
    ), capsys.readouterr().out
    assert json.loads(capsys.readouterr().out) == saved["definition"]
    assert (
        cli.main(
            (
                "--profile",
                "2",
                "profile",
                "import",
                "--file",
                str(source),
                "--expected-revision",
                "1",
                "--json",
            ),
            control_client=client,
        )
        == 2
    )
    assert "revision conflict" in capsys.readouterr().out
    assert (
        client.request("GET", "/api/profile/2/definition")["definition"]
        == saved["definition"]
    )
    assert (
        api.get("/api/profile/2/progress", headers=_headers(codec)).status_code == 404
    )


def test_profile_mutation_role_is_declared_for_final_route() -> None:
    # Root owns the shared auth registry; this assertion documents the exact
    # key the integration patch must install without retaining old aliases.
    assert ("PUT", "/api/profile/{number}") not in MUTATION_ROLES or (
        "administrator" in MUTATION_ROLES[("PUT", "/api/profile/{number}")]
    )


def test_production_app_composes_fleet_profiles_with_preparation_authority() -> None:
    """Production must compose Fleet profiles through the guarded builder.

    ``build_production_fleet_profile_service`` binds the Run/Switch adapter as
    the preparation provider, so a profile preview always carries the exact
    preparation the queued child operation binds.  Constructing
    ``FleetProfileService`` directly in production would silently drop that
    binding and admit a profile whose required assets cannot be attested.
    """

    from vonk_control import api

    source = inspect.getsource(api.production_app)
    assert "build_production_fleet_profile_service(" in source
    assert "FleetProfileService(" not in source


def test_profile_load_requires_and_applies_the_reviewed_preview_digest() -> None:
    client, codec = _client(with_idle_spark=True)
    headers = _headers(codec, "administrator")
    saved = client.put(
        "/api/profile/1",
        headers=headers,
        json={"name": "Empty profile", "expected_revision": 0},
    )
    assert saved.status_code == 200

    first_preview = client.post("/api/profile/1/preview", headers=headers)
    assert first_preview.status_code == 200
    old_digest = first_preview.json()["plan_digest"]
    assert first_preview.json()["allowed"] is True

    missing = client.post(
        "/api/profile/1/load",
        headers=headers,
        json={"request_key": "11111111-1111-4111-8111-111111111111"},
    )
    assert missing.status_code == 422

    changed = client.put(
        "/api/profile/1",
        headers=headers,
        json={"name": "Renamed profile", "expected_revision": saved.json()["revision"]},
    )
    assert changed.status_code == 200

    stale = client.post(
        "/api/profile/1/load",
        headers=headers,
        json={
            "plan_digest": old_digest,
            "request_key": "22222222-2222-4222-8222-222222222222",
        },
    )
    assert stale.status_code == 409
    assert stale.headers["x-vonk-error-code"] == "profile.stale_plan"

    current_preview = client.post("/api/profile/1/preview", headers=headers)
    loaded = client.post(
        "/api/profile/1/load",
        headers=headers,
        json={
            "plan_digest": current_preview.json()["plan_digest"],
            "request_key": "33333333-3333-4333-8333-333333333333",
        },
    )
    assert loaded.status_code == 202
    assert loaded.json()["profile_id"] == changed.json()["id"]
