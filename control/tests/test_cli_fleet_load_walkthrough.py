"""Disposable installed-CLI facilitation for the whole-Fleet load card."""

from __future__ import annotations

import copy
import json
import os
import pty
import select as select_io
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_control.fleet_profile_contract import FleetProfileInput, FleetProfilePreview
from vonk_control.fleet_profiles import build_production_fleet_profile_service
from vonk_control.models import (
    AgentNode,
    AgentNodeProfile,
    AgentOperation,
    CatalogDocumentRevision,
    FleetProfileApplication,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_cli_operator_walkthrough import _run_cli, _session_environment
from .test_fleet_profile_api import _client, _headers
from .test_profile_load_installed_cli import _build_installed_vonkctl, _https_api_peer
from .test_recipe_operations import (
    NOW,
    installed_recipe,
    setup_services,
    started_recipe,
)
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)

pytest_plugins = ("tests.test_profile_load_installed_cli",)

_STALE_REQUEST_KEY = "44444444-4444-4444-8444-444444444444"
_ACCEPTED_REQUEST_KEY = "55555555-5555-4555-8555-555555555555"
_MODE = os.environ.get("VONK_U4_MODE")

pytestmark = [
    pytest.mark.lane,
    pytest.mark.skipif(
        _MODE not in {"smoke", "interactive"},
        reason="set VONK_U4_MODE=smoke or interactive to opt in",
    ),
]


@pytest.fixture
def u4_workspace() -> Iterator[tuple[Path, Path]]:
    with tempfile.TemporaryDirectory(prefix="vonk-u4-walkthrough-") as directory:
        workspace = Path(directory)
        workspace.chmod(0o700)
        executable = _build_installed_vonkctl(workspace / "installed-cli")
        yield workspace, executable
    assert not workspace.exists()


def _side_effect_ids(sessions) -> tuple[tuple[str, ...], ...]:
    owned_tables = (
        FleetProfileApplication,
        AgentOperation,
        Job,
        RecipeInstallation,
        RecipeRun,
        ResourceReservation,
    )
    with sessions() as session:
        return tuple(
            tuple(session.scalars(select(model.id).order_by(model.id)))
            for model in owned_tables
        )


def _run_states(sessions) -> tuple[tuple[str, str, str], ...]:
    with sessions() as session:
        return tuple(
            (row.id, row.state, row.route_state)
            for row in session.scalars(select(RecipeRun).order_by(RecipeRun.id))
        )


