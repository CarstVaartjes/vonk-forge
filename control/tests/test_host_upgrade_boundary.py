from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from vonk_control.host_backup import BackupError
from vonk_control.host_commands import CommandResult
from vonk_control.host_state import (
    GenerationReceipt,
    HostStateConflict,
    SelectedGeneration,
)
from vonk_control.offline import HostUpgradeBoundary
from vonk_control.upgrade import (
    ControlGenerationPlan,
    ProbeDisposition,
    UpgradePhase,
)

from cluster_profiles.platform_release import OciDeploymentBundle

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _selected(tmp_path: Path) -> SelectedGeneration:
    return SelectedGeneration(
        projection_kind="active",
        operation_id="previous-operation",
        plan_digest=f"sha256:{SHA_E}",
        generation_id="gen-" + SHA_A[:24],
        platform_target_name=f"platform/releases/1.1.0/{SHA_A}.json",
        platform_target_sha256=SHA_A,
        tuf_targets_version=6,
        release_digest=f"sha256:{SHA_A}",
        build_digest=f"sha256:{SHA_B}",
        platform_version="1.1.0",
        deployment_bundle_digest=f"sha256:{SHA_C}",
        api_image=f"registry.example/control-api@sha256:{SHA_A}",
        worker_image=f"registry.example/control-worker@sha256:{SHA_B}",
        database_revision="0001_fleet_library_baseline",
        previous_generation=None,
        generation_receipt_sha256=SHA_D,
        selection_receipt_sha256=SHA_E,
        projection_sequence=3,
    )


def _plan() -> ControlGenerationPlan:
    descriptor = OciDeploymentBundle(
        reference=f"registry.example/control-bundle@sha256:{SHA_C}",
        manifest_digest=f"sha256:{SHA_C}",
        manifest_size=100,
        manifest_media_type="application/vnd.oci.image.manifest.v1+json",
        layer_digest=f"sha256:{SHA_D}",
        layer_size=200,
        layer_media_type="application/vnd.vonk-forge.control-deployment.v1.tar",
    )
    values = {
        "schema_version": 1,
        "operation_id": "operation-1",
        "operation_kind": "apply",
        "start_nonce": SHA_E,
        "generation_id": "gen-" + SHA_B[:24],
        "platform_target_name": f"platform/releases/1.2.0/{SHA_B}.json",
        "platform_target_sha256": SHA_B,
        "tuf_targets_version": 7,
        "release_digest": f"sha256:{SHA_B}",
        "build_digest": f"sha256:{SHA_C}",
        "platform_version": "1.2.0",
        "deployment_bundle": descriptor,
        "deployment_bundle_digest": f"sha256:{SHA_D}",
        "api_image": f"registry.example/control-api@sha256:{SHA_B}",
        "worker_image": f"registry.example/control-worker@sha256:{SHA_C}",
        "database_revision": "0012_control_process_heartbeats",
        "current_database_revision": "0001_fleet_library_baseline",
        "current_generation_receipt_sha256": SHA_D,
        "current_selection_receipt_sha256": SHA_E,
        "current_projection_sequence": 3,
        "site_configuration_digest": f"sha256:{SHA_A}",
        "running_identity_digest": f"sha256:{SHA_C}",
        "previous_generation": "gen-" + SHA_A[:24],
        "host_updater_abi": 2,
        "required_bytes": 4096,
    }
    provisional = ControlGenerationPlan(
        **values,
        plan_digest=f"sha256:{'0' * 64}",
    )
    return ControlGenerationPlan(
        **values,
        plan_digest="sha256:"
        + hashlib.sha256(provisional.canonical_payload()).hexdigest(),
    )


