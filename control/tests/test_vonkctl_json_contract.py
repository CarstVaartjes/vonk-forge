"""The vonkctl --json client contract against the real Controller.

Each published document kind is produced by the real command over the real
API, then checked against the published JSON Schema file (what a third-party
client sees) and against the strict Controller model (the exact shape).
"""

from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cluster_profiles import cli
from cluster_profiles.cli_json_contract import (
    DOCUMENT_VERSIONS,
    DocumentKind,
    document_kind,
)
from cluster_profiles.control_client.transfers import ControlClient
from vonk_control.api.application import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.fleet_profiles.service import FleetProfileService
from vonk_control.fleet_projection.service import FleetProjection
from vonk_control.jobs.queue import JobService
from vonk_control.library.api import install_library_routes
from vonk_control.model_primitives import Base
from vonk_control.operation_api.durable import durable_operation_services

from .conftest import load_tools_module
from .test_catalog_entities import _model, _recipe
from .test_catalog_head_reads import _library, _publish, catalog  # noqa: F401
from .test_fleet_profile_api import _client as _profile_api

_contract = load_tools_module("vonkctl_json_contract")
DOCUMENTS = _contract.DOCUMENTS
incompatibilities = _contract.incompatibilities
schema_filename = _contract.schema_filename
schema_text = _contract.schema_text
schema_version = _contract.schema_version

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas" / "vonkctl"


def _control_client(api: TestClient, tmp_path: Path, token: str) -> ControlClient:
    """A CLI client whose transport is the in-memory API."""

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
        return Response(
            api.request(
                request.get_method(),
                request.full_url,
                headers=dict(request.header_items()),
                content=request.data,
            )
        )

    return ControlClient("https://forge.example.test", token_file, opener=opener)


def _published(
    client: ControlClient, capsys, kinds_seen: set[DocumentKind], *argv: str
) -> dict[str, Any]:
    """Run a command, return its document after proving it against its schema."""

    status = cli.main(("--json", "--no-input", *argv), control_client=client)
    printed = capsys.readouterr().out
    assert status == 0, printed
    document = json.loads(printed)
    kind = _kind_of(argv, document)
    kinds_seen.add(kind)
    _prove(kind, document, printed)
    return document


def _prove(kind: DocumentKind, document: dict[str, Any], printed: str) -> None:
    """The printed document satisfies its published schema and its strict model."""

    assert document["schema_version"] == schema_version(kind)
    _contract.validator(kind, SCHEMAS).validate(document)
    DOCUMENTS[kind].validate_json(printed)


class _KeepsRunning:
    """The real client, with one application reported as still running."""

    def __init__(self, client: ControlClient, application_id: str) -> None:
        self._client = client
        self._path = f"/api/profile/applications/{application_id}"

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    def request(self, method, path, *args, **kwargs):
        document = self._client.request(method, path, *args, **kwargs)
        if method == "GET" and path == self._path:
            return {**document, "state": "running", "result": None}
        return document


def _failed(client: object, capsys, *argv: str) -> dict[str, Any]:
    """Run a command that prints a failure or a followed-command observation."""

    status = cli.main(("--json", "--no-input", *argv), control_client=client)
    printed = capsys.readouterr().out
    assert status != 0, printed
    document = json.loads(printed)
    kind = (
        DocumentKind.OBSERVATION
        if "observation" in document and "error" not in document
        else DocumentKind.ERROR
    )
    _prove(kind, document, printed)
    return document


def _kind_of(argv: tuple[str, ...], document: dict[str, Any]) -> DocumentKind:
    args = cli._parser().parse_args(("--json", "--no-input", *argv))
    kind = document_kind(args, document, error=False)
    assert kind is not None, argv
    return kind


def _world(tmp_path: Path) -> tuple[ControlClient, TestClient, dict[str, Any]]:
    """A real Controller API with one saved empty profile, and a CLI client for it."""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    # Seeds one idle Spark into the sessions; the app it returns is not used.
    _profile_api(sessions, with_idle_spark=True)

    def clock():
        return datetime(2026, 9, 10, tzinfo=UTC)

    codec = TokenCodec(b"p" * 32)
    profiles = FleetProfileService(sessions, clock=clock)
    app = create_app(
        jobs=JobService(sessions, clock=clock),
        tokens=codec,
        fleet_projection=FleetProjection(sessions, clock=clock),
        fleet_profiles=profiles,
        operations=durable_operation_services(
            sessions,
            tmp_path,
            clock=clock,
            cursors=codec.cursor_codec(),
            profile_endpoint_intent=profiles.endpoint_intent,
        ),
        now=lambda: 1,
    )
    api = TestClient(app)
    token = codec.issue(Actor("admin", "administrator"), ttl_seconds=100, now=0)
    headers = {"Authorization": f"Bearer {token}"}
    assert (
        api.put(
            "/api/profile/1",
            headers=headers,
            json={"name": "Empty profile", "expected_revision": 0},
        ).status_code
        == 200
    )
    return _control_client(api, tmp_path, token), api, headers


