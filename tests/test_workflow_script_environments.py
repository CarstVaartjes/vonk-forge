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
        (ROOT / ".github/workflows/installer-publication.yml").read_text()
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