class Store:
    def __init__(self, selected: SelectedGeneration) -> None:
        self.selected = selected
        self.identity_root = Path("/control-identity")

    def load_active(self) -> SelectedGeneration:
        return self.selected

    def load_generation(self, generation_id: str) -> GenerationReceipt:
        if generation_id == self.selected.generation_id:
            return GenerationReceipt(
                generation_id=self.selected.generation_id,
                platform_target_name=self.selected.platform_target_name,
                platform_target_sha256=self.selected.platform_target_sha256,
                tuf_targets_version=self.selected.tuf_targets_version,
                release_digest=self.selected.release_digest,
                build_digest=self.selected.build_digest,
                platform_version=self.selected.platform_version,
                deployment_bundle_digest=self.selected.deployment_bundle_digest,
                api_image=self.selected.api_image,
                worker_image=self.selected.worker_image,
                database_revision=self.selected.database_revision,
            )
        return GenerationReceipt(
            generation_id="gen-" + SHA_B[:24],
            platform_target_name=f"platform/releases/1.2.0/{SHA_B}.json",
            platform_target_sha256=SHA_B,
            tuf_targets_version=7,
            release_digest=f"sha256:{SHA_B}",
            build_digest=f"sha256:{SHA_C}",
            platform_version="1.2.0",
            deployment_bundle_digest=f"sha256:{SHA_D}",
            api_image=f"registry.example/control-api@sha256:{SHA_B}",
            worker_image=f"registry.example/control-worker@sha256:{SHA_C}",
            database_revision="0012_control_process_heartbeats",
        )

    def load_candidate(self, _operation_id: str) -> object:
        raise HostStateConflict("candidate is absent")

    def remove_candidate(self, _operation_id: str) -> None:
        return None

    def project_candidate(self, plan) -> object:
        return SimpleNamespace(projection_kind="candidate", **plan.__dict__)


class Runner:
    def __init__(self, selected: SelectedGeneration) -> None:
        self.calls: list[dict[str, object]] = []
        self.selected = selected

    def run(self, argv, *, cwd, env, policy):
        command = tuple(argv)
        self.calls.append(
            {"argv": command, "cwd": Path(cwd), "env": dict(env), "policy": policy}
        )
        if "SELECT version_num FROM alembic_version" in command:
            output = (self.selected.database_revision + "\n").encode()
        elif command[-4:] == (
            "ps",
            "--format",
            "json",
            "control-api",
        ):
            output = b"[]\n"
        elif "ps" in command and "--format" in command:
            output = json.dumps(
                [
                    {
                        "ID": "1" * 64,
                        "Image": self.selected.api_image,
                        "Service": "control-api",
                        "State": "running",
                    },
                    {
                        "ID": "2" * 64,
                        "Image": self.selected.worker_image,
                        "Service": "control-worker",
                        "State": "running",
                    },
                ],
                separators=(",", ":"),
            ).encode()
        elif command[:4] == (
            "/usr/bin/docker",
            "inspect",
            "--type",
            "container",
        ):
            service = "control-api" if command[-1] == "1" * 64 else "control-worker"
            image = (
                self.selected.api_image
                if service == "control-api"
                else self.selected.worker_image
            )
            output = json.dumps(
                [
                    f"VONK_CONTROL_GENERATION_ID={self.selected.generation_id}",
                    f"VONK_CONTROL_PROCESS_IMAGE={image}",
                    f"VONK_PLATFORM_BUILD_DIGEST={self.selected.build_digest}",
                    f"VONK_PLATFORM_RELEASE_DIGEST={self.selected.release_digest}",
                    f"VONK_PLATFORM_VERSION={self.selected.platform_version}",
                    f"VONK_DATABASE_REVISION={self.selected.database_revision}",
                    f"VONK_CONTROL_START_NONCE={SHA_E}",
                ],
                separators=(",", ":"),
            ).encode()
        else:
            output = b""
        return CommandResult(0, output, b"", 0.01)


