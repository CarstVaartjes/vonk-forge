"""The vonkctl --json client contract against the real Controller.

Each published document kind is produced by the real command over the real
API, then checked against the published JSON Schema file (what a third-party
client sees) and against the strict Controller model (the exact shape).
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path

import jsonschema
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.fleet_projection import FleetProjection
from vonk_control.jobs import JobService
from vonk_control.library_api import install_library_routes
from vonk_control.operation_api import durable_operation_services
from vonk_control.vonkctl_json_contract import (
    DOCUMENTS,
    schema_filename,
    schema_text,
)

from cluster_profiles import cli
from cluster_profiles.cli_json_contract import (
    DOCUMENT_VERSIONS,
    DocumentKind,
    document_kind,
)
from cluster_profiles.control_client import ControlClient

from .test_catalog_entities import _model, _recipe
from .test_catalog_head_reads import _library, _publish, catalog  # noqa: F401
from .test_fleet_profile_api import _client as _profile_api

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
) -> dict:
    """Run a command, return its document after proving it against its schema."""

    status = cli.main(("--json", "--no-input", *argv), control_client=client)
    printed = capsys.readouterr().out
    assert status == 0, printed
    document = json.loads(printed)
    kind = _kind_of(argv, document)
    kinds_seen.add(kind)
    assert document["schema_version"] == DOCUMENT_VERSIONS[kind]
    schema = json.loads((SCHEMAS / schema_filename(kind)).read_text())
    jsonschema.Draft202012Validator(schema).validate(document)
    DOCUMENTS[kind].validate_json(printed)
    return document


def _kind_of(argv: tuple[str, ...], document: dict) -> DocumentKind:
    args = cli._parser().parse_args(("--json", "--no-input", *argv))
    kind = document_kind(args, document, error=False)
    assert kind is not None, argv
    return kind


def test_every_fleet_and_profile_document_matches_its_published_schema(
    tmp_path, capsys
) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from vonk_control.models import Base

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    # Seeds one idle Spark into the sessions; the app it returns is not used.
    _profile_api(sessions, with_idle_spark=True)
    clock = lambda: datetime(2026, 9, 10, tzinfo=UTC)
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
    client = _control_client(api, tmp_path, token)
    seen: set[DocumentKind] = set()

    _published(client, capsys, seen, "fleet")
    listing = _published(client, capsys, seen, "profile", "list")
    assert listing["profiles"]
    _published(client, capsys, seen, "--profile", "1", "profile")
    review = _published(
        client, capsys, seen, "--profile", "1", "profile", "load", "--review"
    )
    assert review["schema_version"] == 2
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


def test_published_schema_files_are_the_models_current_export() -> None:
    assert {path.name for path in SCHEMAS.glob("*.schema.json")} == {
        schema_filename(kind) for kind in DOCUMENTS
    }
    for kind in DOCUMENTS:
        assert (SCHEMAS / schema_filename(kind)).read_text() == schema_text(kind), (
            f"{kind}: run python -m vonk_control.vonkctl_json_contract schemas/vonkctl"
        )
        jsonschema.Draft202012Validator.check_schema(json.loads(schema_text(kind)))


def test_every_published_kind_names_its_version_in_schema_and_file() -> None:
    for kind, version in DOCUMENT_VERSIONS.items():
        schema = json.loads(schema_text(kind))
        assert schema_filename(kind).endswith(f"-v{version}.schema.json")
        assert "schema_version" in json.dumps(schema)
