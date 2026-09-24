"""Installed CLI auth and profile endpoint discovery against the real API."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileAssignment,
    FleetProfileAssignmentInput,
    FleetProfileAssignmentPreview,
    FleetProfileEffects,
    FleetProfileEndpointsView,
    FleetProfileIntendedConfiguration,
    FleetProfileNode,
    FleetProfilePlanSummary,
    FleetProfilePreview,
    FleetProfileScope,
    FleetProfileScopePreview,
)
from vonk_control.fleet_profiles import (
    FleetProfileService,
    _digest,
    _profile_document,
)
from vonk_control.fleet_projection import FleetProjection
from vonk_control.jobs import JobService
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    FleetProfile,
    FleetProfileApplication,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RoutePublication,
    RunNode,
)
from vonk_control.operation_api import durable_operation_services

from .test_fleet_profiles import _installed_pair_plan
from .test_profile_load_installed_cli import (
    _https_api_peer,
    _process_environment,
)
from .test_recipe_routes import NOW as ROUTE_NOW
from .test_recipe_routes import atomic_service
from .test_recipe_routes import setup as setup_routes

pytest_plugins = ("tests.test_profile_load_installed_cli",)

_TOKEN_KEY = b"first-connection-and-profile-endpoints-key"


class _Authority:
    """A stable revision source for the real fleet projection."""

    def head(self) -> str:
        return "a" * 64


def _api(
    sessions: sessionmaker,
    route_root: Path,
    *,
    profiles: FleetProfileService | None = None,
    projection_now: datetime = ROUTE_NOW,
) -> tuple[TestClient, TokenCodec]:
    codec = TokenCodec(_TOKEN_KEY)
    clock = lambda: projection_now
    profile_service = profiles or FleetProfileService(sessions, clock=clock)
    operations = durable_operation_services(
        sessions,
        route_root,
        clock=clock,
        cursors=codec.cursor_codec(),
        profile_endpoint_intent=profile_service.endpoint_intent,
    )
    app = create_app(
        jobs=JobService(sessions, clock=clock),
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet_projection=FleetProjection(_Authority(), sessions, clock=clock),
        operations=operations,
        fleet_profiles=profile_service,
        now=lambda: 100,
    )
    return TestClient(app), codec


def _run_json(
    executable: Path,
    arguments: tuple[str, ...],
    environment: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *arguments],
        env=environment,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


def _credentials_environment(
    tmp_path: Path,
    url: str,
    certificate: Path,
    token: str,
) -> dict[str, str]:
    token_path = tmp_path / (
        f"controller-token-{hashlib.sha256(token.encode()).hexdigest()[:12]}"
    )
    environment = _process_environment(
        tmp_path,
        url,
        certificate,
        {"Authorization": f"Bearer {token}"},
    )
    token_path.write_text(token, encoding="utf-8")
    token_path.chmod(0o600)
    environment["VONK_CONTROL_TOKEN_FILE"] = str(token_path)
    return environment


@pytest.mark.lane
def test_installed_cli_first_connection_requires_a_valid_controller_token(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
) -> None:
    """A missing or rejected credential never becomes an authorized fleet read."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    api, codec = _api(sessions, tmp_path / "unused-routes")
    valid_token = codec.issue(Actor("viewer", "viewer"), ttl_seconds=1_000, now=0)
    expired_token = codec.issue(Actor("viewer", "viewer"), ttl_seconds=1, now=0)
    wrong_issuer_token = TokenCodec(b"x" * 32).issue(
        Actor("viewer", "viewer"), ttl_seconds=1_000, now=0
    )
    with _https_api_peer(tmp_path, api, {"Authorization": f"Bearer {valid_token}"}) as (
        url,
        certificate,
        peer,
    ):
        base_environment = _credentials_environment(
            tmp_path, url, certificate, valid_token
        )

        missing_environment = dict(base_environment)
        missing_environment.pop("VONK_CONTROL_TOKEN_FILE")
        missing = _run_json(
            installed_vonkctl,
            ("--check-connection", "--json"),
            missing_environment,
            tmp_path,
        )
        assert missing.returncode == 2
        missing_document = json.loads(missing.stdout)
        assert "VONK_CONTROL_TOKEN_FILE" in missing_document["error"]
        assert not peer.calls

        for token, label in (
            (expired_token, "expired"),
            (wrong_issuer_token, "wrong issuer"),
        ):
            rejected = _run_json(
                installed_vonkctl,
                ("--check-connection", "--json"),
                _credentials_environment(tmp_path, url, certificate, token),
                tmp_path,
            )
            assert rejected.returncode == 2, label
            assert "401" in json.loads(rejected.stdout)["error"], label
            assert token not in rejected.stdout + rejected.stderr
            assert peer.calls[-1] == ("GET", "/api/fleet", None)

        connected = _run_json(
            installed_vonkctl,
            ("--check-connection", "--json"),
            base_environment,
            tmp_path,
        )

    assert connected.returncode == 0, connected.stdout + connected.stderr
    assert json.loads(connected.stdout)["authorized_read"] == "/api/fleet"
    assert connected.stdout.endswith("\n")
    assert not connected.stderr
    assert peer.calls == [
        ("GET", "/api/fleet", None),
        ("GET", "/api/fleet", None),
        ("GET", "/api/fleet", None),
    ]


