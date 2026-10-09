"""A workflow runs a repository Python script inside the environment its imports need.

Batch 7b's release failed: ``scripts/build-nas-compose-bundle`` started importing the
shared Pydantic contracts, but the publication workflow ran it with the runner's bare
Python, which has no pydantic. Every direct workflow invocation of a repository Python
script whose module-level imports need a project package must go through ``uv run``
(a function-level import serves only the subcommands that reach it).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.installer_release import (
    InstallerCandidateArtifacts,
    InstallerCandidateBootstraps,
    InstallerCandidateRelease,
    InstallerPackageArtifact,
    InstallerReleaseImages,
    InstallerReleaseObject,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
# A command line that starts by executing a repository script directly.
_DIRECT = re.compile(r"^\s*(?:[A-Z_]+=\S+\s+)*((?:scripts|tools)/[\w.-]+)(?:\s|$)")
_NEEDS_PROJECT = re.compile(
    r"^(?:from|import)\s+(pydantic|vonk_agent_protocol|vonk_control|vonk_cluster_profiles)\b",
    re.MULTILINE,
)


def _python_script_needing_project(path: Path) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    shebang = text.splitlines()[0] if text.startswith("#!") else ""
    if "python" not in shebang or "uv run" in shebang:
        return None  # not Python, or it brings its own project environment
    found = _NEEDS_PROJECT.search(text)
    return found.group(1) if found else None


def test_workflows_run_project_python_scripts_through_uv() -> None:
    problems = []
    for workflow in WORKFLOWS:
        for number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), 1
        ):
            match = _DIRECT.match(line)
            if not match:
                continue
            module = _python_script_needing_project(ROOT / match.group(1))
            if module:
                problems.append(
                    f"{workflow.name}:{number}: {match.group(1)} imports {module}; "
                    "run it with `uv run --project control --frozen python ...`"
                )
    assert problems == []


def test_contract_generators_prepare_before_project_imports() -> None:
    """A bare interpreter must enter preparation before loading dependencies."""

    scripts = sorted(ROOT.joinpath("scripts").glob("generate-*")) + sorted(
        ROOT.joinpath("scripts").glob("export-*schema*")
    )
    checked = []
    for script in scripts:
        source = script.read_text()
        if "python" not in source.splitlines()[0]:
            continue
        tree = ast.parse(source)
        needs_project = (
            any(
                isinstance(node, ast.ImportFrom)
                and (node.module or "").split(".")[0]
                in {
                    "pydantic",
                    "vonk_agent_protocol",
                    "vonk_control",
                    "vonk_forge_contracts",
                }
                for node in ast.walk(tree)
            )
            or "scripts/export-" in source
        )
        if not needs_project:
            continue
        # No site packages and a preparation sentinel: catches imports before
        # preparation as well as generators that never prepare at all.
        probe = """
import runpy, subprocess, sys, types
def unprepared(*args, **kwargs):
    raise AssertionError("generator effects started before preparation")
subprocess.run = unprepared
script = sys.argv[1]
sys.argv = [script]
helper = types.ModuleType("check_environment")
def prepare(root):
    raise SystemExit(73)
