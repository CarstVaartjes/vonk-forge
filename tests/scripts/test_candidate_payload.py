"""Spark evidence carries over only between candidates with one payload."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.scripts import test_install_release_publication as publication

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/install-release-publication"
SEAL = ROOT / "scripts/seal-agent-deb"
PR_SHA = "d" * 40
PR_RUN = 777001


def _release_key(path: Path) -> Path:
    subprocess.run(
        ["/usr/bin/openssl", "genpkey", "-algorithm", "ED25519", "-out", path],
        check=True,
        capture_output=True,
    )
    path.chmod(0o600)
    return path


def _unsealed(package: Path, directory: Path) -> Path:
    """The fixture package without trust material, as a build leaves it."""
    stage = directory / f"stage-{package.name}"
    subprocess.run(["/usr/bin/dpkg-deb", "-R", package, stage], check=True)
    (stage / "usr/share/keyrings/vonk-forge-release.pub").unlink()
    (stage / "usr/share/keyrings").rmdir()
    unsealed = directory / package.name.replace(".deb", ".unsealed.deb")
    subprocess.run(
        ["/usr/bin/dpkg-deb", "--build", "--root-owner-group", stage, unsealed],
        check=True,
        capture_output=True,
    )
    return unsealed


def _sealed_inputs(root: Path, unsealed: dict[str, Path], key: Path, source: str):
    """Candidate inputs whose agent packages are ``unsealed`` sealed with ``key``."""
    inputs = publication._inputs(root)
    sealed = root / "sealed"
    for group in ("packages", "baseline_packages"):
        mapping = publication._input_mapping(inputs, group)
        for platform, value in mapping.items():
            fixture = Path(str(value))
            result = subprocess.run(
                [
                    SEAL,
                    "--unsealed",
                    unsealed[fixture.name],
                    "--release-private-key",
                    key,
                    "--output-dir",
                    sealed,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            package = sealed / fixture.name
            shutil.copy2(
                fixture.with_suffix(".provenance.json"),
                package.with_suffix(".provenance.json"),
            )
            mapping[platform] = package
    if source != publication.SOURCE_SHA:
        wheel = publication._input_path(inputs, "cli_wheel")
        with zipfile.ZipFile(wheel) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        identity = json.loads(members["cluster_profiles/build-identity.json"])
        identity["source_sha"] = source
        members["cluster_profiles/build-identity.json"] = json.dumps(identity).encode()
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, raw in members.items():
                archive.writestr(name, raw)
    return inputs


def _assemble(root: Path, inputs: dict[str, object], source: str) -> Path:
    command = [
        part.replace(publication.SOURCE_SHA, source)
        for part in publication._assemble_command(root, inputs)
    ]
    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return root / "publication"


def _release(candidate: Path) -> Path:
    plan = json.loads((candidate / "publication-plan.json").read_text())
    return (
        candidate
        / f"objects/artifacts/{plan['channel']}/releases/{plan['generation']}"
        / "release.json"
    )


def _payload(candidate: Path) -> str:
    result = subprocess.run(
        [
            sys.executable,
            SCRIPT,
            "payload",
            "--candidate-release",
            _release(candidate),
            "--object-root",
            candidate / "objects",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture(scope="module")
def candidates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The same artifacts published twice: as a pull request candidate (its
    own key, source and installer key) and as main's candidate."""
    root = tmp_path_factory.mktemp("payload")
    fixtures = publication._inputs(root / "fixtures")
    unsealed_root = root / "unsealed"
    unsealed_root.mkdir()
    unsealed = {
        package.name: _unsealed(package, unsealed_root)
        for group in ("packages", "baseline_packages")
        for package in map(
            Path, map(str, publication._input_mapping(fixtures, group).values())
        )
    }
    pull_request = _sealed_inputs(
        root / "pr", unsealed, _release_key(root / "pr.pem"), PR_SHA
    )
    main = _sealed_inputs(
        root / "main", unsealed, _release_key(root / "main.pem"), publication.SOURCE_SHA
    )
    other = _sealed_inputs(
        root / "other", unsealed, _release_key(root / "other.pem"), PR_SHA
    )
    for value in publication._input_mapping(other, "spark").values():
        path = Path(str(value))
        path.write_bytes(path.read_bytes() + b"# another setup program\n")
    return {
        "pull-request": _assemble(root / "pr", pull_request, PR_SHA),
        "main": _assemble(root / "main", main, publication.SOURCE_SHA),
        "other": _assemble(root / "other", other, PR_SHA),
    }


