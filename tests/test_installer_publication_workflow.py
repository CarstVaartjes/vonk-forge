from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/installer-publication.yml"
SETUPS = ROOT / ".github/workflows/installer-setups.yml"
DEV_IMAGES = ROOT / ".github/workflows/dev-images.yml"
AGENT_RELEASE = ROOT / ".github/workflows/agent-release.yml"


def _workflow(path: Path = WORKFLOW) -> dict[str, object]:
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def _steps(job: dict[str, object]) -> dict[str, dict[str, object]]:
    return {step["name"]: step for step in job["steps"]}


def _run_workflow_shell(
    step: dict[str, object],
    tmp_path: Path,
    *,
    server_version: str,
) -> subprocess.CompletedProcess[str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "docker.log"
    github_environment = tmp_path / "github.env"
    docker = commands / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf \'%s|%s\\n\' "${DOCKER_HOST:-}" "$*" >> "$DOCKER_LOG"\n'
        'case "$1" in\n'
        "  run) printf '%s\\n' daemon-id ;;\n"
        "  version) printf '%s\\n' \"$DOCKER_SERVER_VERSION\" ;;\n"
        "  rm) : ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    environment = os.environ | {
        "DOCKER_LOG": str(log),
        "DOCKER_SERVER_VERSION": server_version,
        "GITHUB_ENV": str(github_environment),
        "GITHUB_WORKSPACE": str(workspace),
        "PATH": f"{commands}:{os.environ['PATH']}",
    }
    environment.update(step.get("env", {}))
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    result.docker_log = log.read_text() if log.exists() else ""  # type: ignore[attr-defined]
    result.github_environment = github_environment  # type: ignore[attr-defined]
    return result


def test_publication_requires_candidate_acceptance_before_promotion() -> None:
    workflow = _workflow()
    triggers = workflow["on"]["workflow_run"]
    assert set(triggers["workflows"]) == {
        "CI",
        "Development images",
        "Installer setup programs",
        "Rust Vonk Forge agent development",
    }
    assert triggers["types"] == ["completed"]
    jobs = workflow["jobs"]
    assert set(jobs["candidate"]["needs"]) == {"authority"}
    assert set(jobs["nas-acceptance"]["needs"]) == {"authority", "candidate"}
    assert set(jobs["spark-acceptance"]["needs"]) == {"authority", "candidate"}
    assert "max-parallel" not in jobs["spark-acceptance"]["strategy"]
    expected_services = {
        "VONK_ACCEPTANCE_TAILSCALE_CONTROL_SERVICE": "svc:vonk-forge-acceptance",
        "VONK_ACCEPTANCE_TAILSCALE_HERMES_API_SERVICE": "svc:hermes-api-acceptance",
        "VONK_ACCEPTANCE_TAILSCALE_HERMES_DASHBOARD_SERVICE": "svc:hermes-dashboard-acceptance",
    }
    nas_environment = _steps(jobs["nas-acceptance"])[
        "Run literal clean NAS and Tailscale configuration acceptance"
    ]["env"]
    spark_environment = _steps(jobs["spark-acceptance"])[
        "Run packaged Spark fresh-install, pairing, job, and renewal acceptance"
    ]["env"]
    for name, service in expected_services.items():
        assert nas_environment[name] == service
        assert spark_environment[name] == service
    assert nas_environment["VONK_ACCEPTANCE_TAILSCALE_GATEWAY_HOSTNAME"] == (
        "vonk-forge-ci-${{ github.run_id }}-${{ github.run_attempt }}-native"
    )
    assert spark_environment["VONK_ACCEPTANCE_TAILSCALE_GATEWAY_HOSTNAME"] == (
        "vonk-spark-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.platform }}"
    )
    assert set(jobs["acceptance"]["needs"]) == {
        "authority",
        "candidate",
        "nas-acceptance",
        "spark-acceptance",
    }
    assert set(jobs["promote"]["needs"]) == {"authority", "candidate", "acceptance"}
    assert "publish" not in jobs


def test_development_source_generation_runs_for_every_main_commit() -> None:
    for path in (DEV_IMAGES, AGENT_RELEASE, SETUPS):
        push = _workflow(path)["on"]["push"]
        assert push["branches"] == ["main"]
        assert "paths" not in push
        assert "paths-ignore" not in push