def _boundary(tmp_path: Path) -> tuple[HostUpgradeBoundary, Runner, Path]:
    state = tmp_path / "control-host"
    generation = state / "generations" / ("gen-" + SHA_A[:24])
    generation.mkdir(parents=True, mode=0o700)
    compose = generation / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    compose.chmod(0o644)
    site = tmp_path / "control.env"
    site.write_text("COMPOSE_PROJECT_NAME=forge-test\n", encoding="utf-8")
    site.chmod(0o600)
    recipients = tmp_path / "recipients"
    identity = tmp_path / "identity"
    recipients.write_text("age1test\n", encoding="utf-8")
    identity.write_text("AGE-SECRET-KEY-test\n", encoding="utf-8")
    recipients.chmod(0o400)
    identity.chmod(0o400)
    selected = _selected(tmp_path)
    runner = Runner(selected)
    boundary = HostUpgradeBoundary(
        state_root=state,
        compose_file=compose,
        recipients_file=recipients,
        identity_file=identity,
        site_environment_file=site,
        health_url="https://127.0.0.1/internal/v1/generation/readiness",
        runner=runner,  # type: ignore[arg-type]
        generation_store=Store(selected),  # type: ignore[arg-type]
    )
    return boundary, runner, site


def test_snapshot_probes_use_only_active_generation_compose_and_redacted_site_digest(
    tmp_path: Path,
) -> None:
    boundary, runner, site = _boundary(tmp_path)

    assert boundary.database_revision() == "0001_fleet_library_baseline"
    assert boundary.site_configuration_digest() == (
        "sha256:" + hashlib.sha256(site.read_bytes()).hexdigest()
    )
    identities = boundary.running_control_identities()

    assert set(identities) == {"control-api", "control-worker"}
    assert identities["control-api"]["generation_id"] == "gen-" + SHA_A[:24]
    assert all(call["cwd"] == runner.calls[0]["cwd"] for call in runner.calls)
    assert all(
        call["argv"][:4] != ("/bin/sh", "-c", "sh", "-c") for call in runner.calls
    )
    assert all(call["policy"].stdout_limit <= 64 * 1024 for call in runner.calls)


def test_snapshot_rejects_mutable_or_linked_site_environment(tmp_path: Path) -> None:
    boundary, _runner, site = _boundary(tmp_path)
    site.chmod(0o666)
    with pytest.raises(Exception, match="site configuration"):
        boundary.site_configuration_digest()

    site.chmod(0o600)
    linked = site.with_suffix(".linked")
    os.link(site, linked)
    with pytest.raises(Exception, match="site configuration"):
        boundary.site_configuration_digest()


def test_maintenance_diagnostics_use_only_the_selected_generation_and_fixed_argv(
    tmp_path: Path,
) -> None:
    boundary, runner, _site = _boundary(tmp_path)

    result = boundary.maintenance("tailscale-serve-status")

    call = runner.calls[-1]
    generation = tmp_path / "control-host" / "generations" / ("gen-" + SHA_A[:24])
    assert call["cwd"] == generation
    assert call["argv"][-8:] == (
        "exec",
        "-T",
        "tailscale-gateway",
        "tailscale",
        "--socket=/var/run/tailscale/tailscaled.sock",
        "serve",
        "status",
        "--json",
    )
    assert result["action"] == "tailscale-serve-status"
    assert result["generation_id"] == "gen-" + SHA_A[:24]
    assert "/bin/sh" not in call["argv"]


def test_maintenance_logs_have_a_fixed_service_allowlist_and_bounded_since(
    tmp_path: Path,
) -> None:
    boundary, runner, _site = _boundary(tmp_path)

    boundary.maintenance("logs", service="step-ca", since_minutes=30)

    assert runner.calls[-1]["argv"][-4:] == (
        "logs",
        "--no-color",
        "--since=30m",
        "step-ca",
    )
    with pytest.raises(Exception, match="maintenance service"):
        boundary.maintenance("logs", service="../../host", since_minutes=30)
    with pytest.raises(Exception, match="maintenance time range"):
        boundary.maintenance("logs", service="step-ca", since_minutes=0)
    with pytest.raises(TypeError):
        boundary.maintenance("status", apply=True)