def _seed_profile_application_for_run(
    sessions: sessionmaker,
    run_id: str,
    *,
    profile_number: int = 1,
    desired_state: Literal["installed", "running"] = "running",
    alias: str | None = None,
) -> tuple[FleetProfileService, str, str, str]:
    """Persist a reviewed profile owner bound to the exact published run."""

    profile_id = str(uuid4())
    application_id = str(uuid4())
    assignment_id = str(uuid4())
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        installation = session.get(RecipeInstallation, run.installation_id)
        mapping = session.get(ClusterMapping, run.mapping_id)
        assert installation is not None and mapping is not None
        revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        assert revision is not None
        recipe = session.get(CatalogDocument, revision.document_id)
        build = session.get(RecipeBuild, installation.recipe_build_id)
        assert recipe is not None and build is not None
        assignment_alias = None if desired_state == "installed" else alias or run.alias

        # The route fixture is intentionally small. Give its installed group the
        # same complete image identity the production owner validates before it
        # will treat a running assignment as current.
        build.image_digest = "sha256:" + "d" * 64
        build.oci_layout_sha256 = "e" * 64
        build.image_bytes = 1_024
        installation.image_digest = build.image_digest
        installation.plan_digest = "f" * 64
        installation.plan = _installed_pair_plan(
            mapping_id=mapping.id,
            build_id=build.id,
            recipe_revision_id=revision.id,
            recipe_digest=revision.content_digest,
        )

        members = tuple(
            session.scalars(
                select(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == mapping.id)
                .order_by(ClusterMappingNode.rank)
            )
        )
        node_ids = sorted(member.node_id for member in members)
        saved_assignment = FleetProfileAssignmentInput(
            recipe_selector=f"{recipe.publisher}/{recipe.slug}",
            spark_ids=node_ids,
            assignment_name=assignment_alias,
            desired_state=desired_state,
        )
        profile = FleetProfile(
            id=profile_id,
            number=profile_number,
            revision=1,
            name="Default" if profile_number == 1 else f"Profile {profile_number}",
            description="",
            installation_policy="keep-cached",
            assignments=[saved_assignment.model_dump(mode="json", exclude_none=True)],
            labels={},
            favorite=False,
            created_by="admin",
            created_at=ROUTE_NOW,
            updated_at=ROUTE_NOW,
        )
        profile_digest = _digest(_profile_document(profile))
        assignment = FleetProfileAssignment(
            id=assignment_id,
            recipe_revision_id=revision.id,
            topology_name=mapping.topology_name,
            desired_state=desired_state,
            alias=assignment_alias,
            nodes=[
                FleetProfileNode(
                    node_id=member.node_id,
                    rank=member.rank,
                    role=member.role,
                    endpoint_owner=member.endpoint_owner,
                )
                for member in members
            ],
            recipe_id=recipe.id,
            recipe_title=recipe.title,
        )
        plan = FleetProfilePreview(
            profile_id=profile_id,
            profile_name=profile.name,
            profile_digest=profile_digest,
            profile_revision=profile.revision,
            profile_definition=None,
            allowed=True,
            scope=FleetProfileScopePreview(node_ids=node_ids, idle_node_ids=[]),
            summary=FleetProfilePlanSummary(
                already_correct=0,
                placements=0,
                builds=0,
                distributions=0,
                installs=0,
                starts=0,
                stops=0,
                uninstalls=0,
                blockers=0,
            ),
            assignments=[
                FleetProfileAssignmentPreview(
                    assignment_id=assignment_id,
                    recipe_revision_id=revision.id,
                    recipe_title=recipe.title,
                    desired_state=desired_state,
                    current_state=(
                        "installed" if desired_state == "installed" else "running"
                    ),
                    node_ids=node_ids,
                    actions=[],
                    reasons=[],
                )
            ],
            resolved_assignments=[assignment],
            admission_decisions=[],
            preparation_decisions=[],
            effects=FleetProfileEffects(runs=[], installations=[], superseded=[]),
            steps=[],
            reasons=[],
            generated_at=ROUTE_NOW,
            assessments=[],
            plan_digest="a" * 64,
        )
        plan_digest = hashlib.sha256(
            canonical_message(plan.reviewed_decision())
        ).hexdigest()
        plan.plan_digest = plan_digest
        intended = FleetProfileIntendedConfiguration(
            profile_digest=profile_digest,
            reviewed_plan_digest=plan_digest,
            reviewed_application_id=application_id,
            installation_policy="keep-cached",
            scope=FleetProfileScope(node_ids=node_ids),
            assignments=[assignment],
        )
        progress = FleetProfileApplicationProgress(intended_profile=intended)
        session.add(profile)
        session.add(
            FleetProfileApplication(
                id=application_id,
                request_key=str(uuid4()),
                profile_id=profile_id,
                profile_digest=profile_digest,
                plan_digest=plan_digest,
                state="succeeded",
                plan=plan.model_dump(mode="json"),
                current_step=0,
                progress=progress.model_dump(mode="json"),
                actor="admin",
                created_at=ROUTE_NOW,
                updated_at=ROUTE_NOW,
            )
        )
    return (
        FleetProfileService(sessions, clock=lambda: ROUTE_NOW),
        profile_id,
        application_id,
        assignment_id,
    )


