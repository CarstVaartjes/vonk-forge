from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-image-metadata"
ALIAS_SCRIPT = ROOT / "scripts/promote-image-aliases"
WORKFLOW = ROOT / ".github/workflows/dev-images.yml"
DOCKERFILE = ROOT / "control/Dockerfile"
SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64


def _metadata(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (str(SCRIPT), *arguments),
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        check=False,
        capture_output=True,
        text=True,
    )


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow_document() -> dict[str, Any]:
    document = yaml.safe_load(_workflow())
    assert isinstance(document, dict)
    return document


def _workflow_job(document: dict[str, Any], name: str) -> dict[str, Any]:
    jobs = document.get("jobs")
    assert isinstance(jobs, dict)
    job = jobs.get(name)
    assert isinstance(job, dict)
    return job


def _workflow_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _workflow_step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in _workflow_steps(job) if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_focused_publication_gate_covers_cohort_and_startup_settings() -> None:
    step = _step(_workflow(), "Run focused source and Compose contracts")

    for contract in (
        "deploy/compose/tests/test_dev_compose_secrets.py",
        "scripts/tests/test_dev_runtime_secrets.py",
        "scripts/tests/test_dev_runtime_project.py",
        "scripts/tests/test_dev_admin_token.py",
        "control/tests/test_dev_cohort.py",
        "control/tests/test_settings.py",
    ):
        assert contract in step


def _docker_stage(text: str, name: str) -> str:
    marker = re.compile(rf"^FROM .+ AS {re.escape(name)}$", re.MULTILINE)
    match = marker.search(text)
    assert match is not None
    following = re.search(r"^FROM ", text[match.end() :], re.MULTILINE)
    end = len(text) if following is None else match.end() + following.start()
    return text[match.start() : end]


def _step(text: str, name: str) -> str:
    marker = f"      - name: {name}"
    start = text.index(marker)
    following = text.find("\n      - name: ", start + len(marker))
    return text[start:] if following < 0 else text[start:following]


def _job(text: str, name: str) -> str:
    marker = f"  {name}:"
    start = text.index(marker)
    following = text.find("\n  ", start + len(marker))
    while following >= 0:
        candidate = text[following + 1 :].splitlines()[0]
        if candidate.startswith("  ") and not candidate.startswith("    "):
            break
        following = text.find("\n  ", following + 3)
    return text[start:] if following < 0 else text[start:following]


def _fake_skopeo(tmp_path: Path) -> tuple[Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "state.json"
    log = tmp_path / "skopeo.log"
    state.write_text("{}\n", encoding="utf-8")
    skopeo = fake_bin / "skopeo"
    skopeo.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "state_path=Path(os.environ['VONK_TEST_ALIAS_STATE'])\n"
        "log_path=Path(os.environ['VONK_TEST_ALIAS_LOG'])\n"
        "args=sys.argv[1:]\n"
        "with log_path.open('a') as stream: stream.write(' '.join(args)+'\\n')\n"
        "state=json.loads(state_path.read_text())\n"
        "ref=args[-1]\n"
        "role=('api' if 'vonk-forge-api' in ref else "
        "'worker' if 'vonk-forge-worker' in ref else 'hermes')\n"
        "if args[0] == 'inspect':\n"
        "    digest=state.get(role)\n"
        "    if not digest:\n"
        "        print('manifest unknown', file=sys.stderr); raise SystemExit(1)\n"
        "    print(digest); raise SystemExit(0)\n"
        "if args[0] == 'copy':\n"
        "    source=args[-2]\n"
        "    digest=source.rsplit('@', 1)[1]\n"
        "    failures=set(filter(None, os.environ.get('VONK_TEST_ALIAS_FAILURES','').split(',')))\n"
        "    if f'{role}:{digest}' in failures: raise SystemExit(9)\n"
        "    state[role]=digest\n"
        "    state_path.write_text(json.dumps(state)+'\\n')\n"
        "    raise SystemExit(0)\n"
        "if args[0] == 'delete':\n"
        "    state.pop(role, None)\n"
        "    state_path.write_text(json.dumps(state)+'\\n')\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(97)\n",
        encoding="utf-8",
    )
    skopeo.chmod(0o755)
    return fake_bin, state, log