def test_publication_identity_and_trust_material_are_not_payload(candidates) -> None:
    """Catches a payload that changes with the key, the source commit or the
    installer signatures, or that misses a changed setup program."""
    assert _payload(candidates["pull-request"]) == _payload(candidates["main"])
    assert _payload(candidates["other"]) != _payload(candidates["main"])


def _pr_spark_report(path: Path, candidate: Path) -> Path:
    publication._spark_gate_report(
        path, candidate, publication.ARM64_SPARK_GATES, "linux-arm64", run_id=PR_RUN
    )
    report = json.loads(path.read_text())
    report["source_sha"] = PR_SHA
    path.write_text(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
    return path


def _accept(tmp_path: Path, main: Path, reused: Path, spark_report: Path):
    nas = publication._gate_report(
        tmp_path / "nas.json",
        main,
        publication.ACCEPTANCE_GATES - publication.ARM64_SPARK_GATES,
    )
    output = tmp_path / "acceptance"
    output.mkdir()
    plan = json.loads((reused / "publication-plan.json").read_text())
    release = _release(reused)
    command = publication._accept_command(main, output, [nas]) + [
        "--reuse-candidate-release",
        str(release),
        "--reuse-baseline-release",
        str(release.parent / "acceptance-baseline/release.json"),
        "--reuse-object-root",
        str(reused / "objects"),
        "--reuse-source-sha",
        PR_SHA,
        "--reuse-generation",
        plan["generation"],
        "--reuse-run-id",
        str(PR_RUN),
        "--reuse-gate-report",
        str(spark_report),
    ]
    return output, subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )


def test_main_signs_with_the_pull_request_spark_evidence_for_one_payload(
    tmp_path: Path, candidates
) -> None:
    pull_request = candidates["pull-request"]
    report = _pr_spark_report(tmp_path / "spark.json", pull_request)
    output, result = _accept(tmp_path, candidates["main"], pull_request, report)
    assert result.returncode == 0, result.stderr
    receipt = json.loads((output / "acceptance.json").read_text())
    main_plan = json.loads((candidates["main"] / "publication-plan.json").read_text())
    assert receipt["generation"] == main_plan["generation"]
    assert receipt["source_sha"] == publication.SOURCE_SHA


@pytest.mark.parametrize("fault", ["payload", "generation", "source", "nas"])
def test_evidence_for_other_content_or_identity_is_refused(
    tmp_path: Path, candidates, fault
) -> None:
    """Fail closed: another payload, a report for another generation or
    source, or NAS evidence offered from the pull request is never signed."""
    reused = candidates["other" if fault == "payload" else "pull-request"]
    report = _pr_spark_report(tmp_path / "spark.json", candidates["pull-request"])
    document = json.loads(report.read_text())
    if fault == "generation":
        document["generation"] = "e" * 64
    elif fault == "source":
        document["source_sha"] = publication.SOURCE_SHA
    report.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    )
    if fault == "nas":
        report = publication._gate_report(
            tmp_path / "pr-nas.json",
            candidates["pull-request"],
            publication.ACCEPTANCE_GATES - publication.ARM64_SPARK_GATES,
        )
    output, result = _accept(tmp_path, candidates["main"], reused, report)
    assert result.returncode == 2
    expected = (
        "another payload" if fault == "payload" else "does not match the candidate"
    )
    assert expected in result.stderr
    assert not (output / "acceptance.json").exists()