@pytest.mark.lane
def test_registered_profile_endpoint_binds_database_owner_alias_and_generation(
    postgres_engine,
    tmp_path: Path,
) -> None:
    """The registered API uses loaded SQL ownership for every profile lookup."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    base_routes, _publisher, _applied, run_id = setup_routes(
        tmp_path / "route-fixture", ranks=2, engine=postgres_engine
    )
    route_root = tmp_path / "routes"
    routes = atomic_service(base_routes, route_root, lambda: ROUTE_NOW)
    first_generation = routes.publish_run(run_id)

    profiles, installed_profile_id, _application_id, installed_assignment_id = (
        _seed_profile_application_for_run(
            sessions, run_id, profile_number=1, desired_state="installed"
        )
    )
    _seed_profile_application_for_run(
        sessions, run_id, profile_number=2, alias="another-profile-model"
    )
    _seed_profile_application_for_run(sessions, run_id, profile_number=3)
    api, codec = _api(sessions, route_root, profiles=profiles)
    token = codec.issue(Actor("viewer", "viewer"), ttl_seconds=1_000, now=0)
    headers = {"Authorization": f"Bearer {token}"}

    installed = api.get("/api/profile/1/endpoints", headers=headers)
    assert installed.status_code == 200, installed.text
    installed_view = FleetProfileEndpointsView.model_validate_json(installed.text)
    assert installed_view.profile_id == installed_profile_id
    assert len(installed_view.assignments) == 1
    installed_assignment = installed_view.assignments[0]
    assert installed_assignment.assignment_id == installed_assignment_id
    assert installed_assignment.desired_state == "installed"
    assert installed_assignment.state == "installed-only"
    assert installed_assignment.alias is None
    assert installed_assignment.endpoint is None

    owned_alias = api.get(
        "/api/profile/2/endpoints",
        params={"alias": "another-profile-model"},
        headers=headers,
    )
    assert owned_alias.status_code == 200, owned_alias.text
    assert owned_alias.json()["assignments"][0]["alias"] == "another-profile-model"

    foreign_alias = api.get(
        "/api/profile/1/endpoints",
        params={"alias": "another-profile-model"},
        headers=headers,
    )
    assert foreign_alias.status_code == 404
    assert "not part of profile 1" in foreign_alias.json()["detail"]

    current = api.get(
        "/api/profile/3/endpoints", params={"alias": "qwen"}, headers=headers
    )
    assert current.status_code == 200, current.text
    current_view = FleetProfileEndpointsView.model_validate_json(current.text)
    current_endpoint = current_view.assignments[0].endpoint
    assert current_endpoint is not None
    assert current_endpoint.generation == first_generation.generation

    with sessions.begin() as session:
        owner = session.scalar(
            select(RunNode).where(RunNode.run_id == run_id, RunNode.rank == 0)
        )
        assert owner is not None
        owner.endpoint = {"url": "http://10.0.0.9:8000"}
        owner.evidence_digest = hashlib.sha256(
            b"replacement route evidence"
        ).hexdigest()

    replacement = routes.publish_run(run_id)
    assert replacement.generation > first_generation.generation
    refreshed = api.get(
        "/api/profile/3/endpoints", params={"alias": "qwen"}, headers=headers
    )
    assert refreshed.status_code == 200, refreshed.text
    refreshed_view = FleetProfileEndpointsView.model_validate_json(refreshed.text)
    refreshed_endpoint = refreshed_view.assignments[0].endpoint
    assert refreshed_endpoint is not None
    assert refreshed_endpoint.generation == replacement.generation
    assert refreshed_endpoint.api_base == "http://10.0.0.9:8000/v1"

    with sessions() as session:
        publication = session.scalar(select(RoutePublication).limit(1))
        assert publication is not None and publication.lease_expires_at is not None
        expires_at = publication.lease_expires_at
    expired_api, expired_codec = _api(
        sessions,
        route_root,
        profiles=profiles,
        projection_now=expires_at + timedelta(microseconds=1),
    )
    expired_token = expired_codec.issue(
        Actor("viewer", "viewer"), ttl_seconds=1_000, now=0
    )
    with expired_api:
        expired = expired_api.get(
            "/api/profile/3/endpoints",
            params={"alias": "qwen"},
            headers={"Authorization": f"Bearer {expired_token}"},
        )
    assert expired.status_code == 200, expired.text
    expired_view = FleetProfileEndpointsView.model_validate_json(expired.text)
    assert expired_view.assignments[0].state == "expired"
    assert expired_view.assignments[0].endpoint is None


@pytest.mark.lane
def test_installed_cli_discovers_only_the_published_profile_endpoint_and_revocation(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
) -> None:
    """The endpoint owner, not a guessed global alias, controls discovery."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    base_routes, _publisher, _applied, run_id = setup_routes(
        tmp_path / "route-fixture", ranks=2, engine=postgres_engine
    )
    route_root = tmp_path / "routes"
    routes = atomic_service(base_routes, route_root, lambda: ROUTE_NOW)
    routes.publish_run(run_id)
    profiles, profile_id, application_id, assignment_id = (
        _seed_profile_application_for_run(sessions, run_id)
    )
    api, codec = _api(sessions, route_root, profiles=profiles)
    token = codec.issue(Actor("viewer", "viewer"), ttl_seconds=1_000, now=0)
    headers = {"Authorization": f"Bearer {token}"}
    owner_response = api.get("/api/profile/1/endpoints", headers=headers)
    assert owner_response.status_code == 200, owner_response.text
    owner_view = FleetProfileEndpointsView.model_validate_json(owner_response.text)
    assert len(owner_view.assignments) == 1
    loaded_assignment = owner_view.assignments[0]
    endpoint = loaded_assignment.endpoint
    assert endpoint is not None and loaded_assignment.alias is not None

    with _https_api_peer(tmp_path, api, headers) as (url, certificate, peer):
        environment = _credentials_environment(tmp_path, url, certificate, token)
        discovered = _run_json(
            installed_vonkctl,
            ("--json", "--profile", "1", "profile", "endpoint"),
            environment,
            tmp_path,
        )
        assert discovered.returncode == 0, discovered.stderr
        endpoint_view = json.loads(discovered.stdout)
        assert endpoint_view["profile_id"] == profile_id
        assert endpoint_view["application_id"] == application_id
        assert [
            (item["assignment_id"], item["alias"], item["state"])
            for item in endpoint_view["assignments"]
        ] == [(assignment_id, "qwen", "published")]
        assert endpoint_view["assignments"][0]["endpoint"]["api_base"] == (
            "http://10.0.0.2:8000/v1"
        )
        assert peer.calls == [("GET", "/api/profile/1/endpoints", None)]

        human = _run_json(
            installed_vonkctl,
            ("--profile", "1", "profile", "endpoint"),
            environment,
            tmp_path,
        )
        assert human.returncode == 0, human.stdout + human.stderr
        expected_expiry = datetime.fromisoformat(endpoint.expires_at).isoformat(sep=" ")
        expected_example = (
            "  API_BASE="
            + shlex.quote(endpoint.api_base)
            + " MODEL="
            + shlex.quote(loaded_assignment.alias)
        )
        assert f"Route expires at: {expected_expiry}" in human.stdout
        assert "Credential-free configuration example:" in human.stdout
        assert expected_example in human.stdout
        assert "Authorization" not in human.stdout
        assert token not in human.stdout + human.stderr

        routes.withdraw_run(run_id)
        withdrawn = _run_json(
            installed_vonkctl,
            ("--json", "--profile", "1", "profile", "endpoint"),
            environment,
            tmp_path,
        )

    assert withdrawn.returncode == 0, withdrawn.stderr
    withdrawn_view = FleetProfileEndpointsView.model_validate_json(withdrawn.stdout)
    assert len(withdrawn_view.assignments) == 1
    assignment = withdrawn_view.assignments[0]
    assert assignment.assignment_id == assignment_id
    assert assignment.state == "withdrawn"
    assert assignment.endpoint is None
    assert peer.calls == [
        ("GET", "/api/profile/1/endpoints", None),
        ("GET", "/api/profile/1/endpoints", None),
        ("GET", "/api/profile/1/endpoints", None),
    ]
