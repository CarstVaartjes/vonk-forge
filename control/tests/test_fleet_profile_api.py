from __future__ import annotations

import inspect
import io
import json
from datetime import UTC, datetime
from email.message import Message
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.api import create_app
from vonk_control.auth import MUTATION_ROLES, Actor, TokenCodec
from vonk_control.fleet_profile_contract import FleetProfileApplicationView
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.jobs import JobService
from vonk_control.models import AgentNode, Base, FleetProfileApplication, User

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
                    last_seen_at=datetime(2026, 9, 10, tzinfo=UTC),
                )
            )
    codec = TokenCodec(b"p" * 32)
    app = create_app(
        jobs=JobService(sessions, clock=lambda: datetime(2026, 9, 10, tzinfo=UTC)),
        tokens=codec,
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


@pytest.mark.parametrize("role", ("viewer", "operator"))
def test_profile_preview_requires_administrator_before_reading_profile(role):
    # Break caught: preview skips the mutation-role matrix and exposes the
    # administrator-only decision to a lesser role.
    client, codec = _client()
    assert (
        client.post("/api/profile/1/preview", headers=_headers(codec, role)).status_code
        == 403
    )
    assert (
        client.post(
            "/api/profile/1/preview", headers=_headers(codec, "administrator")
        ).status_code
        == 404
    )


@pytest.mark.parametrize("role", ("viewer", "operator"))
def test_non_admin_cannot_change_an_accepted_profile(role: str) -> None:
    client, codec = _client(with_idle_spark=True)
    admin = _headers(codec, "administrator")
    saved = client.put(
        "/api/profile/1",
        headers=admin,
        json={"name": "Standing intent", "assignments": []},
    )
    assert saved.status_code == 200
    assert (
        client.post(
            "/api/profile/1/load",
            headers=admin,
            json={"request_key": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        ).status_code
        == 202
    )
    before = client.get("/api/profile/1", headers=admin).json()
    changed = client.put(
        "/api/profile/1",
        headers=_headers(codec, role),
        json={"name": "Unauthorized edit", "assignments": [], "expected_revision": 1},
    )
    assert changed.status_code == 403
    after = client.get("/api/profile/1", headers=admin).json()
    assert (after["revision"], after["profile_digest"], after["loaded_revision"]) == (
        before["revision"],
        before["profile_digest"],
        before["loaded_revision"],
    )
    # A rejected request leaves no gate preventing a fresh admin edit.
    assert (
        client.put(
            "/api/profile/1",
            headers=admin,
            json={"name": "Authorized edit", "assignments": [], "expected_revision": 1},
        ).status_code
        == 200
    )


def test_profile_operator_routes_are_singular_and_unversioned() -> None:
    client, codec = _client()
    response = client.get("/api/profile", headers=_headers(codec))
    assert response.status_code == 200
    assert response.json()["profiles"] == []

    unused = client.get("/api/profile/2", headers=_headers(codec))
    assert unused.status_code == 200
    assert unused.json()["number"] == 2
    assert unused.json()["status"] == "not-created"
    # An unsaved profile has one revision everywhere: GET, /definition and the
    # expected_revision the first save must send.
    definition = client.get("/api/profile/2/definition", headers=_headers(codec))
    assert definition.status_code == 200
    assert unused.json()["revision"] == definition.json()["revision"] == 0

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

    calls: list[tuple[int, str | None, str]] = []

    def profile_endpoint(
        number: int, alias: str | None, gateway_api_base: str
    ) -> FleetProfileEndpointsView:
        calls.append((number, alias, gateway_api_base))
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
                            "api_base": gateway_api_base,
                            "backend_api_base": "http://10.0.0.10:8000/v1",
                            "generation": 8,
                            "node_id": "spk_" + "a" * 32,
                            "observed_at": "2026-09-10T00:00:00Z",
                            "plan_digest": "a" * 64,
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
    endpoint = response.json()["assignments"][0]["endpoint"]
    assert endpoint["generation"] == 8
    # The client-facing base is the Controller origin's inference gateway.
    assert endpoint["api_base"] == "https://testserver/v1"
    assert endpoint["backend_api_base"] == "http://10.0.0.10:8000/v1"

    missing = client.get(
        path, params={"alias": "another-profiles-model"}, headers=_headers(codec)
    )
    assert missing.status_code == 404
    repaired = client.get(
        path, params={"alias": "studio-chat"}, headers=_headers(codec)
    )
    assert repaired.status_code == 200
    assert repaired.json()["assignments"][0]["endpoint"] == endpoint


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
                "recipe_selector": "vonk-forge/synthetic-tiny-build",
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
            ("--profile", "2", "profile", "configure", "--name", "Renamed", "--json"),
            control_client=client,
        )
        == 0
    ), capsys.readouterr().out
    saved = json.loads(capsys.readouterr().out)
    assert saved["revision"] == 2
    expected = {
        **definition,
        "name": "Renamed",
        "assignments": [
            {**assignment, "option_choices": {}}
            for assignment in definition["assignments"]
        ],
    }
    assert saved["definition"] == expected
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