def _promote_aliases(
    fake_bin: Path,
    state: Path,
    log: Path,
    *,
    failures: str = "",
    alias: str = "dev",
    include_hermes: bool = False,
    postcondition: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    arguments = [
        str(ALIAS_SCRIPT),
        "ghcr.io/carstvaartjes/vonk-forge-api",
        DIGEST_A,
        "ghcr.io/carstvaartjes/vonk-forge-worker",
        DIGEST_B,
        alias,
    ]
    if include_hermes:
        arguments.extend(("ghcr.io/carstvaartjes/vonk-forge-hermes", DIGEST_C))
    if postcondition:
        arguments.extend(("--postcondition", *postcondition))
    return subprocess.run(
        arguments,
        cwd=ROOT,
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "VONK_TEST_ALIAS_STATE": str(state),
            "VONK_TEST_ALIAS_LOG": str(log),
            "VONK_TEST_ALIAS_FAILURES": failures,
        },
        check=False,
        capture_output=True,
        text=True,
    )


def test_metadata_emits_only_the_exact_main_development_channel() -> None:
    result = _metadata("refs/heads/main", SHA, SHA)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        f"commit={SHA}",
        f"immutable_tag=dev-sha-{SHA}",
        "dev_alias=dev",
        "api_image=ghcr.io/carstvaartjes/vonk-forge-api",
        "worker_image=ghcr.io/carstvaartjes/vonk-forge-worker",
        f"artifact_name=vonk-forge-dev-compose-{SHA}",
    ]
    assert "latest" not in result.stdout


@pytest.mark.parametrize(
    "event_ref,selected,origin_main",
    (
        ("refs/heads/feature", SHA, SHA),
        ("refs/tags/v1.2.3", SHA, SHA),
        ("main", SHA, SHA),
        ("refs/heads/main", SHA[:-1], SHA),
        ("refs/heads/main", SHA.upper(), SHA.upper()),
        ("refs/heads/main", SHA, "f" * 40),
    ),
)
def test_metadata_rejects_every_non_tip_or_non_main_selection(
    event_ref: str, selected: str, origin_main: str
) -> None:
    result = _metadata(event_ref, selected, origin_main)

    assert result.returncode == 64
    assert result.stdout == ""
    assert result.stderr == "development image metadata is invalid\n"


def test_metadata_rejects_missing_or_extra_arguments() -> None:
    assert _metadata().returncode == 64
    assert _metadata("refs/heads/main", SHA, SHA, SHA).returncode == 64


def test_alias_reconciliation_is_idempotent_for_initial_and_repeated_runs(
    tmp_path: Path,
) -> None:
    fake_bin, state, log = _fake_skopeo(tmp_path)

    first = _promote_aliases(fake_bin, state, log)
    copies_after_first = log.read_text(encoding="utf-8").count("copy ")
    second = _promote_aliases(fake_bin, state, log)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "api": DIGEST_A,
        "worker": DIGEST_B,
    }
    assert copies_after_first == 2
    assert log.read_text(encoding="utf-8").count("copy ") == copies_after_first


def test_alias_reconciliation_rolls_forward_an_initial_partial_state(
    tmp_path: Path,
) -> None:
    fake_bin, state, log = _fake_skopeo(tmp_path)
    state.write_text(json.dumps({"api": DIGEST_A}) + "\n", encoding="utf-8")

    result = _promote_aliases(fake_bin, state, log)

    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "api": DIGEST_A,
        "worker": DIGEST_B,
    }
    assert log.read_text(encoding="utf-8").count("copy ") == 1