def test_every_fleet_and_profile_document_matches_its_published_schema(
    tmp_path, capsys
) -> None:
    client, _, _ = _world(tmp_path)
    seen: set[DocumentKind] = set()

    _published(client, capsys, seen, "fleet")
    listing = _published(client, capsys, seen, "profile", "list")
    assert listing["profiles"]
    _published(client, capsys, seen, "--profile", "1", "profile")
    review = _published(
        client, capsys, seen, "--profile", "1", "profile", "load", "--review"
    )
    assert review["schema_version"] == schema_version(DocumentKind.PROFILE_REVIEW) == 3
    _published(
        client,
        capsys,
        seen,
        "--profile",
        "1",
        "profile",
        "load",
        "--yes",
        "--detach",
        "--request-key",
        "33333333-3333-4333-8333-333333333333",
    )
    _published(client, capsys, seen, "--profile", "1", "profile", "progress")
    _published(client, capsys, seen, "--profile", "1", "profile", "endpoint")
    assert seen >= {
        DocumentKind.FLEET,
        DocumentKind.PROFILE_LIST,
        DocumentKind.PROFILE,
        DocumentKind.PROFILE_REVIEW,
        DocumentKind.PROFILE_APPLICATION,
        DocumentKind.PROFILE_ENDPOINTS,
    }


