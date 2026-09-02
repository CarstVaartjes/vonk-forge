from __future__ import annotations

import os
import re
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
    assert set(jobs["nas-lane-acceptance"]["needs"]) == {
        "authority",
        "candidate",
    }
    assert "always()" in jobs["nas-lane-acceptance"]["if"]
    assert "needs.authority.result == 'success'" in jobs["nas-lane-acceptance"]["if"]
    assert "needs.candidate.result == 'success'" in jobs["nas-lane-acceptance"]["if"]
    assert jobs["nas-lane-acceptance"]["strategy"] == {
        "fail-fast": "false",
        "max-parallel": "2",
        "matrix": {
            "include": [
                {
                    "lane": "native",
                    "gateway": (
                        "vonk-forge-ci-${{ github.run_id }}-"
                        "${{ github.run_attempt }}-native"
                    ),
                    "tailscale_mode": "full",
                },
                {
                    "lane": "docker-29.4.3",
                    "tailscale_mode": "disabled",
                },
            ]
        },
    }
    assert set(jobs["nas-acceptance"]["needs"]) == {
        "authority",
        "candidate",
        "nas-lane-acceptance",
    }
    assert (
        "needs['nas-lane-acceptance'].result == 'success'"
        in jobs["nas-acceptance"]["if"]
    )
    assert set(jobs["spark-acceptance"]["needs"]) == {
        "authority",
        "candidate",
    }
    assert jobs["spark-acceptance"]["strategy"]["max-parallel"] == "2"
    assert "nas-lane-acceptance" not in jobs["spark-acceptance"]["if"]
    expected_services = {
        "VONK_ACCEPTANCE_TAILSCALE_CONTROL_SERVICE": "svc:vonk-forge",
        "VONK_ACCEPTANCE_TAILSCALE_HERMES_API_SERVICE": "svc:hermes-api",
        "VONK_ACCEPTANCE_TAILSCALE_HERMES_DASHBOARD_SERVICE": "svc:hermes-dashboard",
    }
    nas_environment = _steps(jobs["nas-lane-acceptance"])[
        "Run literal clean NAS and Tailscale configuration acceptance"
    ]["env"]
    spark_environment = _steps(jobs["spark-acceptance"])[
        "Run packaged Spark fresh-install, pairing, job, and renewal acceptance"
    ]["env"]
    for name, service in expected_services.items():
        assert nas_environment[name] == service
        assert name not in spark_environment
    assert nas_environment["VONK_ACCEPTANCE_TAILSCALE_GATEWAY_HOSTNAME"] == (
        "${{ matrix.gateway }}"
    )
    assert nas_environment["VONK_ACCEPTANCE_TAILSCALE_MODE"] == "full"
    assert not {
        "VONK_ACCEPTANCE_TAILNET_DNS_SUFFIX",
        "VONK_ACCEPTANCE_TAILNET_KIND",
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID",
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET",
    } & set(nas_environment)
    assert not any("-acceptance" in value for value in expected_services.values())
    assert spark_environment["VONK_ACCEPTANCE_SPARK_CONTROLLER_BOUNDARY"] == (
        "loopback"
    )
    assert not {
        "VONK_ACCEPTANCE_TAILNET_DNS_SUFFIX",
        "VONK_ACCEPTANCE_TAILSCALE_GATEWAY_HOSTNAME",
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID",
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET",
    } & set(spark_environment)
    assert set(jobs["acceptance"]["needs"]) == {
        "authority",
        "candidate",
        "nas-acceptance",
        "spark-acceptance",
    }
    assert set(jobs["promote"]["needs"]) == {"authority", "candidate", "acceptance"}
    assert "publish" not in jobs


def test_publication_concurrency_only_serializes_pushes_for_the_same_source() -> None:
    workflow = _workflow()
    concurrency = workflow["concurrency"]
    group = " ".join(concurrency["group"].split())

    assert group == (
        "vonk-forge-installer-${{ "
        "(github.event_name == 'workflow_run' && "
        "github.event.workflow_run.event == 'push' && "
        "github.event.workflow_run.head_sha) || github.run_id }}"
    )
    assert concurrency["cancel-in-progress"] == "false"

    def selected_identity(
        event_name: str,
        source_event: str,
        source_sha: str,
        publication_run_id: str,
    ) -> str:
        # Python's and/or has the same precedence and selected-value behavior
        # as GitHub expressions' &&/|| for these string and boolean operands.
        return (
            event_name == "workflow_run" and source_event == "push" and source_sha
        ) or publication_run_id

    cases = (
        ("workflow_run", "push", "a" * 40, "101", "a" * 40),
        ("workflow_run", "workflow_dispatch", "a" * 40, "102", "102"),
        ("workflow_run", "push", "", "103", "103"),
        ("schedule", "", "", "104", "104"),
    )
    for event_name, source_event, source_sha, run_id, expected in cases:
        assert (
            selected_identity(event_name, source_event, source_sha, run_id) == expected
        )