def test_nas_acceptance_uses_verified_compatibility_fixtures_and_a_gate_report() -> (
    None
):
    jobs = _workflow()["jobs"]
    nas = jobs["nas-acceptance"]
    assert nas["permissions"] == {"contents": "read"}
    assert "services" not in nas
    steps = _steps(nas)
    fixture_step = steps["Download verified Compose parser fixtures"]
    assert fixture_step["shell"] == "bash"
    assert fixture_step["env"] == {
        "COMPOSE_LOWER_SHA256": "eca30ae32dc451f9e6d6c8ddce078a76f23b355c3ca0ab391d58f59e87c0d310",
        "COMPOSE_UGREEN_SHA256": "a0298760c9772d2c06888fc8703a487c94c3c3b0134adeef830742a2fc7647b4",
    }
    compatibility_step = steps["Run Docker 29.4.3 NAS compatibility acceptance"]
    native_step = steps["Run literal clean NAS and Tailscale configuration acceptance"]
    for acceptance_step in (compatibility_step, native_step):
        assert acceptance_step["env"]["VONK_ACCEPTANCE_COMPOSE_LOWER"] == (
            "${{ runner.temp }}/compose-fixtures/docker-compose-v2.24.6"
        )
        assert acceptance_step["env"]["VONK_ACCEPTANCE_COMPOSE_UGREEN"] == (
            "${{ runner.temp }}/compose-fixtures/docker-compose-v5.1.3"
        )
        assert acceptance_step["env"]["VONK_ACCEPTANCE_REFERENCE_COMPOSE"] == (
            "${{ runner.temp }}/compose-fixtures/docker-compose-v5.1.3"
        )
        assert acceptance_step["env"]["VONK_ACCEPTANCE_NAS_BIND_IP"] == "127.0.0.1"
        assert acceptance_step["env"]["VONK_ACCEPTANCE_NAS_IP"] == "127.0.0.1"
        assert acceptance_step["env"]["VONK_ACCEPTANCE_WORKSPACE"] == (
            "${{ github.workspace }}"
        )
    assert compatibility_step["env"]["DOCKER_HOST"] == "tcp://127.0.0.1:2375"
    assert compatibility_step["env"]["VONK_ACCEPTANCE_REQUIRE_TAILNET_CLIENT"] == (
        "false"
    )
    assert compatibility_step["env"]["VONK_ACCEPTANCE_TAILSCALE_GATEWAY_HOSTNAME"] == (
        "vonk-forge-ci-${{ github.run_id }}-${{ github.run_attempt }}-dind"
    )
    assert "DOCKER_HOST" not in native_step["env"]
    assert native_step["env"]["VONK_ACCEPTANCE_REQUIRE_TAILNET_CLIENT"] == "false"
    assert native_step["env"]["VONK_ACCEPTANCE_COMPOSE_LOWER"] == (
        "${{ runner.temp }}/compose-fixtures/docker-compose-v2.24.6"
    )
    assert "nas-acceptance/report.json" not in compatibility_step["run"]
    assert "nas-acceptance/report.json" in native_step["run"]
    assert "for attempt in 1 2; do" not in native_step["run"]
    assert (
        native_step["run"].count(
            "uv run python tests/acceptance/test_fresh_nas_install.py"
        )
        == 1
    )
    report = steps["Upload NAS behavioral gate report"]
    assert report["uses"].startswith("actions/upload-artifact@")
    assert report["with"]["if-no-files-found"] == "error"
    assert report["with"]["path"] == "${{ runner.temp }}/nas-acceptance/report.json"


def test_nas_dind_fixture_starts_a_host_network_daemon_and_fails_wrong_version(
    tmp_path: Path,
) -> None:
    nas = _workflow()["jobs"]["nas-acceptance"]
    steps = _steps(nas)
    start = steps["Start Docker 29.4.3 compatibility daemon"]

    ready = _run_workflow_shell(start, tmp_path / "ready", server_version="29.4.3")
    assert ready.returncode == 0, ready.stderr
    docker_log = ready.docker_log  # type: ignore[attr-defined]
    workspace = tmp_path / "ready/workspace"
    assert f"--network host --volume {workspace}:{workspace}" in docker_log
    assert "--publish" not in docker_log
    assert "--privileged" in docker_log
    assert (
        "docker:29.4.3-dind@sha256:685b91dca8eab7de1dce1c303dbb7a763e4082d6a60db10968adf3295fbd2495"
        in docker_log
    )
    assert "dockerd --host=tcp://127.0.0.1:2375" in docker_log
    assert "tcp://127.0.0.1:2375|version --format {{.Server.Version}}" in docker_log
    assert "|inspect " not in docker_log
    assert not ready.github_environment.exists()  # type: ignore[attr-defined]

    wrong = _run_workflow_shell(start, tmp_path / "wrong", server_version="29.4.2")
    assert wrong.returncode != 0