def _u4_fixture(postgres_engine, workspace: Path):
    """Use real Recipe and Profile owners with two affected and one idle Spark."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        workspace / "owners", nodes=2, engine=postgres_engine
    )
    installed = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id="66666666-6666-4666-8666-666666666666",
    )
    previous = started_recipe(
        sessions,
        lifecycle,
        installed.owner_id,
        nodes,
        request_id="77777777-7777-4777-8777-777777777777",
        alias="studio-chat",
    )

    idle_node = "spk_" + "3" * 32
    with sessions.begin() as session:
        run = session.get(RecipeRun, previous.owner_id)
        assert run is not None and run.state == "running"
        # The currently withdrawn route still needs a visible stop before its
        # replacement can start; no worker is attached to this fixture.
        run.route_state = "withdrawn"
        session.add(
            AgentNode(
                node_id=idle_node,
                state="active",
                protocol_version=2,
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=NOW,
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
    return (
        sessions,
        lifecycle,
        profiles,
        profile,
        previous,
        nodes,
        idle_node,
        api,
        headers,
    )


def _reviewed_pty(
    executable: Path,
    arguments: tuple[str, ...],
    environment: dict[str, str],
    cwd: Path,
    *,
    reviewed_text: tuple[str, ...],
) -> tuple[int, str, str]:
    """Confirm only after the entire expected review is already on screen."""

    master, slave = pty.openpty()
    process = subprocess.Popen(
        [str(executable), *arguments],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=slave,
        env=environment,
        cwd=cwd,
    )
    os.close(slave)
    transcript = bytearray()
    confirmation_sent = False
    deadline = time.monotonic() + 45
    try:
        while True:
            if time.monotonic() >= deadline:
                process.kill()
                raise TimeoutError("installed CLI did not finish U4 review")
            ready, _, _ = select_io.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if not chunk and process.poll() is not None:
                    break
                transcript.extend(chunk)
                if not confirmation_sent and b"[y/N]" in transcript:
                    visible = transcript.decode(errors="replace")
                    missing = tuple(
                        item for item in reviewed_text if item not in visible
                    )
                    assert not missing, (
                        "consent appeared before the complete Fleet review; "
                        f"missing {missing!r}"
                    )
                    os.write(master, b"yes\n")
                    confirmation_sent = True
            elif process.poll() is not None:
                break
        stdout, _ = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        os.close(master)
    assert confirmation_sent, "the installed CLI never presented its consent prompt"
    returncode = process.returncode
    assert returncode is not None and stdout is not None
    return returncode, stdout.decode(), transcript.decode(errors="replace")


def _review_text(review: FleetProfilePreview) -> tuple[str, ...]:
    texts = [
        "Ready for review",
        f"Profile: {review.profile_name}",
        f"Plan digest: {review.plan_digest}",
        f"All Sparks: {', '.join(review.scope.node_ids)}",
        f"Idle Sparks: {', '.join(review.scope.idle_node_ids)}",
    ]
    for key, value in review.summary.model_dump(mode="json").items():
        texts.append(f"{key.replace('_', ' ').title()}: {value}")
    for assignment in review.assignments:
        texts.extend(
            (
                f"Recipe: {assignment.recipe_title}",
                f"Sparks: {', '.join(assignment.node_ids)}",
                f"Current: {assignment.current_state}",
                f"Desired: {assignment.desired_state}",
            )
        )
    for step in review.steps:
        texts.extend(
            (
                f"{step.index}. {step.label}",
                f"Affected Sparks: {', '.join(step.node_ids)}",
            )
        )
    for run in review.effects.runs:
        texts.extend(
            (
                f"{run.action.title()} endpoint: {run.alias}",
                f"Run: {run.run_id}",
                f"Complete group: {', '.join(run.node_ids)}",
            )
        )
    for installation in review.effects.installations:
        texts.extend(
            (
                (
                    f"{installation.action.title()} installation: "
                    f"{installation.installation_id}"
                ),
                f"Complete group: {', '.join(installation.node_ids)}",
            )
        )
    for item in review.preparation_decisions:
        texts.extend(
            (
                f"Exact model set: {item.model.artifact_set_sha256}",
                f"Exact image: {item.runtime_image.image_digest}",
                f"OCI archive SHA-256: {item.runtime_image.oci_layout_sha256}",
                f"Architecture: {item.runtime_image.architecture}",
            )
        )
    return tuple(texts)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _make_first_load_stale(api, headers, number: int, monkeypatch):
    """Change a real saved assignment immediately before the first load POST."""

    original_request = api.request
    state: dict[str, object] = {"changed": False, "status": None, "body": None}

    def change_before_load(method, path, **kwargs):
        if (
            method == "POST"
            and path == f"/api/profile/{number}/load"
            and state["changed"] is False
        ):
            raw_body = kwargs.get("content")
            assert isinstance(raw_body, bytes)
            submitted = json.loads(raw_body)
            state["body"] = submitted
            current = original_request(
                "GET", f"/api/profile/{number}/definition", headers=headers
            )
            assert current.status_code == 200, current.text
            saved = current.json()
            definition = copy.deepcopy(saved["definition"])
            assignments = definition["assignments"]
            assert len(assignments) == 1
            # The presented plan replaces the withdrawn running endpoint. The
            # owner changes to install-only before admission, so the updated
            # review must stop but no longer start that endpoint.
            assignments[0]["desired_state"] = "installed"
            assignments[0]["assignment_name"] = None
            changed = original_request(
                "PUT",
                f"/api/profile/{number}",
                headers=headers,
                json={**definition, "expected_revision": saved["revision"]},
            )
            state["status"] = changed.status_code
            assert changed.status_code == 200, changed.text
            state["changed"] = True
        return original_request(method, path, **kwargs)

    monkeypatch.setattr(api, "request", change_before_load)
    return state


def _dry_run(
    installed_vonkctl: Path, environment: dict[str, str], cwd: Path, number: int
) -> tuple[FleetProfilePreview, str]:
    result = _run_cli(
        installed_vonkctl,
        (
            "--no-input",
            "--json",
            "--profile",
            str(number),
            "profile",
            "load",
            "--dry-run",
        ),
        environment,
        cwd,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    review = FleetProfilePreview.model_validate_json(result.stdout)
    return review, review.plan_digest


def _exercise_smoke(
    *, installed_vonkctl: Path, postgres_engine, workspace: Path, monkeypatch
) -> None:
    (
        sessions,
        _lifecycle,
        _profiles,
        profile,
        previous,
        nodes,
        idle_node,
        api,
        headers,
    ) = _u4_fixture(postgres_engine, workspace / "scenario")
    before_rows = _side_effect_ids(sessions)
    before_runs = _run_states(sessions)
    with _https_api_peer(workspace, api, headers) as (url, certificate, peer):
        environment = _session_environment(
            installed_vonkctl=installed_vonkctl,
            workspace=workspace,
            url=url,
            certificate=certificate,
            headers=headers,
        )
        first_review, old_digest = _dry_run(
            installed_vonkctl, environment, workspace, profile.number
        )
        assert first_review.allowed is True
        assert first_review.summary.starts == 1
        assert first_review.summary.stops == 1
        assert set(first_review.scope.node_ids) == {*nodes, idle_node}
        assert first_review.scope.idle_node_ids == [idle_node]
        assert first_review.effects.runs[0].action == "stop"
        assert first_review.effects.runs[0].run_id == previous.owner_id

        stale = _make_first_load_stale(api, headers, profile.number, monkeypatch)
        status, stdout, transcript = _reviewed_pty(
            installed_vonkctl,
            (
                "--profile",
                str(profile.number),
                "profile",
                "load",
                "--expected-plan",
                old_digest,
                "--request-key",
                _STALE_REQUEST_KEY,
                "--detach",
            ),
            environment,
            workspace,
            reviewed_text=_review_text(first_review),
        )
        assert stale["changed"] is True and stale["status"] == 200
        submitted = stale["body"]
        assert isinstance(submitted, dict)
        assert submitted["plan_digest"] == old_digest
        assert submitted["request_key"] == _STALE_REQUEST_KEY
        assert status == 2
        assert not stdout
        assert "stale" in transcript.casefold()
        assert "submitted load was refused" in transcript.casefold()
        assert old_digest in transcript
        assert _side_effect_ids(sessions) == before_rows
        assert _run_states(sessions) == before_runs
        missing = api.get(
            f"/api/profile/{profile.number}/requests/{_STALE_REQUEST_KEY}",
            headers=headers,
        )
        assert missing.status_code == 404

        current_review, current_digest = _dry_run(
            installed_vonkctl, environment, workspace, profile.number
        )
        assert current_digest != old_digest
        assert current_review.allowed is True
        assert current_review.summary.starts == 0
        assert current_review.summary.stops == 1
        assert any(
            assignment.desired_state == "installed"
            for assignment in current_review.assignments
        )
        assert current_digest in transcript
        assert "Starts: 0" in transcript and "Stops: 1" in transcript

        accepted = _run_cli(
            installed_vonkctl,
            (
                "--no-input",
                "--json",
                "--profile",
                str(profile.number),
                "profile",
                "load",
                "--expected-plan",
                current_digest,
                "--yes",
                "--request-key",
                _ACCEPTED_REQUEST_KEY,
                "--detach",
            ),
            environment,
            workspace,
        )
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        assert accepted.stdout.count("\n") == 1
        assert not accepted.stderr
        receipt = json.loads(accepted.stdout)
        assert receipt["request_key"] == _ACCEPTED_REQUEST_KEY
        assert receipt["state"] == "queued"
        intended = receipt["progress"]["intended_profile"]
        assert intended["reviewed_plan_digest"] == current_digest
        observed = api.get(
            f"/api/profile/{profile.number}/requests/{_ACCEPTED_REQUEST_KEY}",
            headers=headers,
        )
        assert observed.status_code == 200, observed.text
        assert observed.json() == receipt
        assert _run_states(sessions) == before_runs
        with sessions() as session:
            applications = list(
                session.scalars(
                    select(FleetProfileApplication).order_by(
                        FleetProfileApplication.created_at,
                        FleetProfileApplication.id,
                    )
                )
            )
            assert len(applications) == 1
            assert applications[0].request_key == _ACCEPTED_REQUEST_KEY
            assert applications[0].state == "queued"
        load_bodies = [
            body
            for method, path, body in peer.calls
            if method == "POST" and path == f"/api/profile/{profile.number}/load"
        ]
        assert len(load_bodies) == 2
        assert load_bodies[0] == {
            "plan_digest": old_digest,
            "request_key": _STALE_REQUEST_KEY,
        }
        assert load_bodies[1] == {
            "plan_digest": current_digest,
            "request_key": _ACCEPTED_REQUEST_KEY,
        }


@pytest.mark.skipif(_MODE != "smoke", reason="set VONK_U4_MODE=smoke")
def test_installed_cli_u4_whole_fleet_stale_and_exact_scripted_consent(
    u4_workspace: tuple[Path, Path], postgres_engine, monkeypatch
) -> None:
    tmp_path, installed_vonkctl = u4_workspace
    _exercise_smoke(
        installed_vonkctl=installed_vonkctl,
        postgres_engine=postgres_engine,
        workspace=tmp_path,
        monkeypatch=monkeypatch,
    )
    print(
        "U4 smoke passed: complete affected/idle review, stale refusal with no "
        "application, then exact-digest --yes acceptance. No worker or Spark ran."
    )


@pytest.mark.skipif(_MODE != "interactive", reason="set VONK_U4_MODE=interactive")
def test_installed_cli_u4_disposable_human_session(
    u4_workspace: tuple[Path, Path], postgres_engine, monkeypatch
) -> None:
    tmp_path, installed_vonkctl = u4_workspace
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        pytest.skip("interactive U4 needs a terminal")
    (
        sessions,
        _lifecycle,
        _profiles,
        profile,
        _previous,
        nodes,
        idle_node,
        api,
        headers,
    ) = _u4_fixture(postgres_engine, tmp_path / "scenario")
    stale = _make_first_load_stale(api, headers, profile.number, monkeypatch)
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, _peer):
        environment = _session_environment(
            installed_vonkctl=installed_vonkctl,
            workspace=tmp_path,
            url=url,
            certificate=certificate,
            headers=headers,
        )
        shell = shutil.which("bash") or "/bin/bash"
        print(
            "Disposable U4 session. Use only the task card and shipped runbook; "
            "no worker or Spark target is connected. This is not a network sandbox."
        )
        print("\nU4: Review a whole-Fleet load, identify affected and idle Sparks, and")
        print("explain its effects before accepting. Handle a stale review without")
        print("executing changed effects, then repeat the consent decision")
        print("noninteractively.\n")
        process = subprocess.Popen(
            [shell, "--noprofile", "--norc", "-i"],
            env=environment,
            cwd=tmp_path,
        )
        try:
            process.wait(timeout=900)
        except subprocess.TimeoutExpired:
            _stop_process(process)
            pytest.fail("U4 interactive session reached its 15-minute limit")
        finally:
            _stop_process(process)
        print(f"Local shell exit status: {process.returncode}")
        print(
            f"Stale review was injected: {stale['changed']}; affected Sparks: "
            f"{', '.join(nodes)}; idle Spark: {idle_node}."
        )
        print("Human acceptance outcome must be recorded separately in the scorecard.")
    # The interactive result is evidence to inspect, not an automated participant
    # pass. Only verify that all work stayed inside this disposable SQL owner.
    with sessions() as session:
        rows = list(session.scalars(select(FleetProfileApplication)))
        assert all(row.profile_id == profile.id for row in rows)