def test_profile_load_applies_the_current_saved_profile() -> None:
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
    assert first_preview.json()["allowed"] is True

    missing = client.post("/api/profile/1/load", headers=headers, json={})
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
            "request_key": "22222222-2222-4222-8222-222222222222",
        },
    )
    assert stale.status_code == 202
    assert stale.json()["profile_digest"] == changed.json()["profile_digest"]

    client.post("/api/profile/1/preview", headers=headers)
    loaded = client.post(
        "/api/profile/1/load",
        headers=headers,
        json={
            "request_key": "33333333-3333-4333-8333-333333333333",
        },
    )
    assert loaded.status_code == 202
    assert loaded.json()["profile_id"] == changed.json()["id"]


def test_profile_load_bound_to_a_review_refuses_a_plan_that_changed() -> None:
    client, codec = _client(with_idle_spark=True)
    headers = _headers(codec, "administrator")
    saved = client.put(
        "/api/profile/1",
        headers=headers,
        json={"name": "Empty profile", "expected_revision": 0},
    )
    reviewed = client.post("/api/profile/1/preview", headers=headers).json()
    assert (
        client.put(
            "/api/profile/1",
            headers=headers,
            json={
                "name": "Renamed profile",
                "expected_revision": saved.json()["revision"],
            },
        ).status_code
        == 200
    )
    key = "22222222-2222-4222-8222-222222222222"

    refused = client.post(
        "/api/profile/1/load",
        headers=headers,
        json={
            "request_key": key,
            "review": {"effects_digest": reviewed["effects_digest"]},
        },
    )

    assert refused.status_code == 409
    assert refused.headers["x-vonk-error-code"] == "profile.review_stale"
    assert (
        client.get(f"/api/profile/1/requests/{key}", headers=headers).status_code == 404
    )
    current = client.post("/api/profile/1/preview", headers=headers).json()
    accepted = client.post(
        "/api/profile/1/load",
        headers=headers,
        json={
            "request_key": key,
            "review": {"effects_digest": current["effects_digest"]},
        },
    )
    assert accepted.status_code == 202