def test_library_documents_match_their_published_schemas(
    catalog,  # noqa: F811
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    model = _model()
    _publish(catalog, model)
    first = _publish(catalog, _recipe(model))
    app = FastAPI()
    install_library_routes(
        app,
        actor_dependency=Depends(lambda: Actor("test", "administrator")),
        projection=_library(catalog),
    )
    client = _control_client(TestClient(app), tmp_path, "synthetic-token")
    seen: set[DocumentKind] = set()
    _published(client, capsys, seen, "recipe")
    detail = _published(
        client, capsys, seen, "recipe", "detail", f"{first.publisher}/{first.slug}"
    )
    assert detail["identity"]["recipe_revision_id"] == first.id
    assert seen == {DocumentKind.RECIPE_LIBRARY, DocumentKind.RECIPE_DETAIL}


def test_cancel_follow_and_controller_failures_match_their_published_schemas(
    tmp_path, capsys
) -> None:
    client, _, _ = _world(tmp_path)
    seen: set[DocumentKind] = set()
    key = "44444444-4444-4444-8444-444444444444"
    loaded = _published(
        client,
        capsys,
        seen,
        "--profile",
        "1",
        "profile",
        "load",
        "--yes",
        "--detach",
        "--request-key",
        key,
    )

    # A followed command that runs out of time prints the observation wrapper:
    # the last confirmed document, tagged by its kind. The application stays
    # running on the wire so the follow has something to wait for.
    running = _KeepsRunning(client, loaded["id"])
    observed = _failed(
        running,
        capsys,
        "--profile",
        "1",
        "profile",
        "progress",
        "--load",
        loaded["id"],
        "--follow",
        "--timeout-seconds",
        "1",
        "--interval-seconds",
        "0.1",
    )
    assert observed["result_kind"] == DocumentKind.PROFILE_APPLICATION
    assert observed["result"]["schema_version"] == schema_version(
        DocumentKind.PROFILE_APPLICATION
    )

    # The application ended, so the Controller refuses the cancel: a typed
    # failure with its code, detail, recovery actions and unconfirmed submission.
    refused_cancel = _failed(
        client,
        capsys,
        "--profile",
        "1",
        "profile",
        "cancel",
        loaded["id"],
        "--yes",
        "--detach",
    )
    assert refused_cancel["code"] == "profile.request_invalid"
    assert refused_cancel["submission"]["acceptance"] == "refused"

    # A Controller refusal: its code, detail and recovery actions are typed.
    refused = _failed(client, capsys, "--profile", "99", "profile", "load", "--review")
    assert refused["error_type"] == "control_api"
    assert refused["code"]
    assert refused["detail"]
    assert isinstance(refused["recovery_actions"], list)


def test_incompatible_schema_edits_are_detected() -> None:
    """Only additive edits are compatible; removals, retyping and new required fields are not."""

    base = {
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a"],
    }
    assert incompatibilities(base, base) == []
    # Additive changes keep the version.
    assert (
        incompatibilities(
            base,
            {
                "properties": {**base["properties"], "c": {"type": "string"}},
                "required": ["a"],
            },
        )
        == []
    )
    # Removing a property, requiring more, or changing a type does not.
    assert incompatibilities(
        base, {"properties": {"a": {"type": "string"}}, "required": ["a"]}
    )
    assert incompatibilities(base, {**base, "required": ["a", "b"]})
    assert incompatibilities(base, {**base, "required": []})
    # A union that drops an alternative nobody emits any more keeps the version.
    union = {"anyOf": [{"$ref": "#/A"}, {"$ref": "#/B"}, {"$ref": "#/C"}]}
    assert incompatibilities(union, {"anyOf": [{"$ref": "#/A"}, {"$ref": "#/C"}]}) == []
    assert incompatibilities(
        base,
        {
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a"],
        },
    )


def test_a_ref_to_the_old_definition_plus_optional_fields_keeps_the_version() -> None:
    def doc(target: str, extra: dict[str, Any]) -> dict[str, Any]:
        return {
            "properties": {"knob": {"$ref": f"#/$defs/{target}"}},
            "$defs": {
                "Setting": {"properties": {"value": {"type": "string"}}},
                target: {"properties": {"value": {"type": "string"}, **extra}},
            },
        }

    old = doc("Setting", {})
    assert incompatibilities(old, doc("Knob", {"step": {"type": "integer"}})) == []
    # A recursive definition renamed (Node to Node2) terminates as compatible.
    node = {"properties": {"next": {"$ref": "#/$defs/Node"}}}
    node2 = {"properties": {"next": {"$ref": "#/$defs/Node2"}}}
    tree = {"properties": {"root": {"$ref": "#/$defs/Node"}}, "$defs": {"Node": node}}
    renamed = {
        "properties": {"root": {"$ref": "#/$defs/Node2"}},
        "$defs": {"Node2": node2},
    }
    assert incompatibilities(tree, renamed) == []
    dropped = doc("Knob", {})
    dropped["$defs"]["Knob"] = {"properties": {}}
    assert incompatibilities(old, dropped)


def test_schema_version_is_required_and_owned_by_one_model() -> None:
    shared = _contract.definitions_schema()["$defs"]
    for kind in DOCUMENTS:
        schema = json.loads(schema_text(kind))
        members = (
            [schema]
            if "properties" in schema
            else [
                shared[alternative["$ref"].rsplit("/", 1)[-1]]
                for alternative in schema.get("anyOf", schema.get("oneOf", []))
            ]
        )
        assert members, kind
        for member in members:
            assert "schema_version" in member.get("required", []), kind
    # The review's number lives on its Controller model alone.
    assert DocumentKind.PROFILE_REVIEW not in DOCUMENT_VERSIONS


def test_published_schema_files_are_the_models_current_export() -> None:
    assert {path.name for path in SCHEMAS.glob("*.schema.json")} == {
        schema_filename(kind) for kind in DOCUMENTS
    } | {_contract.DEFINITIONS_FILENAME}
    for kind in DOCUMENTS:
        assert (SCHEMAS / schema_filename(kind)).read_text() == schema_text(kind), (
            f"{kind}: run python tools/vonkctl_json_contract.py schemas/vonkctl"
        )
        jsonschema.Draft202012Validator.check_schema(json.loads(schema_text(kind)))
    assert (
        SCHEMAS / _contract.DEFINITIONS_FILENAME
    ).read_text() == _contract.definitions_text(), "run tools/vonkctl_json_contract.py"


def test_definitions_live_only_in_the_shared_file() -> None:
    """A kind file keeps no ``$defs``; every ``$ref`` of it resolves in the shared file."""

    shared = json.loads((SCHEMAS / _contract.DEFINITIONS_FILENAME).read_text())["$defs"]
    prefix = f"{_contract.DEFINITIONS_FILENAME}#/$defs/"
    for kind in DOCUMENTS:
        text = (SCHEMAS / schema_filename(kind)).read_text()
        assert '"$defs"' not in text, kind
        refs = set(re.findall(r'"\$ref": "([^"]+)"', text))
        assert all(ref.startswith(prefix) for ref in refs), kind
        assert {ref.removeprefix(prefix) for ref in refs} <= shared.keys(), kind


def test_a_shared_definition_still_rejects_an_invalid_document() -> None:
    kind = DocumentKind.ERROR
    good = {"schema_version": schema_version(kind), "error": "x", "error_type": "local"}
    validator = _contract.validator(kind, SCHEMAS)
    validator.validate(good)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**good, "error_type": "not-a-type"})


def test_every_published_kind_names_its_version_in_schema_and_file() -> None:
    for kind in DOCUMENTS:
        version = schema_version(kind)
        schema = json.loads(schema_text(kind))
        assert schema_filename(kind).endswith(f"-v{version}.schema.json")
        assert "schema_version" in json.dumps(schema)