def test_predecessor_verified_is_an_exact_generation_receipt_probe(
    tmp_path: Path,
) -> None:
    boundary, _runner, _site = _boundary(tmp_path)
    rollback = replace(
        _plan(),
        operation_kind="rollback",
        generation_id="gen-" + SHA_A[:24],
        platform_target_name=f"platform/releases/1.1.0/{SHA_A}.json",
        platform_target_sha256=SHA_A,
        release_digest=f"sha256:{SHA_A}",
        build_digest=f"sha256:{SHA_B}",
        platform_version="1.1.0",
        deployment_bundle_digest=f"sha256:{SHA_C}",
        api_image=f"registry.example/control-api@sha256:{SHA_A}",
        worker_image=f"registry.example/control-worker@sha256:{SHA_B}",
        database_revision="0001_fleet_library_baseline",
    )

    observation = boundary.probe_phase(UpgradePhase.PREDECESSOR_VERIFIED, rollback)

    assert observation.disposition is ProbeDisposition.EXACT
    assert observation.evidence == {
        "generation_id": rollback.generation_id,
        "generation_receipt_sha256": SHA_D,
    }


def test_every_upgrade_phase_has_a_fixed_probe_and_performer(tmp_path: Path) -> None:
    boundary, _runner, _site = _boundary(tmp_path)
    plan = _plan()
    boundary._generation_store.selected = boundary._selected_from_plan(plan)

    for phase in UpgradePhase:
        if phase is UpgradePhase.AUTHORIZED:
            continue
        observation = boundary.probe_phase(phase, plan)
        assert isinstance(observation.disposition, ProbeDisposition), phase
        assert isinstance(dict(observation.evidence), dict), phase
        assert callable(boundary.perform_phase), phase