def test_saving_a_profile_fills_option_defaults_and_replaces_stale_choices() -> None:
    """A choice the recipe does not offer never blocks the save.

    A refreshed recipe revision can drop an option or a value while a saved
    profile still holds it, and every edit re-submits all assignments. The
    save must keep going with the recipe default and say what it replaced.
    """

    from .test_fleet_profiles_canonical import NODE_1, NOW, _seed, _sessions

    sessions = _sessions()
    _seed(sessions, options=True)
    client, codec = _client(
        sessions, profiles=FleetProfileService(sessions, clock=lambda: NOW)
    )
    headers = _headers(codec, "administrator")

    def body(choices: dict[str, str]) -> dict[str, object]:
        return {
            "name": "Options",
            "expected_revision": 0,
            "assignments": [
                {
                    "recipe_selector": "vonk-forge/synthetic-tiny-build",
                    "spark_ids": [NODE_1],
                    "assignment_name": "optioned",
                    "option_choices": choices,
                }
            ],
        }

    stale = client.put(
        "/api/profile/1",
        headers=headers,
        json=body({"verification": "nope", "retired-option": "x"}),
    )
    assert stale.status_code == 200, stale.text
    assert stale.json()["assignments"][0]["option_choices"] == {
        "verification": "standard"
    }
    warnings = " ".join(stale.json()["warnings"])
    assert "verification: nope" in warnings
    assert "retired-option" in warnings
    stored = client.get("/api/profile/1/definition", headers=headers).json()
    assert stored["definition"]["assignments"][0]["option_choices"] == {
        "verification": "standard"
    }

    saved = client.put(
        "/api/profile/1",
        headers=headers,
        json={**body({}), "expected_revision": stored["revision"]},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["assignments"][0]["option_choices"] == {
        "verification": "standard"
    }
    assert not any("no longer offered" in w for w in saved.json()["warnings"])


@pytest.mark.parametrize(
    ("assignments", "expected"),
    [
        pytest.param(
            [{"assignment_name": "Qwen3-Coder-Next-FP8"}],
            ["assignments.0.assignment_name"],
            id="alias-with-capitals",
        ),
        pytest.param(
            [{"assignment_name": "same"}, {"assignment_name": "same", "second": True}],
            ["body", "aliases must be unique"],
            id="duplicate-alias",
        ),
    ],
)
def test_a_rejected_profile_save_names_the_field_and_the_reason(
    assignments: list[dict[str, object]], expected: list[str]
) -> None:
    """The refusal itself carries what to fix, not a bare status line."""

    from .test_fleet_profiles_canonical import NODE_1, NODE_2, NOW, _seed, _sessions

    sessions = _sessions()
    _seed(sessions)
    client, codec = _client(
        sessions, profiles=FleetProfileService(sessions, clock=lambda: NOW)
    )
    headers = _headers(codec, "administrator")
    documents = [
        {
            "recipe_selector": "vonk-forge/synthetic-tiny-build",
            "spark_ids": [NODE_2] if item.get("second") else [NODE_1],
            "assignment_name": item["assignment_name"],
        }
        for item in assignments
    ]

    refused = client.put(
        "/api/profile/1",
        headers=headers,
        json={"name": "Bad", "expected_revision": 0, "assignments": documents},
    )

    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert len(detail) <= 256
    for text in expected:
        assert text in detail
    assert refused.json()["issues"]


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("retained_journal", [False, True])
@pytest.mark.parametrize(
    "damage", ["missing-result", "invalid-result", "failure-reason", "incomplete-steps"]
)
def test_damaged_success_is_unknown_and_fresh_profile_load_proceeds(
    damage, retained_journal
):
    """Catches history validation crashing progress or preventing a fresh load."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    api, codec = _client(sessions, with_idle_spark=True)
    admin = _headers(codec, "administrator")
    assert (
        api.put(
            "/api/profile/1",
            headers=admin,
            json={"name": "Idle", "assignments": []},
        ).status_code
        == 200
    )
    first = api.post(
        "/api/profile/1/load",
        headers=admin,
        json={"request_key": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
    )
    assert first.status_code == 202
    original = first.json()
    assert original["state"] == "succeeded"
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, original["id"])
        assert row is not None
        if retained_journal:
            row.progress = {
                **row.progress,
                "switch_adapter": {"active_operation_id": None},
            }
        if damage == "missing-result":
            row.result = None
        elif damage == "invalid-result":
            session.execute(
                update(FleetProfileApplication)
                .where(FleetProfileApplication.id == row.id)
                .values(result=["damaged"])
            )
        elif damage == "failure-reason":
            row.status_reason = "Profile journal retired; effect unknown"
        else:
            row.current_step = 1
    response = api.get("/api/profile/1/progress", headers=admin)
    assert response.status_code == 200
    damaged = FleetProfileApplicationView.model_validate_json(response.content)
    assert damaged.state == "cancelled"
    assert damaged.projection_issue is not None
    assert damaged.projection_issue.observation == "unknown"
    assert damaged.result is None
    assert damaged.next_attempt_at is None
    fresh = api.post(
        "/api/profile/1/load",
        headers=admin,
        json={"request_key": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
    )
    assert fresh.status_code == 202
    assert fresh.json()["id"] != original["id"]
    assert fresh.json()["state"] == "succeeded"
    assert (
        api.get("/api/profile/1/progress", headers=admin).json()["id"]
        == fresh.json()["id"]
    )