helper.run_in_control = prepare
sys.modules["check_environment"] = helper
runpy.run_path(script, run_name="__main__")
"""
        result = subprocess.run(
            [sys.executable, "-S", "-c", probe, str(script)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 73, f"{script.name}: {result.stderr}"
        checked.append(script.name)
    assert "generate-agent-wire" in checked


def test_native_renewal_helper_uses_candidate_content_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches rebuilding identity from current provenance when a package is reused.

    Execute the workflow's identity exports and manifest producer, then exercise
    the real canary gate up to its first effect. No git, compiler or host services.
    """
    from tests.acceptance.test_spark_lifecycle import LifecycleError, SparkLifecycle

    package = InstallerPackageArtifact(
        path="agent.deb",
        sha256="a" * 64,
        size=1,
        architecture="linux-arm64",
        host_signature="b" * 128,
        package_version="0.1.1~dev.3939+gbe7bee8b1377",
        target_binary_digest="c" * 64,
        target_build_digest="sha256:" + "d" * 64,
    )
    artifact = InstallerReleaseObject(path="object", sha256="a" * 64, size=1)
    release = InstallerCandidateRelease(
        channel="dev",
        generation="e" * 64,
        schema_version=2,
        source_sha="f" * 40,
        version="0.2.0~dev.4000+gf3ad9aa00000",
        images=InstallerReleaseImages(
            **{
                role: f"ghcr.io/vonk/{role}:dev@sha256:{'a' * 64}"
                for role in InstallerReleaseImages.model_fields
            }
        ),
        artifacts=InstallerCandidateArtifacts.model_validate(
            {
                field.alias or name: package
                if name == "agent_package_linux_arm64"
                else artifact
                for name, field in InstallerCandidateArtifacts.model_fields.items()
            }
        ),
        bootstraps=InstallerCandidateBootstraps(spark=artifact, nas=artifact),
    )
    release_path = (
        tmp_path
        / "spark-publication/installer-publication/objects/artifacts/dev/releases"
        / release.generation
        / "release.json"
    )
    release_path.parent.mkdir(parents=True)
    release_path.write_text(release.model_dump_json(by_alias=True))
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/release-acceptance-core.yml").read_text()
    )
    step = next(
        step
        for step in workflow["jobs"]["spark-acceptance"]["steps"]
        if step.get("name") == "Build exact-source native renewal scheduling peer"
    )
    script = step["run"]
    # The reviewed checkout checks are outside this hermetic boundary. Start
    # after the epoch lookup and stop before compilation, preserving all exports.
    exports = script.split('epoch=$(git show -s --format=%ct "$SOURCE_SHA")\n', 1)[
        1
    ].split("cargo build", 1)[0]
    exports = exports.replace(
        "uv run --project control --frozen python", shlex.quote(sys.executable)
    )
    environment = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "SOURCE_SHA": "8" * 40,
        "VERSION": release.version,
        "CHANNEL": release.channel,
        "GENERATION": release.generation,
    }
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            "epoch=0\n"
            + exports
            + 'printf "%s\n%s\n" "$VONK_AGENT_BUILD_DIGEST" "$VONK_AGENT_SEMANTIC_VERSION"',
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    build_digest, semantic_version = result.stdout.splitlines()
    assert build_digest == package.target_build_digest
    assert semantic_version == "0.1.1"
    helper_root = tmp_path / "spark-renewal-helper"
    helper_root.mkdir()
    helper = helper_root / "acceptance_certificate_renewal"
    helper.write_bytes(b"hermetic native helper")
    provenance = script.split("<<'PYPROVENANCE'\n", 1)[1].split("\nPYPROVENANCE", 1)[0]
    subprocess.run(
        [sys.executable, "-c", provenance],
        cwd=ROOT,
        env={**environment, "VONK_AGENT_BUILD_DIGEST": build_digest},
        check=True,
        capture_output=True,
        timeout=10,
    )
    identity = AgentRuntimeIdentity(
        architecture="linux-arm64",
        semantic_version=semantic_version,
        build_digest=build_digest,
        binary_digest=package.target_binary_digest,
    )
    run = SparkLifecycle.__new__(SparkLifecycle)
    monkeypatch.setattr(
        run,
        "_required_environment",
        lambda name: {
            "VONK_ACCEPTANCE_RENEWAL_HELPER": str(helper),
            "VONK_ACCEPTANCE_RENEWAL_HELPER_MANIFEST": str(
                helper_root / "manifest.json"
            ),
        }[name],
    )
    run.arguments = argparse.Namespace(source_sha=release.source_sha)
    monkeypatch.setattr(run, "_self_test", lambda: identity.model_dump(mode="json"))

    class FirstEffect(Exception):
        pass

    def first_effect(*args, **kwargs):
        raise FirstEffect

    monkeypatch.setattr(run, "_run_command", first_effect)
    manifest_path = helper_root / "manifest.json"
    verified_manifest = manifest_path.read_bytes()
    for defect in (None, "build", "binary", "input", "symlink"):
        if defect == "build":
            changed = identity.model_copy(update={"build_digest": "sha256:" + "9" * 64})
            monkeypatch.setattr(
                run,
                "_self_test",
                lambda changed=changed: changed.model_dump(mode="json"),
            )
        elif defect == "binary":
            helper.write_bytes(b"substituted helper")
        elif defect == "input":
            import json

            manifest = json.loads(verified_manifest)
            name = next(iter(manifest["source_inputs"]))
            manifest["source_inputs"][name] = "0" * 64
            manifest_path.write_text(json.dumps(manifest))
        elif defect == "symlink":
            target = helper_root / "substituted"
            target.write_bytes(helper.read_bytes())
            helper.unlink()
            helper.symlink_to(target)
        if defect:
            with pytest.raises(LifecycleError):
                run._exercise_native_renewal()
        # A fresh valid helper is admitted immediately after each refusal.
        monkeypatch.setattr(run, "_self_test", lambda: identity.model_dump(mode="json"))
        if helper.is_symlink():
            helper.unlink()
        helper.write_bytes(b"hermetic native helper")
        manifest_path.write_bytes(verified_manifest)
        with pytest.raises(FirstEffect):
            run._exercise_native_renewal()


