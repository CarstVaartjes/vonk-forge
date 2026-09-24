"""A real signal interrupts the installed observer without cancelling its owner."""

from __future__ import annotations

import json
import signal
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentPresence,
    CatalogDocumentRevision,
    FleetProfileApplication,
    NodeArtifact,
)

from .test_fleet_profile_api import _client, _headers
from .test_fleet_profile_cancel import _application
from .test_profile_load_installed_cli import _https_api_peer, _process_environment
from .test_recipe_operations import NOW

pytest_plugins = ("tests.test_profile_load_installed_cli",)


def _add_disjoint_profile_node(
    sessions: sessionmaker[Session], source_node_id: str, node_id: str
) -> str:
    capabilities = ["runtime.vonk.v1", "recipe.operations.v1"]
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                architecture="linux-arm64",
                capabilities=capabilities,
            )
        )
        session.flush()
        certificate_serial = f"serial-{node_id[-1]}"
        session.add(
            AgentCertificate(
                serial=certificate_serial,
                node_id=node_id,
                fingerprint=f"fingerprint-{node_id[-1]}",
                not_before=NOW,
                not_after=NOW + timedelta(days=365),
            )
        )
        session.flush()
        session.add(
            AgentPresence(
                node_id=node_id,
                certificate_serial=certificate_serial,
                certificate_fingerprint=f"fingerprint-{node_id[-1]}",
                management_address="192.168.1.212",
                observed_at=NOW,
            )
        )
        artifacts = tuple(
            session.scalars(
                select(NodeArtifact).where(NodeArtifact.node_id == source_node_id)
            )
        )
        session.add_all(
            NodeArtifact(
                node_id=node_id,
                kind=artifact.kind,
                digest=artifact.digest,
                source=artifact.source,
                size_bytes=artifact.size_bytes,
                state=artifact.state,
                ref_count=0,
                verified_at=artifact.verified_at,
                updated_at=NOW,
            )
            for artifact in artifacts
        )
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        assert revision is not None
        revision_id = revision.id
        revision_slug = revision.slug
    InventoryRepository(sessions, clock=lambda: NOW).record(
        InventorySnapshotInput(
            node_id,
            NOW,
            10_000,
            8_000,
            10_000,
            8_000,
            10_000,
            8_000,
            1,
            False,
            tuple(capabilities),
            memory_pool="shared",
        )
    )
    mappings = ClusterMappingService(sessions)
    mapping = mappings.preview(revision_id, (node_id,), {}, "admin")
    mappings.materialize(mapping, actor="admin", now=NOW)
    return revision_slug


@pytest.mark.lane
def test_installed_follow_sigint_retains_exact_application_without_remote_mutation(
    installed_vonkctl: Path, postgres_engine, tmp_path: Path
) -> None:
    sessions, _switch, service, _profile, application, _adapter, _nodes = _application(
        tmp_path, engine=postgres_engine
    )
    api, codec = _client(sessions, profiles=service)
    headers = _headers(codec, "administrator")
    request_key = application.request_key
    identity = application.id
    path = f"/api/profile/applications/{identity}"
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, state):
        environment = _process_environment(tmp_path, url, certificate, headers)
        arguments = [
            str(installed_vonkctl),
            "profile",
            "progress",
            "--application",
            identity,
            "--json",
        ]
        process = subprocess.Popen(
            [*arguments, "--follow", "--interval-seconds", "0.1"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd=tmp_path,
            text=True,
        )
        try:
            deadline = time.monotonic() + 20
            # A second request proves the observer consumed the first receipt.
            while (
                sum(
                    method == "GET" and route == path
                    for method, route, _ in state.calls
                )
                < 2
            ):
                assert process.poll() is None, process.communicate(timeout=5)
                assert time.monotonic() < deadline, state.calls
                time.sleep(0.02)
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        assert process.returncode == 130, stdout + stderr
        assert stderr == ""
        assert stdout.count("\n") == 1
        interrupted = json.loads(stdout)
        assert interrupted["observation"]["status"] == "interrupted"
        assert identity in interrupted["observation"]["reconnect_command"]
        assert interrupted["result"]["id"] == identity
        assert interrupted["result"]["request_key"] == request_key

        reconnect = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=environment,
            cwd=tmp_path,
            text=True,
            timeout=20,
            check=False,
        )
        assert reconnect.returncode == 0, reconnect.stdout + reconnect.stderr
        assert json.loads(reconnect.stdout)["id"] == identity
        assert all(
            method == "GET" and route == path for method, route, _ in state.calls
        )

    with sessions() as session:
        owner = session.get(FleetProfileApplication, identity)
        assert owner is not None
        assert owner.state == application.state
        assert owner.request_key == request_key