def test_same_reconciliation_contract_applies_to_latest(tmp_path: Path) -> None:
    fake_bin, state, log = _fake_skopeo(tmp_path)

    result = _promote_aliases(
        fake_bin, state, log, alias="latest", include_hermes=True
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "api": DIGEST_A,
        "worker": DIGEST_B,
        "hermes": DIGEST_C,
    }
    assert all(":latest" in line for line in log.read_text().splitlines())


def test_latest_failure_restores_an_established_triplet(tmp_path: Path) -> None:
    fake_bin, state, log = _fake_skopeo(tmp_path)
    state.write_text(
        json.dumps({"api": DIGEST_D, "worker": DIGEST_D, "hermes": DIGEST_D})
        + "\n",
        encoding="utf-8",
    )

    result = _promote_aliases(
        fake_bin,
        state,
        log,
        alias="latest",
        include_hermes=True,
        failures=f"hermes:{DIGEST_C}",
    )

    assert result.returncode != 0
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "api": DIGEST_D,
        "worker": DIGEST_D,
        "hermes": DIGEST_D,
    }


def test_alias_failure_restores_an_established_pair(tmp_path: Path) -> None:
    fake_bin, state, log = _fake_skopeo(tmp_path)
    state.write_text(
        json.dumps({"api": DIGEST_C, "worker": DIGEST_D}) + "\n",
        encoding="utf-8",
    )

    result = _promote_aliases(
        fake_bin, state, log, failures=f"worker:{DIGEST_B}"
    )

    assert result.returncode != 0
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "api": DIGEST_C,
        "worker": DIGEST_D,
    }
    assert "not advanced as a set" in result.stderr


def test_alias_failure_is_red_when_rollback_cannot_converge(tmp_path: Path) -> None:
    fake_bin, state, log = _fake_skopeo(tmp_path)
    state.write_text(
        json.dumps({"api": DIGEST_C, "worker": DIGEST_D}) + "\n",
        encoding="utf-8",
    )

    result = _promote_aliases(
        fake_bin,
        state,
        log,
        failures=f"worker:{DIGEST_B},api:{DIGEST_C}",
    )

    assert result.returncode != 0
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "api": DIGEST_A,
        "worker": DIGEST_D,
    }
    assert "could not be restored" in result.stderr


def test_postcondition_failure_restores_the_complete_prior_alias_set(
    tmp_path: Path,
) -> None:
    fake_bin, state, log = _fake_skopeo(tmp_path)
    state.write_text(
        json.dumps({"api": DIGEST_D, "worker": DIGEST_D, "hermes": DIGEST_D})
        + "\n",
        encoding="utf-8",
    )
    postcondition = fake_bin / "postcondition"
    postcondition.write_text("#!/usr/bin/env bash\nexit 23\n", encoding="utf-8")
    postcondition.chmod(0o755)

    result = _promote_aliases(
        fake_bin,
        state,
        log,
        alias="latest",
        include_hermes=True,
        postcondition=(str(postcondition),),
    )

    assert result.returncode != 0
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "api": DIGEST_D,
        "worker": DIGEST_D,
        "hermes": DIGEST_D,
    }
    assert "not advanced as a set" in result.stderr


def test_initial_postcondition_failure_happens_before_any_alias_mutation(
    tmp_path: Path,
) -> None:
    fake_bin, state, log = _fake_skopeo(tmp_path)
    postcondition = fake_bin / "postcondition"
    postcondition.write_text("#!/usr/bin/env bash\nexit 23\n", encoding="utf-8")
    postcondition.chmod(0o755)

    result = _promote_aliases(
        fake_bin,
        state,
        log,
        alias="latest",
        include_hermes=True,
        postcondition=(str(postcondition),),
    )

    assert result.returncode != 0
    assert json.loads(state.read_text(encoding="utf-8")) == {}
    assert not any(
        line.startswith(("copy ", "delete "))
        for line in log.read_text(encoding="utf-8").splitlines()
    )