def test_development_source_generation_runs_for_every_main_commit() -> None:
    for path in (DEV_IMAGES, AGENT_RELEASE, SETUPS):
        push = _workflow(path)["on"]["push"]
        assert push["branches"] == ["main"]
        assert "paths" not in push
        assert "paths-ignore" not in push


def test_stale_development_fan_in_is_a_clean_noop() -> None:
    authority = _steps(_workflow()["jobs"]["authority"])[
        "Bind accepted workflow evidence to source authority"
    ]["run"]

    assert 'if test "$SOURCE_SHA" != \\' in authority
    assert 'refs/remotes/origin/main^{commit})"; then' in authority
    assert re.search(r"not_ready\n\s+fi", authority)
    assert (
        'test "$SOURCE_SHA" = \\\n'
        '                "$(git rev-parse --verify refs/remotes/origin/main^{commit})"'
        not in authority
    )


def test_nas_acceptance_uses_verified_compatibility_fixtures_and_a_gate_report() -> (
    None
):
    jobs = _workflow()["jobs"]
    lanes = jobs["nas-lane-acceptance"]
    assert lanes["permissions"] == {"contents": "read"}
    assert "services" not in lanes
    steps = _steps(lanes)
    assert lanes["timeout-minutes"] == "60"
    assert steps["Check out exact candidate acceptance suite"]["timeout-minutes"] == "5"
    assert steps["Install uv and Python"]["timeout-minutes"] == "5"
    fixture_step = steps["Download verified Compose parser fixtures"]
    assert fixture_step["timeout-minutes"] == "5"
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
    assert compatibility_step["env"]["VONK_ACCEPTANCE_TAILSCALE_MODE"] == "disabled"
    assert compatibility_step["env"]["VONK_ACCEPTANCE_TAILNET_DNS_SUFFIX"] == (
        "acceptance.invalid"
    )
    assert "VONK_ACCEPTANCE_REQUIRE_TAILNET_CLIENT" not in compatibility_step["env"]
    assert "VONK_ACCEPTANCE_TAILSCALE_GATEWAY_HOSTNAME" not in compatibility_step["env"]
    assert "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID" not in compatibility_step["env"]
    assert (
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET" not in compatibility_step["env"]
    )
    assert "DOCKER_HOST" not in native_step["env"]
    assert native_step["env"]["VONK_ACCEPTANCE_REQUIRE_TAILNET_CLIENT"] == "false"
    assert native_step["env"]["VONK_ACCEPTANCE_COMPOSE_LOWER"] == (
        "${{ runner.temp }}/compose-fixtures/docker-compose-v2.24.6"
    )
    create_tailnet = steps["Create isolated disposable Tailscale tailnet"]
    assert create_tailnet["if"] == "matrix.lane == 'native'"
    assert create_tailnet["timeout-minutes"] == "5"
    assert create_tailnet["env"] == {
        "VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_ID": (
            "${{ secrets.VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_ID }}"
        ),
        "VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_SECRET": (
            "${{ secrets.VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_SECRET }}"
        ),
    }
    assert "scripts/tailscale-acceptance-tailnet create" in create_tailnet["run"]
    assert '--github-env "$GITHUB_ENV"' in create_tailnet["run"]
    assert (
        '--state "$RUNNER_TEMP/vonk-acceptance-tailnet.json"' in create_tailnet["run"]
    )
    delete_tailnet = steps["Delete isolated disposable Tailscale tailnet"]
    assert delete_tailnet["if"] == "always() && matrix.lane == 'native'"
    assert delete_tailnet["timeout-minutes"] == "5"
    assert "env" not in delete_tailnet
    assert "scripts/tailscale-acceptance-tailnet delete" in delete_tailnet["run"]
    assert (
        '--state "$RUNNER_TEMP/vonk-acceptance-tailnet.json"' in delete_tailnet["run"]
    )
    ordered_steps = list(steps)
    assert (
        ordered_steps.index("Create isolated disposable Tailscale tailnet")
        < ordered_steps.index(
            "Run literal clean NAS and Tailscale configuration acceptance"
        )
        < ordered_steps.index("Delete isolated disposable Tailscale tailnet")
    )
    workflow_text = WORKFLOW.read_text()
    assert "vars.VONK_ACCEPTANCE_TAILNET" not in workflow_text
    assert "secrets.VONK_ACCEPTANCE_TAILSCALE_OAUTH" not in workflow_text
    assert compatibility_step["if"] == "matrix.lane == 'docker-29.4.3'"
    assert native_step["if"] == "matrix.lane == 'native'"
    assert native_step["timeout-minutes"] == "25"
    bounded_before_cleanup = sum(
        int(steps[name]["timeout-minutes"])
        for name in (
            "Check out exact candidate acceptance suite",
            "Install uv and Python",
            "Download verified Compose parser fixtures",
            "Create isolated disposable Tailscale tailnet",
            "Run literal clean NAS and Tailscale configuration acceptance",
        )
    )
    assert int(lanes["timeout-minutes"]) >= (
        bounded_before_cleanup + int(delete_tailnet["timeout-minutes"]) + 10
    )
    assert "nas-acceptance/report" not in compatibility_step["run"]
    assert "nas-acceptance/report" not in native_step["run"]
    assert "for attempt in 1 2; do" not in native_step["run"]
    assert (
        native_step["run"].count(
            "uv run python tests/acceptance/test_fresh_nas_install.py"
        )
        == 1
    )
    lane_report = steps["Write exact NAS lane evidence"]
    assert lane_report["env"]["VONK_ACCEPTANCE_LANE"] == "${{ matrix.lane }}"
    assert "lane:$lane" in lane_report["run"]
    report = _steps(jobs["nas-acceptance"])["Upload NAS behavioral gate report"]
    assert report["uses"].startswith("actions/upload-artifact@")
    assert report["with"]["if-no-files-found"] == "error"
    assert report["with"]["path"] == "${{ runner.temp }}/nas-acceptance/report.json"

    aggregate = jobs["nas-acceptance"]
    assert aggregate["permissions"] == {"contents": "read"}
    aggregate_steps = _steps(aggregate)
    combine = aggregate_steps[
        "Bind both exact NAS lanes to one behavioral gate report"
    ]["run"]
    assert "scripts/install-release-publication combine-nas-evidence" in combine
    assert combine.count("--lane-report") == 2
    assert "report-native.json" in combine
    assert "report-docker-29.4.3.json" in combine