@pytest.mark.lane
def test_installed_follow_timeout_stays_with_original_application_after_newer_load(
    installed_vonkctl: Path, postgres_engine, monkeypatch, tmp_path: Path
) -> None:
    """A later profile application cannot redirect a running observer."""

    sessions, _switch, service, profile, application, _adapter, _nodes = _application(
        tmp_path, engine=postgres_engine
    )
    api, codec = _client(sessions, profiles=service)
    headers = _headers(codec, "administrator")
    identity = application.id
    first_state = application.state
    latest_path = f"/api/profile/{profile.number}/progress"
    exact_path = f"/api/profile/applications/{identity}"
    original_request = api.request
    newer_application_id: str | None = None

    def publish_newer_after_first_read(method, path, **kwargs):
        nonlocal newer_application_id
        result = original_request(method, path, **kwargs)
        if method == "GET" and path == latest_path and newer_application_id is None:
            # Save a new same-profile intent on another Spark. Its admitted
            # plan is newer, but its execution scope leaves the observed
            # application intact.
            source_node_id = _nodes[0]
            second_node_id = "spk_" + f"{2:032x}"
            recipe_slug = _add_disjoint_profile_node(
                sessions, source_node_id, second_node_id
            )

            def newer_clock():
                return application.created_at + timedelta(seconds=1)

            service._clock = newer_clock
            definition = profile.definition.model_dump(mode="json")
            definition["expected_revision"] = profile.revision
            definition["assignments"] = [
                {
                    "recipe_selector": f"vonk-forge/{recipe_slug}",
                    "spark_ids": [second_node_id],
                    "assignment_name": "later-disjoint",
                    "desired_state": "running",
                }
            ]
            saved = api.put(
                f"/api/profile/{profile.number}",
                json=definition,
                headers=headers,
            )
            assert saved.status_code == 200, saved.text
            reviewed = api.post(
                f"/api/profile/{profile.number}/preview", headers=headers
            )
            assert reviewed.status_code == 200, reviewed.text
            preview = reviewed.json()
            assert preview["allowed"] is True
            loaded = api.post(
                f"/api/profile/{profile.number}/load",
                json={
                    "request_key": str(uuid4()),
                    "plan_digest": preview["plan_digest"],
                },
                headers=headers,
            )
            assert loaded.status_code == 202, loaded.text
            newer = service.application(loaded.json()["id"])
            assert newer.id != identity
            assert newer.created_at > application.created_at
            assert service.progress_number(profile.number).id == newer.id
            assert service.application(identity).state == first_state
            newer_application_id = newer.id
        return result

    monkeypatch.setattr(api, "request", publish_newer_after_first_read)
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, peer):
        environment = _process_environment(tmp_path, url, certificate, headers)
        completed = subprocess.run(
            [
                str(installed_vonkctl),
                "--profile",
                str(profile.number),
                "--json",
                "profile",
                "progress",
                "--follow",
                "--timeout-seconds",
                "2",
                "--interval-seconds",
                "0.02",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=environment,
            cwd=tmp_path,
            text=True,
            timeout=30,
            check=False,
        )

    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert completed.stdout.count("\n") == 1
    assert completed.stderr == ""
    document = json.loads(completed.stdout)
    result = document["result"]
    assert result["id"] == identity
    assert result["state"] == first_state
    observation = document["observation"]
    assert observation["status"] == "timed_out"
    assert identity in observation["reconnect_command"]
    assert "--application" in observation["reconnect_command"]
    assert newer_application_id is not None and newer_application_id != identity
    calls = [(method, path) for method, path, _ in peer.calls]
    assert calls[0] == ("GET", latest_path)
    assert len(calls) > 1
    assert calls[1:] == [("GET", exact_path)] * (len(calls) - 1)
    assert all(method == "GET" for method, _path, _document in peer.calls)
    with sessions() as session:
        original = session.get(FleetProfileApplication, identity)
        newer = session.get(FleetProfileApplication, newer_application_id)
        assert original is not None and original.state == first_state
        assert newer is not None and newer.id != original.id
