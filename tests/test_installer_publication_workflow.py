from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/installer-publication.yml"
SETUPS = ROOT / ".github/workflows/installer-setups.yml"


def _workflow(path: Path = WORKFLOW) -> dict[str, object]:
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def _steps(job: dict[str, object]) -> dict[str, dict[str, object]]:
    return {step["name"]: step for step in job["steps"]}


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
    assert set(jobs["acceptance"]["needs"]) == {
        "authority",
        "candidate",
        "nas-acceptance",
        "spark-acceptance",
    }
    assert set(jobs["promote"]["needs"]) == {"authority", "candidate", "acceptance"}
    assert "publish" not in jobs


def test_nas_acceptance_uses_verified_compatibility_fixtures_and_a_gate_report() -> None:
    jobs = _workflow()["jobs"]
    nas = jobs["nas-acceptance"]
    assert nas["permissions"] == {"contents": "read"}
    assert nas["services"]["docker-engine"]["image"] == (
        "docker:29.4.3-dind@sha256:685b91dca8eab7de1dce1c303dbb7a763e4082d6a60db10968adf3295fbd2495"
    )
    assert nas["services"]["docker-engine"]["env"] == {"DOCKER_TLS_CERTDIR": ""}
    steps = _steps(nas)
    fixture_step = steps["Download verified Compose parser fixtures"]
    assert fixture_step["shell"] == "bash"
    assert fixture_step["env"] == {
        "COMPOSE_LOWER_SHA256": "eca30ae32dc451f9e6d6c8ddce078a76f23b355c3ca0ab391d58f59e87c0d310",
        "COMPOSE_UGREEN_SHA256": "a0298760c9772d2c06888fc8703a487c94c3c3b0134adeef830742a2fc7647b4",
    }
    acceptance_step = steps["Run literal clean NAS and Compose acceptance"]
    assert acceptance_step["env"]["VONK_ACCEPTANCE_COMPOSE_LOWER"] == (
        "${{ runner.temp }}/compose-fixtures/docker-compose-v2.24.6"
    )
    assert acceptance_step["env"]["VONK_ACCEPTANCE_COMPOSE_UGREEN"] == (
        "${{ runner.temp }}/compose-fixtures/docker-compose-v5.1.3"
    )
    assert acceptance_step["env"]["DOCKER_HOST"] == "tcp://docker-engine:2375"
    assert acceptance_step["env"]["VONK_ACCEPTANCE_REFERENCE_COMPOSE"] == (
        "${{ runner.temp }}/compose-fixtures/docker-compose-v5.1.3"
    )
    report = steps["Upload NAS behavioral gate report"]
    assert report["uses"].startswith("actions/upload-artifact@")
    assert report["with"]["if-no-files-found"] == "error"
    assert report["with"]["path"] == "${{ runner.temp }}/nas-acceptance/report.json"


def test_spark_acceptance_is_native_on_both_linux_architectures() -> None:
    spark = _workflow()["jobs"]["spark-acceptance"]
    assert spark["strategy"]["matrix"]["include"] == [
        {"platform": "linux-amd64", "runner": "ubuntu-24.04"},
        {"platform": "linux-arm64", "runner": "ubuntu-24.04-arm"},
    ]
    report = _steps(spark)["Upload Spark behavioral gate report"]
    assert report["uses"].startswith("actions/upload-artifact@")
    assert report["with"]["if-no-files-found"] == "error"


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