def test_nas_dind_fixture_starts_a_host_network_daemon_and_fails_wrong_version(
    tmp_path: Path,
) -> None:
    nas = _workflow()["jobs"]["nas-lane-acceptance"]
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


def test_nas_lane_reports_are_bound_fail_closed_beside_spark_acceptance() -> None:
    jobs = _workflow()["jobs"]
    lane_steps = _steps(jobs["nas-lane-acceptance"])
    upload = lane_steps["Upload exact NAS lane evidence"]
    assert upload["with"] == {
        "name": (
            "installer-nas-lane-${{ needs.candidate.outputs.generation }}-"
            "${{ matrix.lane }}"
        ),
        "retention-days": "7",
        "if-no-files-found": "error",
        "path": ("${{ runner.temp }}/nas-acceptance/report-${{ matrix.lane }}.json"),
    }

    aggregate = jobs["nas-acceptance"]
    assert "always()" in aggregate["if"]
    aggregate_steps = _steps(aggregate)
    assert (
        aggregate_steps["Check out exact NAS acceptance authority"]["with"]["ref"]
        == "${{ needs.authority.outputs.source_sha }}"
    )
    native = aggregate_steps["Download exact native NAS lane evidence"]["with"]
    docker = aggregate_steps["Download exact Docker NAS lane evidence"]["with"]
    assert native["name"].endswith("${{ needs.candidate.outputs.generation }}-native")
    assert docker["name"].endswith(
        "${{ needs.candidate.outputs.generation }}-docker-29.4.3"
    )
    combine = aggregate_steps["Bind both exact NAS lanes to one behavioral gate report"]
    assert combine["env"] == {
        "VONK_ACCEPTANCE_CHANNEL": "${{ needs.authority.outputs.channel }}",
        "VONK_ACCEPTANCE_GENERATION": "${{ needs.candidate.outputs.generation }}",
        "VONK_ACCEPTANCE_SOURCE_SHA": "${{ needs.authority.outputs.source_sha }}",
        "VONK_ACCEPTANCE_VERSION": "${{ needs.authority.outputs.version }}",
    }
    for argument in ("--channel", "--version", "--source-sha", "--generation"):
        assert argument in combine["run"]
    assert '--run-id "$GITHUB_RUN_ID"' in combine["run"]
    assert jobs["spark-acceptance"]["needs"] == [
        "authority",
        "candidate",
    ]


