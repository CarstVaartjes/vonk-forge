"""Installed profile editing preserves authoring intent and rejects stale saves."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import DesiredAssignmentState, LifecycleState, RunSwitchCode
from vonk_agent_protocol.reason_codes.profiles import ProfileReasonCode
from vonk_control.fleet_profile_contract import (
    FleetProfileAssignmentInput,
    FleetProfileDefinition,
    FleetProfileInput,
    FleetProfilePreview,
    FleetProfileView,
)
from vonk_control.models import (
    CatalogDocumentRevision,
    ModelCacheSet,
    NodeInventorySnapshot,
)

pytest_plugins = ("tests.test_profile_load_installed_cli",)

from .test_fleet_profiles_canonical import NODE_1
from .test_profile_load_installed_cli import (
    _https_api_peer,
    _process_environment,
)
from .test_profile_load_submission import _profile_api


def test_installed_profile_edit_preserves_definition_and_rejects_concurrent_save(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sessions, api, _codec, headers, _preview = _profile_api(postgres_engine)
    initial = {
        "name": "Installed draft",
        "description": "Keep this description",
        "favorite": True,
        "labels": {"purpose": "evaluation"},
        "installation_policy": "exact",
        "assignments": [
            {
                "recipe_selector": "vonk-forge/synthetic-tiny-build",
                "spark_ids": [NODE_1],
                "assignment_name": "installed-draft",
                "model_variant": "precise-variant",
                "desired_state": "installed",
                "option_choices": {},
            }
        ],
    }
    created = api.put(
        "/api/profile/1",
        headers=headers,
        json={**initial, "expected_revision": 1},
    )
    assert created.status_code == 200, created.text
    assert created.json()["revision"] == 2

    with _https_api_peer(tmp_path, api, headers) as (url, certificate, state):
        environment = _process_environment(tmp_path, url, certificate, headers)
        edited = subprocess.run(
            [
                str(installed_vonkctl),
                "--profile",
                "1",
                "profile",
                "configure",
                "--favorite",
                "false",
                "--json",
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert edited.returncode == 0, edited.stderr
        assert edited.stdout.count("\n") == 1
        assert not edited.stderr
        saved = json.loads(edited.stdout)
        expected = {**initial, "favorite": False}
        assert saved["revision"] == 3
        assert saved["definition"] == expected

        current = api.get("/api/profile/1/definition", headers=headers)
        assert current.status_code == 200
        assert current.json()["definition"] == expected
        assert current.json()["revision"] == 3

        original_request = state.api.request
        changed_once = False
        concurrent_write_status: list[int] = []

        def write_after_definition_read(method: str, path: str, *args, **kwargs):
            nonlocal changed_once
            response = original_request(method, path, *args, **kwargs)
            if (
                method == "GET"
                and path == "/api/profile/1/definition"
                and not changed_once
            ):
                changed_once = True
                concurrent = {
                    **expected,
                    "name": "Concurrent edit",
                    "expected_revision": 3,
                }
                competing_save = original_request(
                    "PUT",
                    "/api/profile/1",
                    headers=headers,
                    json=concurrent,
                )
                concurrent_write_status.append(competing_save.status_code)
            return response

        monkeypatch.setattr(state.api, "request", write_after_definition_read)
        stale = subprocess.run(
            [
                str(installed_vonkctl),
                "--profile",
                "1",
                "profile",
                "configure",
                "--name",
                "Stale CLI edit",
                "--json",
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )

    assert stale.returncode == 2
    assert stale.stdout.count("\n") == 1
    refusal = json.loads(stale.stdout)
    assert refusal["code"] == ProfileReasonCode.REQUEST_INVALID
    assert "profile revision conflict" in refusal["detail"]
    assert concurrent_write_status == [200]
    assert state.calls == [
        ("GET", "/api/profile/1/definition", None),
        ("PUT", "/api/profile/1", expected | {"expected_revision": 2}),
        ("GET", "/api/profile/1/definition", None),
        (
            "PUT",
            "/api/profile/1",
            expected | {"name": "Stale CLI edit", "expected_revision": 3},
        ),
    ]
    final = api.get("/api/profile/1/definition", headers=headers)
    assert final.status_code == 200
    assert final.json()["revision"] == 4
    assert final.json()["definition"] == {
        **expected,
        "name": "Concurrent edit",
    }

    fresh = api.put(
        "/api/profile/1",
        headers=headers,
        json={
            **expected,
            "name": "Fresh edit",
            "expected_revision": final.json()["revision"],
        },
    )
    assert fresh.status_code == 200
    assert fresh.json()["definition"]["name"] == "Fresh edit"


@pytest.mark.parametrize("action", ["remove", "pin"])
@pytest.mark.parametrize("fault", [None, "manifest", "catalog", "exact-model-manifest"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_installed_remove_and_pin_save_ignore_unrelated_cache_evidence(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
    fault: str | None,
    action: str,
) -> None:
    """Catches a global removal-reference scan blocking ordinary profile edits."""
    from .test_fleet_profile_api import _client, _headers
    from .test_profile_installed_execution import _profile_service
    from .test_recipe_operations import setup_services

    sessions, lifecycle, _, _, _, nodes = setup_services(
        tmp_path, engine=postgres_engine, nodes=2, mapping_node_count=1
    )
    profiles, _planner = _profile_service(sessions, lifecycle)
    with sessions.begin() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
        assert revision is not None
        selector = f"{revision.publisher}/{revision.slug}"
        inventory = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == nodes[1]
            )
        )
        assert inventory is not None
        inventory.disk_free_bytes = 0
    definition = FleetProfileDefinition(
        name="Hardware sweep",
        assignments=[
            FleetProfileAssignmentInput(
                recipe_selector=selector,
                spark_ids=[node],
                assignment_name=alias,
                desired_state=DesiredAssignmentState.RUNNING,
            )
            for node, alias in zip(nodes, ("admitted", "disk-blocked"), strict=True)
        ],
    )
    saved = profiles.create(
        FleetProfileInput.model_validate_json(definition.model_dump_json()),
        actor="admin",
        number=10,
    )
    api, codec = _client(sessions, profiles=profiles)
    headers = _headers(codec, "administrator")
    review_response = api.post("/api/profile/10/preview", headers=headers)
    assert review_response.status_code == 200, review_response.text
    review = FleetProfilePreview.model_validate_json(review_response.content)
    decisions = {item.alias: item for item in review.admission_decisions}
    assert decisions["admitted"].allowed
    assert not decisions["disk-blocked"].allowed
    assert any(
        blocker.code == RunSwitchCode.INSUFFICIENT_DISK
        for blocker in decisions["disk-blocked"].blockers
    )
    if fault == "manifest":
        with sessions.begin() as session:
            session.add(
                ModelCacheSet(
                    artifact_set_sha256="e" * 64,
                    model_content_sha256="f" * 64,
                    manifest={},
                    expected_bytes=0,
                    state=LifecycleState.FAILED,
                    created_at=saved.created_at,
                    updated_at=saved.updated_at,
                    last_accessed_at=saved.updated_at,
                )
            )
    elif fault in {"catalog", "exact-model-manifest"}:
        with sessions.begin() as session:
            model = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "model"
                )
            )
            assert model is not None
            # Production retained twenty unreadable historical model rows.
            # Neither those nor a damaged cache row owns this pin's file list.
            for offset in range(1, 21 if fault == "exact-model-manifest" else 2):
                damaged = CatalogDocumentRevision(
                    document_id=model.document_id,
                    kind=model.kind,
                    publisher=model.publisher,
                    slug=model.slug,
                    revision_number=model.revision_number + offset,
                    schema_version=model.schema_version,
                    state=model.state,
                    document={},
                    content_digest=f"{offset:064x}",
                    created_by=model.created_by,
                    created_at=model.created_at,
                )
                session.add(damaged)
                session.flush()
            if fault == "exact-model-manifest":
                session.add(
                    ModelCacheSet(
                        artifact_set_sha256="e" * 64,
                        model_content_sha256=model.content_digest,
                        manifest={},
                        expected_bytes=0,
                        state=LifecycleState.FAILED,
                        created_at=saved.created_at,
                        updated_at=saved.updated_at,
                        last_accessed_at=saved.updated_at,
                    )
                )
    if action == "remove":
        arguments = (
            "remove",
            "admitted",
            "--yes",
            "--profile",
            "10",
            "--timeout-seconds",
            "3",
        )
        expected = definition.model_copy(
            update={"assignments": definition.assignments[1:]}
        )
        number, revision = 10, saved.revision + 1
    else:
        expected = definition.model_copy(
            update={"name": "Sweep pins", "assignments": definition.assignments[:1]}
        )
        pin_path = tmp_path / "pins.json"
        pin_path.write_text(expected.model_dump_json())
        arguments = (
            "import",
            "--file",
            str(pin_path),
            "--expected-revision",
            "0",
            "--profile",
            "13",
        )
        number, revision = 13, 1
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, _peer):
        environment = _process_environment(tmp_path, url, certificate, headers)
        edited = subprocess.run(
            [str(installed_vonkctl), "--json", "--no-input", "profile", *arguments],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert edited.returncode == 0, edited.stdout + edited.stderr
        view = FleetProfileView.model_validate_json(edited.stdout)
        assert view.definition == expected and view.revision == revision
    persisted = profiles.definition_number(number)
    assert persisted.definition == expected and persisted.revision == revision


@pytest.mark.usefixtures("damaged_json_rows")
def test_profile_save_reports_exact_model_damage_and_recovers(
    postgres_engine, tmp_path
):
    """Catches an untyped reference-scan exception escaping the save boundary."""
    from .test_fleet_profile_api import _client, _headers
    from .test_profile_installed_execution import _profile_service
    from .test_recipe_operations import setup_services

    sessions, lifecycle, _, _, _, nodes = setup_services(
        tmp_path, engine=postgres_engine, nodes=2, mapping_node_count=1
    )
    profiles, _planner = _profile_service(sessions, lifecycle)
    with sessions.begin() as session:
        recipe = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
        model = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model"
            )
        )
        assert recipe is not None and model is not None
        selector = f"{recipe.publisher}/{recipe.slug}"
        model_id, original = model.id, model.document
        model.document = {}
    api, codec = _client(sessions, profiles=profiles)
    headers = _headers(codec, "administrator")
    request = FleetProfileInput(
        name="Sweep pins",
        assignments=[
            FleetProfileAssignmentInput(recipe_selector=selector, spark_ids=nodes[:1])
        ],
    )
    refused = api.put(
        "/api/profile/13", headers=headers, json=request.model_dump(mode="json")
    )
    from vonk_agent_protocol.reason_codes.profiles import ProfileReasonCode

    assert refused.status_code == 503, refused.text
    assert (
        refused.headers["x-vonk-error-code"] == ProfileReasonCode.ADMISSION_EFFECT_BUSY
    )
    assert model_id in refused.text
    assert profiles.definition_number(13).revision == 0
    with sessions.begin() as session:
        model = session.get(CatalogDocumentRevision, model_id)
        assert model is not None
        model.document = original
    healed = api.put(
        "/api/profile/13", headers=headers, json=request.model_dump(mode="json")
    )
    assert healed.status_code == 200, healed.text
    assert FleetProfileView.model_validate_json(healed.content).revision == 1