def test_selected_service_start_uses_generation_directory_and_persists_fresh_nonce(
    tmp_path: Path,
) -> None:
    boundary, runner, _site = _boundary(tmp_path)
    target = tmp_path / "control-host" / "generations" / ("gen-" + SHA_B[:24])
    target.mkdir(mode=0o700)
    (target / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

    boundary.perform_phase(UpgradePhase.SERVICES_STARTED, _plan())

    call = runner.calls[-1]
    assert call["cwd"] == target
    assert str(target / "compose.yaml") in call["argv"]
    assert call["argv"][-3:] == ("up", "-d", "--remove-orphans")
    nonce = call["env"]["VONK_CONTROL_START_NONCE"]
    assert nonce != SHA_E
    assert len(nonce) == 64
    runtime = (
        tmp_path
        / "control-host"
        / "runtime-launches"
        / "operation-1"
        / ("gen-" + SHA_B[:24] + ".json")
    )
    assert json.loads(runtime.read_text(encoding="ascii"))["start_nonce"] == nonce
    assert not (
        tmp_path
        / "control-host"
        / "operations"
        / "operation-1"
        / "selected-runtime.json"
    ).exists()


def test_selected_service_start_applies_the_canonical_generation(
    tmp_path: Path,
) -> None:
    boundary, runner, site = _boundary(tmp_path)
    site.write_text("COMPOSE_PROJECT_NAME=forge-test\n", encoding="utf-8")
    plan = _plan()
    target = tmp_path / "control-host" / "generations" / plan.generation_id
    target.mkdir(mode=0o700)
    (target / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

    boundary.perform_phase(UpgradePhase.SERVICES_STARTED, plan)

    call = runner.calls[-1]
    assert call["argv"] == (
        "/usr/bin/docker",
        "compose",
        "--file",
        str(target / "compose.yaml"),
        "up",
        "-d",
        "--remove-orphans",
    )


def test_site_configuration_digest_binds_referenced_secret_contents(
    tmp_path: Path,
) -> None:
    boundary, _runner, site = _boundary(tmp_path)
    secret = tmp_path / "database-url"
    secret.write_text("postgresql://first\n", encoding="utf-8")
    secret.chmod(0o600)
    site.write_text(
        f"COMPOSE_PROJECT_NAME=forge-test\nDATABASE_URL_FILE={secret}\n",
        encoding="utf-8",
    )

    first = boundary.site_configuration_digest()
    secret.write_text("postgresql://second\n", encoding="utf-8")
    secret.chmod(0o400)

    assert boundary.site_configuration_digest() != first


def test_first_install_boundary_does_not_require_repository_or_active_compose(
    tmp_path: Path,
) -> None:
    state = tmp_path / "control-host"
    state.mkdir(mode=0o700)
    site = tmp_path / "site.env"
    site.write_text("COMPOSE_PROJECT_NAME=forge-test\n", encoding="utf-8")
    recipients = tmp_path / "recipients"
    recipients.write_text("age1test\n", encoding="utf-8")
    recipients.chmod(0o400)
    store = Store(_selected(tmp_path))
    store.load_active = lambda: None  # type: ignore[method-assign]

    boundary = HostUpgradeBoundary(
        state_root=state,
        compose_file=state / "bootstrap-unavailable/compose.yaml",
        recipients_file=recipients,
        site_environment_file=site,
        health_url="https://127.0.0.1/internal/v1/generation/readiness",
        runner=Runner(_selected(tmp_path)),  # type: ignore[arg-type]
        generation_store=store,  # type: ignore[arg-type]
    )

    assert boundary.database_revision() == "uninitialized"
    assert boundary.running_control_identities() == {
        "control-api": {"status": "absent"},
        "control-worker": {"status": "absent"},
    }


def test_target_and_compensation_predecessor_get_distinct_durable_nonces(
    tmp_path: Path,
) -> None:
    boundary, _runner, _site = _boundary(tmp_path)
    plan = _plan()

    target = boundary._selected_runtime_nonce(plan.operation_id, plan.generation_id)
    predecessor = boundary._selected_runtime_nonce(
        plan.operation_id, plan.previous_generation
    )

    assert target != predecessor
    receipts = sorted(
        (tmp_path / "control-host" / "runtime-launches" / plan.operation_id).glob(
            "*.json"
        )
    )
    assert [item.stem for item in receipts] == sorted(
        [plan.generation_id, plan.previous_generation]
    )


def test_safe_absence_is_repeatable_not_a_phase_conflict(tmp_path: Path) -> None:
    boundary, _runner, _site = _boundary(tmp_path)
    plan = _plan()
    staging = (
        tmp_path / "control-host" / "generations" / f".{plan.generation_id}.staging"
    )
    staging.mkdir(mode=0o700)
    rendered = b"services: {}\n"
    (staging / "compose.rendered.yaml").write_bytes(rendered)
    (staging / "staging.json").write_bytes(
        (
            json.dumps(
                {
                    "bundle_digest": plan.deployment_bundle_digest,
                    "generation_id": plan.generation_id,
                    "plan_digest": plan.plan_digest,
                    "rendered_compose_sha256": hashlib.sha256(rendered).hexdigest(),
                    "schema_version": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    )
    (staging / "compose.rendered.yaml").chmod(0o400)
    (staging / "staging.json").chmod(0o400)

    assert (
        boundary.probe_phase(UpgradePhase.BUNDLE_IMAGES_ACQUIRED, plan).disposition
        is ProbeDisposition.ABSENT
    )
    assert (
        boundary.probe_phase(UpgradePhase.GENERATION_COMMITTED, plan).disposition
        is ProbeDisposition.ABSENT
    )


def test_worker_ready_requires_generation_bound_readiness_not_only_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, _runner, _site = _boundary(tmp_path)
    plan = _plan()
    boundary._generation_store.selected = boundary._selected_from_plan(plan)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        boundary, "_selected_api_container", lambda generation: "1" * 64
    )

    def readiness(_container, generation, **kwargs):
        calls.append((generation.generation_id, kwargs["start_nonce"]))
        return {"status": "ready", "generation_id": generation.generation_id}

    monkeypatch.setattr(boundary, "_readiness_probe", readiness)
    nonce = boundary._selected_runtime_nonce(plan.operation_id, plan.generation_id)

    observation = boundary.probe_phase(UpgradePhase.WORKER_READY, plan)

    assert observation.disposition is ProbeDisposition.EXACT
    assert calls == [(plan.generation_id, nonce)]


def test_worker_not_ready_is_repeatable_instead_of_an_identity_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, _runner, _site = _boundary(tmp_path)
    plan = _plan()
    boundary._generation_store.selected = boundary._selected_from_plan(plan)
    boundary._selected_runtime_nonce(plan.operation_id, plan.generation_id)
    monkeypatch.setattr(
        boundary, "_selected_api_container", lambda _generation: "1" * 64
    )
    monkeypatch.setattr(
        boundary,
        "_readiness_probe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BackupError("not ready")),
    )

    observation = boundary.probe_phase(UpgradePhase.WORKER_READY, plan)

    assert observation.disposition is ProbeDisposition.PARTIAL


def test_candidate_not_ready_is_repeatable_after_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, _runner, _site = _boundary(tmp_path)
    plan = _plan()
    host_plan = boundary._host_plan(plan)
    monkeypatch.setattr(
        boundary._generation_store,
        "load_candidate",
        lambda _operation_id: SimpleNamespace(
            projection_kind="candidate", **host_plan.__dict__
        ),
    )
    monkeypatch.setattr(
        boundary,
        "_readiness_probe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BackupError("not ready")),
    )

    observation = boundary.probe_phase(UpgradePhase.CANDIDATE_READY, plan)

    assert observation.disposition is ProbeDisposition.PARTIAL


def test_migration_and_candidate_start_use_only_uncommitted_staging_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, runner, _site = _boundary(tmp_path)
    plan = _plan()
    final = tmp_path / "control-host" / "generations" / plan.generation_id
    staging = final.with_name("." + final.name + ".staging")
    staging.mkdir(mode=0o700)
    (staging / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    assert not final.exists()

    boundary.perform_phase(
        UpgradePhase.SERVICES_STOPPED_DATABASE_MIGRATED,
        plan,
    )
    monkeypatch.setattr(boundary, "_wait_for_readiness", lambda *_args, **_kwargs: {})
    boundary.perform_phase(UpgradePhase.CANDIDATE_READY, plan)

    target_commands = [call for call in runner.calls if call["cwd"] == staging]
    assert any("alembic" in call["argv"] for call in target_commands)
    assert any(
        "VONK_CONTROL_STARTUP_MODE=preselection" in call["argv"]
        for call in target_commands
    )
    assert all(call["cwd"] != final for call in runner.calls)


def test_first_install_skips_backup_and_initializes_staged_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, runner, _site = _boundary(tmp_path)
    plan = replace(
        _plan(),
        previous_generation=None,
        current_generation_receipt_sha256=None,
        current_selection_receipt_sha256=None,
        current_projection_sequence=None,
    )
    staging = (
        tmp_path
        / "control-host"
        / "generations"
        / ("." + plan.generation_id + ".staging")
    )
    staging.mkdir(mode=0o700)
    (staging / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    boundary._generation_store.load_active = lambda: None  # type: ignore[method-assign]

    boundary.perform_phase(UpgradePhase.BACKUP_COMPLETED, plan)
    assert runner.calls == []

    boundary.perform_phase(
        UpgradePhase.SERVICES_STOPPED_DATABASE_MIGRATED,
        plan,
    )

    assert [call["argv"] for call in runner.calls] == [
        (
            "/usr/bin/docker",
            "compose",
            "--file",
            str(staging / "compose.yaml"),
            "up",
            "-d",
            "postgres",
        ),
        (
            "/usr/bin/docker",
            "compose",
            "--file",
            str(staging / "compose.yaml"),
            "run",
            "--rm",
            "--no-deps",
            "control-api",
            "python",
            "-m",
            "alembic",
            "upgrade",
            plan.database_revision,
        ),
    ]

    monkeypatch.setattr(boundary, "_wait_for_readiness", lambda *_args, **_kwargs: {})
    boundary.perform_phase(UpgradePhase.CANDIDATE_READY, plan)
    assert any(
        call["cwd"] == staging
        and "--name" in call["argv"]
        and "VONK_CONTROL_STARTUP_MODE=preselection" in call["argv"]
        for call in runner.calls
    )


def test_selected_start_never_falls_back_to_leftover_staging(tmp_path: Path) -> None:
    boundary, _runner, _site = _boundary(tmp_path)
    plan = _plan()
    final = tmp_path / "control-host" / "generations" / plan.generation_id
    staging = final.with_name("." + final.name + ".staging")
    staging.mkdir(mode=0o700)
    (staging / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

    with pytest.raises(Exception, match="generation Compose file"):
        boundary.perform_phase(UpgradePhase.SERVICES_STARTED, plan)