def test_nas_dind_fixture_is_always_removed_and_candidate_receipt_is_uploaded(
    tmp_path: Path,
) -> None:
    jobs = _workflow()["jobs"]
    nas_steps = jobs["nas-lane-acceptance"]["steps"]
    steps = _steps(jobs["nas-lane-acceptance"])
    cleanup = steps["Remove Docker 29.4.3 compatibility daemon"]
    assert cleanup["if"] == "always() && matrix.lane == 'docker-29.4.3'"
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
        < step_names.index("Upload exact NAS lane evidence")
    )

    candidate = _steps(jobs["candidate"])[
        "Upload immutable publication bundle and candidate receipt"
    ]
    assert set(candidate["with"]["path"].splitlines()) == {
        "${{ runner.temp }}/installer-publication",
        "${{ runner.temp }}/candidate-receipt.jsonl",
    }


def test_promotion_uses_the_candidate_artifact_directory_root() -> None:
    promote = _workflow()["jobs"]["promote"]
    steps = _steps(promote)
    download = steps["Download immutable candidate publication bundle"]
    assert download["with"]["path"] == "${{ runner.temp }}/installer-publication"

    publish = steps["Publish acceptance evidence and advance pointer last"]["run"]
    bundle = '"$RUNNER_TEMP/installer-publication/installer-publication"'
    assert f"--bundle {bundle}" in publish

    receipt = steps["Upload promotion receipt"]
    assert set(receipt["with"]["path"].splitlines()) == {
        (
            "${{ runner.temp }}/installer-publication/installer-publication/"
            "publication-plan.json"
        ),
        "${{ runner.temp }}/promotion-receipt.jsonl",
    }


def test_spark_acceptance_is_native_on_both_linux_architectures() -> None:
    spark = _workflow()["jobs"]["spark-acceptance"]
    assert spark["permissions"] == {"contents": "read"}
    assert spark["strategy"]["matrix"]["include"] == [
        {"platform": "linux-amd64", "runner": "ubuntu-24.04"},
        {"platform": "linux-arm64", "runner": "ubuntu-24.04-arm"},
    ]
    assert (
        len({entry["runner"] for entry in spark["strategy"]["matrix"]["include"]}) == 2
    )
    report = _steps(spark)["Upload Spark behavioral gate report"]
    assert report["uses"].startswith("actions/upload-artifact@")
    assert report["with"]["if-no-files-found"] == "error"


def test_spark_acceptance_enables_and_verifies_native_docker_cdi() -> None:
    spark = _workflow()["jobs"]["spark-acceptance"]
    steps = _steps(spark)
    cdi = steps["Enable Docker CDI for the synthetic Spark device"]["run"]
    names = list(steps)

    assert names.index(
        "Enable Docker CDI for the synthetic Spark device"
    ) < names.index(
        "Run packaged Spark fresh-install, pairing, job, and renewal acceptance"
    )
    assert '.features = ((.features // {}) + {"cdi": true})' in cdi
    assert 'dockerd --validate --config-file "$updated_config"' in cdi
    assert "systemctl restart docker.service" in cdi
    assert "docker info --format '{{json .CDISpecDirs}}'" in cdi
    assert 'index("/etc/cdi") != null' in cdi