def test_failed_first_publication_is_repairable_by_failed_job_rerun(
    tmp_path: Path,
) -> None:
    fake_bin, state, log = _fake_skopeo(tmp_path)
    failed = _promote_aliases(
        fake_bin, state, log, failures=f"worker:{DIGEST_B}"
    )
    partial = json.loads(state.read_text(encoding="utf-8"))
    repaired = _promote_aliases(fake_bin, state, log)

    assert failed.returncode != 0
    assert partial == {"api": DIGEST_A}
    assert repaired.returncode == 0, repaired.stderr
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "api": DIGEST_A,
        "worker": DIGEST_B,
    }
    assert "delete " not in log.read_text(encoding="utf-8")


def test_workflow_is_main_only_publication_without_repository_secrets() -> None:
    text = _workflow()
    validator = _job(text, "build-and-accept")
    publisher = _job(text, "publish-development-images")

    assert "branches: [main]" in text
    assert "workflow_dispatch:" in text
    assert "packages: write" in text
    assert "packages: write" not in validator
    assert "attestations: write" in publisher
    assert "contents: read" in publisher
    assert "id-token: write" in publisher
    assert "packages: write" in publisher
    assert text.count("packages: write") == 1
    assert "contents: read" in text
    assert "environment:" not in text
    assert "id-token: write" in publisher
    assert "attestations: write" in publisher
    assert "secrets.GITHUB_TOKEN" in _step(text, "Log in to GHCR")
    assert text.count("${{ secrets.") == 1
    assert "refs/remotes/origin/main" in _step(text, "Verify exact main tip")
    assert '"$GITHUB_REF" "$GITHUB_SHA"' in _step(text, "Verify exact main tip")
    checkout = _step(text, "Check out full main history")
    assert "persist-credentials: false" in checkout