def test_nas_dind_fixture_is_always_removed_and_candidate_receipt_is_uploaded(
    tmp_path: Path,
) -> None:
    jobs = _workflow()["jobs"]
    nas_steps = jobs["nas-acceptance"]["steps"]
    steps = _steps(jobs["nas-acceptance"])
    cleanup = steps["Remove Docker 29.4.3 compatibility daemon"]
    assert cleanup["if"] == "always()"
    result = _run_workflow_shell(cleanup, tmp_path, server_version="29.4.3")
    assert result.returncode == 0, result.stderr
    assert "|rm --force vonk-acceptance-dind" in result.docker_log  # type: ignore[attr-defined]

    assert "Restore native Docker networking" not in steps
    step_names = [step["name"] for step in nas_steps]
    assert (
        step_names.index("Download verified Compose parser fixtures")
        < step_names.index(
            "Run literal clean NAS and Tailscale configuration acceptance"
        )
        < step_names.index("Start Docker 29.4.3 compatibility daemon")
    )
    assert (
        step_names.index("Run Docker 29.4.3 NAS compatibility acceptance")
        < (step_names.index("Remove Docker 29.4.3 compatibility daemon"))
        < step_names.index("Upload NAS behavioral gate report")
    )

    candidate = _steps(jobs["candidate"])[
        "Upload immutable publication bundle and candidate receipt"
    ]
    assert set(candidate["with"]["path"].splitlines()) == {
        "${{ runner.temp }}/installer-publication",
        "${{ runner.temp }}/candidate-receipt.jsonl",
    }


def test_spark_acceptance_is_native_on_both_linux_architectures() -> None:
    spark = _workflow()["jobs"]["spark-acceptance"]
    assert spark["permissions"] == {"contents": "read"}
    assert spark["strategy"]["matrix"]["include"] == [
        {"platform": "linux-amd64", "runner": "ubuntu-24.04"},
        {"platform": "linux-arm64", "runner": "ubuntu-24.04-arm"},
    ]
    report = _steps(spark)["Upload Spark behavioral gate report"]
    assert report["uses"].startswith("actions/upload-artifact@")
    assert report["with"]["if-no-files-found"] == "error"


def test_spark_acceptance_enables_and_verifies_native_docker_cdi() -> None:
    spark = _workflow()["jobs"]["spark-acceptance"]
    steps = _steps(spark)
    cdi = steps["Enable Docker CDI for the synthetic Spark device"]["run"]
    names = list(steps)

    assert names.index("Enable Docker CDI for the synthetic Spark device") < names.index(
        "Run packaged Spark fresh-install, pairing, job, and renewal acceptance"
    )
    assert '.features = ((.features // {}) + {"cdi": true})' in cdi
    assert 'dockerd --validate --config-file "$updated_config"' in cdi
    assert "systemctl restart docker.service" in cdi
    assert "docker info --format '{{json .CDISpecDirs}}'" in cdi
    assert 'index("/etc/cdi") != null' in cdi


def test_acceptance_jobs_do_not_claim_a_same_host_external_tailnet_boundary() -> None:
    jobs = _workflow()["jobs"]
    for name in ("nas-acceptance", "spark-acceptance"):
        assert "Join tailnet as isolated acceptance client" not in _steps(jobs[name])
        assert jobs[name]["permissions"] == {"contents": "read"}
    native = _steps(jobs["nas-acceptance"])[
        "Run literal clean NAS and Tailscale configuration acceptance"
    ]
    assert native["env"]["VONK_ACCEPTANCE_REQUIRE_TAILNET_CLIENT"] == "false"


def test_spark_job_gate_is_owned_only_by_the_native_arm64_workload_runner() -> None:
    spark = _workflow()["jobs"]["spark-acceptance"]
    steps = _steps(spark)
    publication = steps["Download exact Spark publication graph"]
    assert publication["uses"].startswith("actions/download-artifact@")
    assert publication["with"] == {
        "name": (
            "installer-candidate-${{ needs.authority.outputs.channel }}-"
            "${{ needs.candidate.outputs.generation }}"
        ),
        "path": "${{ runner.temp }}/spark-publication",
    }
    run = steps["Run packaged Spark fresh-install, pairing, job, and renewal acceptance"][
        "run"
    ]
    environment = steps[
        "Run packaged Spark fresh-install, pairing, job, and renewal acceptance"
    ]["env"]

    immutable_root = '"$RUNNER_TEMP/spark-publication/installer-publication/objects"'
    assert environment["VONK_ACCEPTANCE_WORKSPACE"] == "${{ github.workspace }}"
    assert 'test "$VONK_ACCEPTANCE_PLATFORM" = linux-amd64 || ' in run
    assert 'test "$VONK_ACCEPTANCE_PLATFORM" = linux-arm64' in run
    assert "tests/acceptance/test_spark_lifecycle.py run" in run
    assert f"--object-root {immutable_root}" in run
    assert (
        "--candidate-release "
        f"{immutable_root[:-1]}/artifacts/$VONK_ACCEPTANCE_CHANNEL/releases/"
        '$VONK_ACCEPTANCE_GENERATION/release.json"'
    ) in run
    assert (
        "--baseline-release "
        f"{immutable_root[:-1]}/artifacts/$VONK_ACCEPTANCE_CHANNEL/releases/"
        '$VONK_ACCEPTANCE_GENERATION/acceptance-baseline/release.json"'
    ) in run
    assert (
        '--output "$RUNNER_TEMP/spark-acceptance/report-$VONK_ACCEPTANCE_PLATFORM.json"'
    ) in run
    assert "jq " not in run
    assert "schema_version:1" not in run
    assert "gates=" not in run