def test_acceptance_jobs_do_not_claim_a_same_host_external_tailnet_boundary() -> None:
    jobs = _workflow()["jobs"]
    for name in ("nas-lane-acceptance", "spark-acceptance"):
        assert "Join tailnet as isolated acceptance client" not in _steps(jobs[name])
        assert jobs[name]["permissions"] == {"contents": "read"}
    native = _steps(jobs["nas-lane-acceptance"])[
        "Run literal clean NAS and Tailscale configuration acceptance"
    ]
    assert native["env"]["VONK_ACCEPTANCE_REQUIRE_TAILNET_CLIENT"] == "false"
    docker = _steps(jobs["nas-lane-acceptance"])[
        "Run Docker 29.4.3 NAS compatibility acceptance"
    ]
    assert docker["env"]["VONK_ACCEPTANCE_TAILSCALE_MODE"] == "disabled"
    assert docker["env"]["VONK_ACCEPTANCE_TAILNET_DNS_SUFFIX"] == "acceptance.invalid"
    for forbidden in (
        "VONK_ACCEPTANCE_REQUIRE_TAILNET_CLIENT",
        "VONK_ACCEPTANCE_TAILNET_KIND",
        "VONK_ACCEPTANCE_TAILSCALE_GATEWAY_HOSTNAME",
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID",
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET",
    ):
        assert forbidden not in docker["env"]

    evidence = _steps(jobs["nas-lane-acceptance"])["Write exact NAS lane evidence"]
    assert evidence["env"]["VONK_ACCEPTANCE_TAILSCALE_MODE"] == (
        "${{ matrix.tailscale_mode }}"
    )
    assert "schema_version:2" in evidence["run"]
    assert "tailscale_mode:$tailscale_mode" in evidence["run"]


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
    run = steps[
        "Run packaged Spark fresh-install, pairing, job, and renewal acceptance"
    ]["run"]
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
    availability = next(
        step for step in refresh["steps"] if step.get("id") == "availability"
    )
    assert availability["name"] == "Detect published channel"
    assert "rclone lsf" in availability["run"]
    assert "refs/tags/v" in availability["run"]
    assert "Stable installer channel is not published yet" in availability["run"]
    renewal = next(
        step
        for step in refresh["steps"]
        if step["name"] == "Verify immutable generation and refresh signed pointer"
    )
    assert renewal["if"] == "steps.availability.outputs.published == 'true'"


def test_setup_build_matrix_is_complete_and_native() -> None:
    workflow = _workflow(SETUPS)
    matrix = workflow["jobs"]["build-and-test"]["strategy"]["matrix"]["include"]
    actual = {
        (
            entry["platform"],
            entry["runner"],
            entry["binaries"],
            entry["sccache_sha256"],
        )
        for entry in matrix
    }
    assert actual == {
        (
            "linux-amd64",
            "ubuntu-24.04",
            "vonk-nas-setup vonk-spark-setup",
            "91544c5e9e3440aa0b75e0299cd3e880ac0be91ac537333adee69b507b3495c6",
        ),
        (
            "linux-arm64",
            "ubuntu-24.04-arm",
            "vonk-nas-setup vonk-spark-setup",
            "42998f3f9740df2a53d824cc1008f93337a85002a8801d4f4dc1d739b18aa787",
        ),
        (
            "darwin-amd64",
            "macos-15-intel",
            "vonk-nas-setup",
            "b3a564635eddda9119eeaffe3ada7400188e205907e6b68838c7400ec6f9103b",
        ),
        (
            "darwin-arm64",
            "macos-15",
            "vonk-nas-setup",
            "1e1da56216f82f4968a705ee19991ad1bcf474ab994cc440fd5cdfd045f7c4e8",
        ),
    }
    assert all("target" not in entry for entry in matrix)
    steps = _steps(workflow["jobs"]["build-and-test"])
    cache = steps["Restore Rust dependency cache"]
    assert cache["uses"] == (
        "Swatinem/rust-cache@6323deb102c322ba6fcbdcafc7e3dddab59af2b6"
    )
    assert cache["with"] == {
        "shared-key": "${{ matrix.platform }}",
        "add-job-id-key": "false",
    }
    compiler_cache = steps["Run Rust compiler cache"]
    assert compiler_cache["uses"] == (
        "mozilla-actions/sccache-action@fc920bf0ec8de6ee65d409111f7ec508035751ba"
    )
    assert compiler_cache["with"] == {"version": "v0.16.0"}
    verification = steps["Verify pinned Rust compiler cache binary"]
    assert verification["env"] == {
        "EXPECTED_SCCACHE_SHA256": "${{ matrix.sccache_sha256 }}"
    }
    verification_run = verification["run"]
    assert 'test -n "${SCCACHE_PATH:-}"' in verification_run
    assert 'test -x "$SCCACHE_PATH"' in verification_run
    assert 'sha256sum "$SCCACHE_PATH"' in verification_run
    assert 'shasum -a 256 "$SCCACHE_PATH"' in verification_run
    assert '"$actual" != "$EXPECTED_SCCACHE_SHA256"' in verification_run
    build_step = steps["Test and build exact native setup programs"]
    assert build_step["env"] == {
        "BINARIES": "${{ matrix.binaries }}",
        "PLATFORM": "${{ matrix.platform }}",
        "RUSTC_WRAPPER": "sccache",
        "SCCACHE_GHA_ENABLED": "true",
    }
    run = build_step["run"]
    step_names = [step["name"] for step in workflow["jobs"]["build-and-test"]["steps"]]
    assert step_names.index(
        "Verify pinned Rust compiler cache binary"
    ) < step_names.index("Test and build exact native setup programs")
    package_argument = 'package_args+=(--package "$binary")'
    release_test = 'cargo test --locked --release "${package_args[@]}"'
    release_build = 'cargo build --locked --release --package "$binary" --bin "$binary"'
    assert 'read -r -a binaries <<< "$BINARIES"' in run
    assert 'test "${#binaries[@]}" -ge 1' in run
    assert package_argument in run
    assert release_test in run
    assert release_build in run
    assert run.count("cargo test --locked --release") == 1
    assert 'cargo test --locked --release --package "$binary"' not in run
    assert (
        run.index(package_argument) < run.index(release_test) < run.index(release_build)
    )