@pytest.mark.parametrize(
    "fault", ["reply", "fields", "profile", "timeout", "read", "self-test", "service"]
)
def test_renewal_unknown_observes_once_issued_effect_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """Catches malformed receipts causing a repeated rotation or skipped restart."""
    _exercise_renewal_observation(tmp_path, monkeypatch, fault, exhaust=False)


def test_renewal_observation_exhaustion_allows_a_fresh_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exercise_renewal_observation(tmp_path, monkeypatch, "reply", exhaust=True)


def _exercise_renewal_observation(tmp_path, monkeypatch, fault, *, exhaust):
    import argparse
    import hashlib
    import json
    from types import SimpleNamespace

    from vonk_agent_protocol.agent_state import (
        NativeRenewalClock,
        NativeRenewalEvidence,
        RenewalHelperManifest,
    )

    import tests.acceptance.test_spark_lifecycle as canary

    helper = tmp_path / "helper"
    helper.write_bytes(b"verified native helper")
    manifest = RenewalHelperManifest(
        source_sha="a" * 40,
        binary_sha256=hashlib.sha256(helper.read_bytes()).hexdigest(),
        build_digest="sha256:" + "b" * 64,
        source_inputs={
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in canary.RENEWAL_HELPER_INPUTS
        },
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json())
    run = canary.SparkLifecycle.__new__(canary.SparkLifecycle)
    run.arguments = argparse.Namespace(output=str(tmp_path / "report.json"))
    monkeypatch.setattr(
        run,
        "_required_environment",
        lambda name: (
            str(helper)
            if name == "VONK_ACCEPTANCE_RENEWAL_HELPER"
            else str(manifest_path)
        ),
    )
    elapsed = [0.0]
    monkeypatch.setattr(
        canary,
        "time",
        SimpleNamespace(
            monotonic=lambda: elapsed[0],
            sleep=lambda delay: elapsed.__setitem__(0, elapsed[0] + delay),
        ),
    )
    identity = {
        "architecture": "linux-arm64",
        "semantic_version": "0.1.1",
        "binary_digest": "c" * 64,
        "build_digest": manifest.build_digest,
    }
    identity_reads = [0]

    def self_test():
        identity_reads[0] += 1
        if fault == "self-test" and identity_reads[0] == 1:
            raise canary.LifecycleError("self test observation unavailable")
        return identity

    monkeypatch.setattr(run, "_self_test", self_test)
    if fault == "read":
        original_read = Path.read_bytes
        reads = [0]

        def read(path):
            if path == helper:
                reads[0] += 1
                if reads[0] == 1:
                    raise OSError("temporary read unavailable")
            return original_read(path)

        monkeypatch.setattr(Path, "read_bytes", read)
    evidence = NativeRenewalEvidence(
        scheduling_clock=NativeRenewalClock.CERTIFICATE_DERIVED,
        wall_clock_utc="2026-10-08T00:00:00Z",
        scheduling_clock_utc="2026-11-01T00:00:00Z",
        source_agent_binary_sha256=identity["binary_digest"],
        source_agent_build_digest=identity["build_digest"],
        source_certificate_sha256="d" * 64,
        replacement_certificate_sha256="e" * 64,
        source_public_key_sha256="f" * 64,
        replacement_public_key_sha256="1" * 64,
        source_lifetime_seconds=canary.CERTIFICATE_LIFETIME_SECONDS,
        replacement_lifetime_seconds=canary.CERTIFICATE_LIFETIME_SECONDS,
    )
    rotations, observations, starts, stops = [], [], [], []
    failing = [True]

    def command(arguments, **kwargs):
        if "--renew" in arguments:
            rotations.append(tuple(arguments))
            if failing[0] and fault == "timeout":
                raise canary.LifecycleError("helper reply unavailable")
            reply = (
                "unreadable"
                if failing[0] and fault == "reply"
                else evidence.model_dump_json()
            )
            if failing[0] and fault == "fields":
                reply = evidence.model_dump(exclude={"scheduling_clock"})
                reply = json.dumps(reply)
            if failing[0] and fault == "profile":
                reply = evidence.model_copy(
                    update={"source_lifetime_seconds": 0}
                ).model_dump_json()
        elif "--observe" in arguments:
            observations.append(tuple(arguments))
            reply = (
                "unreadable" if failing[0] and exhaust else evidence.model_dump_json()
            )
        else:
            if "start" in arguments:
                starts.append(tuple(arguments))
            if "stop" in arguments:
                stops.append(tuple(arguments))
                if failing[0] and fault == "service" and len(stops) == 1:
                    raise canary.LifecycleError("service response unavailable")
            reply = ""
        return subprocess.CompletedProcess(arguments, 0, stdout=reply, stderr="")

    monkeypatch.setattr(run, "_run_command", command)
    observed = run._exercise_native_renewal(deadline=elapsed[0] + 10)
    assert len(rotations) == 1 and len(starts) == 1
    if exhaust:
        assert observed is None
        assert elapsed[0] <= 10
        failing[0] = False
        fresh = run._exercise_native_renewal(deadline=elapsed[0] + 10)
        assert fresh is not None
        assert len(rotations) == 2 and len(starts) == 2
    else:
        assert observed is not None
        assert (tmp_path / "renewal-scheduling-clock.json").exists()
        if fault in {"reply", "fields", "profile", "timeout"}:
            assert len(observations) == 1


@pytest.mark.parametrize(
    "workflow_name", ["release-acceptance-core.yml", "spark-upgrade-acceptance.yml"]
)
@pytest.mark.parametrize("features", [None, False, "damaged", [], {"buildkit": False}])
def test_acceptance_repairs_runner_feature_bookkeeping(
    workflow_name: str, features: object
) -> None:
    # Catches refusing a fresh acceptance run because an ephemeral runner has a
    # malformed Docker feature map; unrelated daemon settings must survive.
    workflow = yaml.safe_load((ROOT / ".github/workflows" / workflow_name).read_text())
    scripts = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "daemon_config=/etc/docker/daemon.json" in step.get("run", "")
    ]
    assert len(scripts) == 1
    expression = re.search(r"sudo jq\s+\\\n\s*'([^']+)'", scripts[0])
    assert expression is not None
    result = subprocess.run(
        ["jq", expression[1]],
        input=json.dumps({"features": features, "log-level": "info"}),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    repaired = json.loads(result.stdout)
    assert repaired["features"]["cdi"] is True
    assert repaired["log-level"] == "info"
    if isinstance(features, dict):
        assert repaired["features"]["buildkit"] is False