def test_complete_acceptance_is_signed_before_the_channel_can_advance() -> None:
    jobs = _workflow()["jobs"]
    acceptance = jobs["acceptance"]
    assert acceptance["permissions"] == {"actions": "read", "contents": "read"}
    assert acceptance["environment"] == (
        "installer-acceptance-${{ needs.authority.outputs.channel }}"
    )
    receipt = _steps(acceptance)["Upload signed acceptance receipt"]
    assert receipt["uses"].startswith("actions/upload-artifact@")
    assert receipt["with"]["if-no-files-found"] == "error"
    promote = jobs["promote"]
    assert promote["environment"] == (
        "installer-promotion-${{ needs.authority.outputs.channel }}"
    )
    assert promote["concurrency"]["cancel-in-progress"] == "false"


def test_acceptance_authority_binds_reports_to_downloaded_candidate_objects() -> None:
    acceptance = _workflow()["jobs"]["acceptance"]
    steps = _steps(acceptance)
    candidate = steps["Download exact candidate publication graph"]
    assert candidate["uses"].startswith("actions/download-artifact@")
    assert candidate["with"] == {
        "name": (
            "installer-candidate-${{ needs.authority.outputs.channel }}-"
            "${{ needs.candidate.outputs.generation }}"
        ),
        "path": "${{ runner.temp }}/candidate-publication",
    }
    signing = steps["Bind and sign complete acceptance"]["run"]
    immutable_root = (
        '"$RUNNER_TEMP/candidate-publication/installer-publication/objects"'
    )
    assert f"--object-root {immutable_root}" in signing
    assert (
        "--candidate-release "
        f'{immutable_root[:-1]}/artifacts/$CHANNEL/releases/$GENERATION/release.json"'
    ) in signing
    assert (
        "--baseline-release "
        f"{immutable_root[:-1]}/artifacts/$CHANNEL/releases/$GENERATION/"
        'acceptance-baseline/release.json"'
    ) in signing


def test_publication_refreshes_both_signed_channels_before_expiry() -> None:
    workflow = _workflow()
    assert workflow["on"]["schedule"] == [{"cron": "17 3 * * *"}]
    refresh = workflow["jobs"]["refresh"]
    assert refresh["if"] == "github.event_name == 'schedule'"
    assert {entry["channel"] for entry in refresh["strategy"]["matrix"]["include"]} == {
        "dev",
        "stable",
    }
    assert refresh["environment"] == "installer-promotion-${{ matrix.channel }}"


def test_setup_build_matrix_is_complete_and_native() -> None:
    workflow = _workflow(SETUPS)
    matrix = workflow["jobs"]["build-and-test"]["strategy"]["matrix"]["include"]
    actual = {
        (entry["platform"], entry["runner"], entry["binaries"]) for entry in matrix
    }
    assert actual == {
        ("linux-amd64", "ubuntu-24.04", "vonk-nas-setup vonk-spark-setup"),
        ("linux-arm64", "ubuntu-24.04-arm", "vonk-nas-setup vonk-spark-setup"),
        ("darwin-amd64", "macos-15-intel", "vonk-nas-setup"),
        ("darwin-arm64", "macos-15", "vonk-nas-setup"),
    }
    assert all("target" not in entry for entry in matrix)


def test_setup_checksums_are_portable_and_exclude_the_manifest_itself() -> None:
    workflow = _workflow(SETUPS)
    run = _steps(workflow["jobs"]["build-and-test"])[
        "Test and build exact native setup programs"
    ]["run"]

    assert 'cd "$output"' in run
    assert "sha256sum -- $BINARIES" in run
    assert "> SHA256SUMS" in run
    assert 'find "$output"' not in run