def test_setup_build_tests_all_packages_together_before_exact_binary_builds(
    tmp_path: Path,
) -> None:
    workflow = _workflow(SETUPS)
    run = _steps(workflow["jobs"]["build-and-test"])[
        "Test and build exact native setup programs"
    ]["run"]
    commands = tmp_path / "commands"
    commands.mkdir()
    cargo_log = tmp_path / "cargo.log"
    cargo = commands / "cargo"
    cargo.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        'printf \'%s\\n\' "$*" >> "$CARGO_LOG"\n'
        'if [[ "$1" == build ]]; then\n'
        "  binary=\n"
        "  while (($#)); do\n"
        '    if [[ "$1" == --bin ]]; then shift; binary=$1; fi\n'
        "    shift\n"
        "  done\n"
        '  test -n "$binary"\n'
        "  mkdir -p target/release\n"
        '  printf \'%s\\n\' "$binary" > "target/release/$binary"\n'
        "fi\n"
    )
    cargo.chmod(0o755)
    uname = commands / "uname"
    uname.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  -s) printf '%s\\n' Linux ;;\n"
        "  -m) printf '%s\\n' x86_64 ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n"
    )
    uname.chmod(0o755)
    sha256sum = commands / "sha256sum"
    sha256sum.write_text(
        "#!/bin/sh\n"
        'for path in "$@"; do\n'
        '  test "$path" = -- && continue\n'
        "  printf '%064d  %s\\n' 0 \"$path\"\n"
        "done\n"
    )
    sha256sum.chmod(0o755)
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    result = subprocess.run(
        ["bash", "-c", run],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env=os.environ
        | {
            "BINARIES": "vonk-nas-setup vonk-spark-setup",
            "CARGO_LOG": str(cargo_log),
            "PATH": f"{commands}:{os.environ['PATH']}",
            "PLATFORM": "linux-amd64",
            "RUNNER_TEMP": str(runner_temp),
        },
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert cargo_log.read_text().splitlines() == [
        "test --locked --release --package vonk-nas-setup --package vonk-spark-setup",
        "build --locked --release --package vonk-nas-setup --bin vonk-nas-setup",
        "build --locked --release --package vonk-spark-setup --bin vonk-spark-setup",
    ]
    output = runner_temp / "setup/linux-amd64"
    assert (output / "vonk-nas-setup").read_text() == "vonk-nas-setup\n"
    assert (output / "vonk-spark-setup").read_text() == "vonk-spark-setup\n"
    assert (output / "SHA256SUMS").read_text().splitlines() == [
        "0000000000000000000000000000000000000000000000000000000000000000  vonk-nas-setup",
        "0000000000000000000000000000000000000000000000000000000000000000  vonk-spark-setup",
    ]


def test_setup_checksums_are_portable_and_exclude_the_manifest_itself() -> None:
    workflow = _workflow(SETUPS)
    run = _steps(workflow["jobs"]["build-and-test"])[
        "Test and build exact native setup programs"
    ]["run"]

    assert 'cd "$output"' in run
    assert "sha256sum -- $BINARIES" in run
    assert "> SHA256SUMS" in run
    assert 'find "$output"' not in run
