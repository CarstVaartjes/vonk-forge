"""The installed CLI presents the Controller's whole-fleet review before consent."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import build_production_fleet_profile_service
from vonk_control.models import (
    AgentNode,
    AgentNodeProfile,
    CatalogDocumentRevision,
    RecipeRun,
)
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_fleet_profile_api import _client, _headers
from .test_profile_load_installed_cli import (
    _https_api_peer,
    _process_environment,
    _run_pty,
)
from .test_recipe_operations import installed_recipe, setup_services, started_recipe
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)

pytest_plugins = ("tests.test_profile_load_installed_cli",)


@pytest.mark.lane
def test_installed_cli_reviews_real_whole_fleet_effects_before_prompt(
    installed_vonkctl: Path, postgres_engine, tmp_path: Path
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installed = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4())
    )
    previous = started_recipe(
        sessions,
        lifecycle,
        installed.owner_id,
        nodes,
        request_id=str(uuid4()),
        alias="studio-chat",
    )
    idle_node = "spk_" + "3" * 32
    with sessions.begin() as session:
        run = session.get(RecipeRun, previous.owner_id)
        assert run is not None and run.state == "running"
        # A withdrawn route is not a healthy desired assignment. The Controller
        # must show the old run being stopped before its replacement is started.
        run.route_state = "withdrawn"
        session.add(
            AgentNode(
                node_id=idle_node,
                state="active",
                protocol_version=2,
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=lifecycle._clock(),
            )
        )
        session.add(
            AgentNodeProfile(
                node_id=idle_node,
                display_name="Idle Spark",
                hostname="idle-spark",
            )
        )

    planner = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    profiles = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=planner
    )
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "Studio replacement",
                "assignments": [
                    {
                        "recipe_selector": f"{revision.publisher}/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "studio-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    api, tokens = _client(sessions, profiles=profiles)
    headers = _headers(tokens, "administrator")
    current = api.post(f"/api/profile/{profile.number}/preview", headers=headers)
    assert current.status_code == 200, current.text
    review = current.json()
    assert review["allowed"] is True
    assert review["summary"]["starts"] == review["summary"]["stops"] == 1
    assert review["scope"]["idle_node_ids"] == [idle_node]
    assert review["effects"]["runs"][0]["action"] == "stop"
    assert review["effects"]["runs"][0]["run_id"] == previous.owner_id
    decision = review["preparation_decisions"][0]
    assert set(decision["model_reuse_node_ids"]) == set(nodes)
    assert set(decision["image_reuse_node_ids"]) == set(nodes)
    fit = review["assessments"][0]["assessment"]["fit_current"]
    assert fit["allowed"] is False
    assert any(
        "Port 8000 is already reserved" in reason["detail"]
        for node in fit["nodes"]
        for reason in node["blockers"]
    )
    fit_after_stop = review["assessments"][0]["assessment"]["fit_after_stop"]
    assert fit_after_stop["allowed"] is True, fit_after_stop
    assert any(
        reason["code"] == "profile.interruption_expected"
        for reason in review["reasons"]
    )

    with _https_api_peer(tmp_path, api, headers) as (url, certificate, peer):
        environment = _process_environment(tmp_path, url, certificate, headers)
        status, stdout, transcript = _run_pty(
            installed_vonkctl,
            ("--profile", str(profile.number), "profile", "load", "--detach"),
            environment,
            tmp_path,
            answer="no",
        )

    visible = stdout + transcript
    assert status == 2
    assert "Ready for review" in visible and "[y/N]" in visible
    assert profile.name in visible
    assert idle_node in visible
    assert previous.owner_id in visible and "Stop endpoint: studio-chat" in visible
    assert f"Starts: {review['summary']['starts']}" in visible
    assert f"Stops: {review['summary']['stops']}" in visible
    assert all(node_id in visible for node_id in nodes)
    assert all(step["label"] in visible for step in review["steps"])
    assert all(
        node_id in visible for step in review["steps"] for node_id in step["node_ids"]
    )
    assert decision["model"]["artifact_set_sha256"] in visible
    assert decision["runtime_image"]["image_digest"] in visible
    assert decision["runtime_image"]["oci_layout_sha256"] in visible
    assert "Reuse model on:" in visible and "Reuse image on:" in visible
    assert "Current capacity: blocked" in visible
    assert "Capacity after stops: fits" in visible
    assert f"{nodes[0]} capacity blocker" in visible
    assert "Port 8000 is already reserved" in visible
    assert "Available in limiting pool:" in visible
    assert "profile.interruption_expected" in visible
    assert "workloads may be unavailable until final starts complete" in visible
    assert [(method, path) for method, path, _ in peer.calls] == [
        ("POST", f"/api/profile/{profile.number}/preview")
    ]