def test_workflow_yaml_and_embedded_bash_are_structurally_valid() -> None:
    document = _workflow_document()
    jobs = document.get("jobs")
    assert isinstance(jobs, dict)
    assert {"build-and-accept", "publish-development-images"} <= set(jobs)

    for job_name, untyped_job in jobs.items():
        assert isinstance(untyped_job, dict)
        for step in _workflow_steps(untyped_job):
            script = step.get("run")
            if script is None:
                continue
            assert step.get("shell") == "bash"
            assert isinstance(script, str)
            shell_input = re.sub(
                r"\$\{\{[^{}]+\}\}", "github-expression", script
            )
            result = subprocess.run(
                ("bash", "-n"),
                input=shell_input,
                check=False,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"{job_name}/{step.get('name')}: {result.stderr}"
            assert result.stderr == "", f"{job_name}/{step.get('name')}: {result.stderr}"


def test_complete_local_scan_gates_every_external_publication() -> None:
    document = _workflow_document()
    builder = _workflow_job(document, "build-and-accept")
    publisher = _workflow_job(document, "publish-development-images")
    build_steps = _workflow_steps(builder)
    publish_steps = _workflow_steps(publisher)

    assert publisher.get("needs") == ["build-and-accept"]
    assert "if" not in publisher

    scan = _workflow_step(builder, "Scan complete local publication inputs")
    scan_index = build_steps.index(scan)
    accepted_upload = _workflow_step(builder, "Upload accepted publication inputs")
    for prerequisite in (
        "Build exact OCI archives",
        "Render and validate disposable development Compose artifacts",
        "Render and smoke complete disposable development stack",
    ):
        assert build_steps.index(_workflow_step(builder, prerequisite)) < scan_index
    assert scan_index < build_steps.index(accepted_upload)
    assert not any(
        str(step.get("uses", "")).startswith("actions/upload-artifact@")
        for step in build_steps[: scan_index + 1]
    )
    assert not any(
        str(step.get("uses", "")).startswith(
            ("actions/attest@", "docker/login-action@")
        )
        for step in build_steps
    )
    pre_scan_scripts = "\n".join(
        str(step.get("run", "")) for step in build_steps[:scan_index]
    )
    assert "skopeo copy --all" not in pre_scan_scripts
    assert "push-to-registry: true" not in pre_scan_scripts

    scan_script = scan.get("run")
    assert isinstance(scan_script, str)
    assert "docker history --no-trunc" in scan_script
    assert "docker image inspect" in scan_script
    assert "tar --extract" in scan_script
    assert 'index_queue=("$layout/index.json")' in scan_script
    assert "application/vnd.oci.image.index.v1+json" in scan_script
    assert 'index_queue+=("$descriptor_path")' in scan_script
    assert "attestation-manifest" in scan_script
    assert "https://spdx.dev/Document" in scan_script
    assert "https://slsa.dev/provenance/" in scan_script
    assert "scripts/verify-dev-image-secrets" in scan_script
    assert '--forbid-bytes-dir "$canary_root"' in scan_script
    assert '--scan-path "$publication_root"' in scan_script
    assert '--scan-path "$evidence_root"' in scan_script
    assert "vonk-forge-api:dev-local vonk-forge-worker:dev-local" in scan_script

    accepted_paths = accepted_upload.get("with", {}).get("path")
    assert isinstance(accepted_paths, str)
    for filename in (
        "vonk-forge-api.oci.tar",
        "vonk-forge-worker.oci.tar",
        "docker-compose.pinned.yml",
        "docker-compose.dev.yml",
    ):
        assert f"accepted/{filename}" in accepted_paths

    publish_names = [step.get("name") for step in publish_steps]
    assert publish_names.index("Publish immutable tested images") < publish_names.index(
        "Sign accepted API image provenance"
    )
    assert publish_names.index("Publish immutable tested images") < publish_names.index(
        "Sign accepted worker image provenance"
    )
    assert not any("Scan" in str(name) for name in publish_names)

    compose_upload = _workflow_step(publisher, "Upload Compose artifact")
    compose_paths = compose_upload.get("with", {}).get("path")
    assert isinstance(compose_paths, str)
    assert "accepted/docker-compose.pinned.yml" in compose_paths
    assert "accepted/docker-compose.dev.yml" in compose_paths
    assert not any(
        "scripts/render-dev-compose" in str(step.get("run", ""))
        for step in publish_steps
    )


def test_workflow_builds_scans_and_accepts_oci_archives_before_login() -> None:
    text = _workflow()
    build = _step(text, "Build exact OCI archives")
    load = _step(text, "Load tested images without pulling")
    preload = _step(text, "Preload pinned runtime dependencies")
    render = _step(text, "Render and validate disposable development Compose artifacts")
    smoke = _step(text, "Render and smoke complete disposable development stack")
    accept = _step(text, "Scan complete local publication inputs")

    assert "docker buildx build" in build
    assert "--platform linux/amd64" in build
    assert "--target api" in build and "--target worker" in build
    assert "type=oci" in build
    assert "--sbom=true" in build
    assert "--provenance=mode=max" in build
    assert build.count('--build-arg VONK_DEV_SOURCE_COMMIT="$GITHUB_SHA"') == 2
    assert build.count("--build-arg") == 2
    assert "--secret" not in build
    assert "skopeo copy" in load
    assert "oci-archive:" in load
    assert "docker-daemon:vonk-forge-api:dev-local" in load
    assert "docker-daemon:vonk-forge-worker:dev-local" in load
    assert "deploy/compose/compose.dev.images.yaml" in preload
    assert "mapfile -t runtime_images" in preload
    assert "@sha256:[0-9a-f]{64}$" in preload
    assert 'test "${#runtime_images[@]}" = 3' in preload
    assert 'for runtime_image in "${runtime_images[@]}"; do' in preload
    assert 'docker pull "$runtime_image"' in preload
    assert 'scripts/render-dev-compose \\' in render
    assert "docker-compose.pinned.yml" in render
    assert "docker-compose.dev.yml" in render
    assert "--channel dev" in render
    assert 'docker compose -f "$publication_root/docker-compose.pinned.yml" config -q' in render
    assert 'docker compose -f "$publication_root/docker-compose.dev.yml" config -q' in render
    assert "deploy/compose/tests/test_dev_complete_stack.py" in smoke
    assert "test_complete_development_stack_enforces_tls_identity_and_acks_routes" in smoke
    assert "--maxfail=1" in smoke
    assert "200|400|401|403|405|409|415|422" not in smoke
    assert "enroll_status" not in smoke
    assert "uv run --project control --frozen pytest" in smoke
    assert text.index("Preload pinned runtime dependencies") < text.index(
        "Render and validate disposable development Compose artifacts"
    )
    assert text.index("Render and validate disposable development Compose artifacts") < text.index(
        "Render and smoke complete disposable development stack"
    )
    assert text.index("Render and smoke complete disposable development stack") < text.index(
        "Scan complete local publication inputs"
    )
    assert "scripts/verify-dev-image-secrets" in accept
    assert '--expected-commit "$GITHUB_SHA"' in accept
    assert '--expected-repository "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY"' in accept
    assert "scripts/dev-image-acceptance" in accept
    assert text.index("Scan complete local publication inputs") < text.index("Log in to GHCR")
    assert "Upload accepted publication inputs" in text
    assert "needs: [build-and-accept]" in text
    assert "Download accepted OCI archives" in text


def test_development_dockerfile_embeds_role_identity_without_authority_build_args() -> None:
    text = _dockerfile()
    zero_commit = "0" * 40

    assert f"ARG VONK_DEV_SOURCE_COMMIT={zero_commit}" in text
    for role in ("api", "worker"):
        stage = _docker_stage(text, f"{role}-root")
        target = _docker_stage(text, role)
        assert "ARG VONK_DEV_SOURCE_COMMIT" in stage
        assert "vonk_control.dev_cohort" in stage
        assert "DEVELOPMENT_IMAGE_IDENTITY_PATH" in stage
        assert "build_identity" in stage
        assert f'"{role}"' in stage
        assert f"COPY --from={role}-root / /" in target

    argument_names = {
        line.split("=", 1)[0].split()[1]
        for line in text.splitlines()
        if line.startswith("ARG ")
    }
    assert not any(
        marker in name.lower()
        for name in argument_names
        for marker in ("credential", "password", "secret", "token", "key")
    )


def test_workflow_publishes_prevalidated_archives_and_compose_channels() -> None:
    text = _workflow()
    publish = _step(text, "Publish immutable tested images")
    verify = _step(text, "Verify immutable manifests and attestations")
    render = _step(text, "Render and validate disposable development Compose artifacts")
    artifact_scan = _step(text, "Scan complete local publication inputs")
    accepted_upload = _step(text, "Upload accepted publication inputs")
    upload = _step(text, "Upload Compose artifact")

    assert "skopeo copy --all" in publish
    assert publish.count("oci-archive:") == 2
    assert ":${IMMUTABLE_TAG}" in publish
    assert "digestfile" in publish
    assert "skopeo manifest-digest" in publish
    assert "refusing to overwrite immutable" in publish
    assert "manifest unknown|name unknown|not found" in publish
    assert "docker buildx imagetools inspect" in verify
    assert ".Provenance" in verify and ".SBOM" in verify
    assert "slsa.dev/provenance" in verify
    assert ".SLSA?.buildType?" in verify
    assert "SPDXRef-DOCUMENT" in verify
    for role in ("API", "worker"):
        attestation = _step(text, f"Sign accepted {role} image provenance")
        assert "uses: actions/attest@" in attestation
        assert "subject-name:" in attestation
        assert "subject-digest:" in attestation
        assert "push-to-registry: true" in attestation
    assert "scripts/render-dev-compose" in render
    pinned_render, mutable_render = render.split(
        "scripts/render-dev-compose", maxsplit=2
    )[1:]
    assert "vonk-forge-api:dev-sha-$GITHUB_SHA@$api_digest" in pinned_render
    assert "vonk-forge-worker:dev-sha-$GITHUB_SHA@$worker_digest" in pinned_render
    assert '--commit "$GITHUB_SHA"' in pinned_render
    assert 'vonk-forge-api:dev"' not in pinned_render
    assert 'vonk-forge-worker:dev"' not in pinned_render
    assert "ghcr.io/carstvaartjes/vonk-forge-api:dev" in mutable_render
    assert "ghcr.io/carstvaartjes/vonk-forge-worker:dev" in mutable_render
    assert "@$api_digest" not in mutable_render
    assert "@$worker_digest" not in mutable_render
    assert "--commit" not in mutable_render
    assert "--channel dev" in mutable_render
    assert render.count('docker compose -f "$publication_root/docker-compose.') == 2
    assert "docker history --no-trunc" in artifact_scan
    assert "docker image inspect" in artifact_scan
    assert "attestation-manifest" in artifact_scan
    assert "https://slsa.dev/provenance/" in artifact_scan
    assert "https://spdx.dev/Document" in artifact_scan
    assert "scripts/verify-dev-image-secrets" in artifact_scan
    assert '--scan-path "$publication_root"' in artifact_scan
    assert '--scan-path "$evidence_root"' in artifact_scan
    assert "--forbid-bytes-dir" in artifact_scan
    assert text.index("Render and validate disposable development Compose artifacts") < text.index(
        "Scan complete local publication inputs"
    )
    assert text.index("Scan complete local publication inputs") < text.index(
        "Upload accepted publication inputs"
    )
    assert "accepted/docker-compose.pinned.yml" in accepted_upload
    assert "accepted/docker-compose.dev.yml" in accepted_upload
    assert "accepted/docker-compose.pinned.yml" in upload
    assert "accepted/docker-compose.dev.yml" in upload
    assert "if-no-files-found: error" in upload
    assert "secrets/" not in upload


def test_dev_alias_is_the_last_mutation_and_latest_is_never_published() -> None:
    text = _workflow()
    alias = _step(text, "Advance accepted development aliases")
    alias_helper = ALIAS_SCRIPT.read_text(encoding="utf-8")

    assert text.index("Upload Compose artifact") < text.index(
        "Advance accepted development aliases"
    )
    assert "scripts/promote-image-aliases" in alias
    assert '"$API_IMAGE" "$API_DIGEST"' in alias
    assert '"$WORKER_IMAGE" "$WORKER_DIGEST" "$DEV_ALIAS"' in alias
    assert "skopeo inspect" in alias_helper
    assert "finish" in alias_helper
    assert "set_alias" in alias_helper
    assert "for attempt in 1 2 3" in alias_helper
    assert "previous_digests" in alias_helper
    assert "trap" in alias_helper
    assert "rerun the failed publication job" in alias_helper
    assert "cancel-in-progress: false" in text
    assert text.count("refs/remotes/origin/main") >= 2
    assert text.index("Recheck exact main before publication") < text.index(
        "Log in to GHCR"
    )
    assert ":latest" not in text
    assert "latest=" not in text
    render = _step(text, "Render and validate disposable development Compose artifacts")
    assert "$DEV_ALIAS" not in render
    publisher = _job(text, "publish-development-images")
    assert publisher.rstrip().endswith('"$WORKER_IMAGE" "$WORKER_DIGEST" "$DEV_ALIAS"')


def test_every_external_action_is_pinned_to_an_exact_commit() -> None:
    for line in _workflow().splitlines():
        stripped = line.strip()
        if stripped.startswith("uses:"):
            reference = stripped.split("uses:", 1)[1].strip().split()[0]
            assert "@" in reference
            revision = reference.rsplit("@", 1)[1]
            assert len(revision) == 40
            assert all(character in "0123456789abcdef" for character in revision)
