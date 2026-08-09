from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-image-metadata"
ALIAS_SCRIPT = ROOT / "scripts/promote-image-aliases"
WORKFLOW = ROOT / ".github/workflows/dev-images.yml"
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
        "role='api' if 'vonk-forge-api' in ref else 'worker'\n"
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
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            str(ALIAS_SCRIPT),
            "ghcr.io/carstvaartjes/vonk-forge-api",
            DIGEST_A,
            "ghcr.io/carstvaartjes/vonk-forge-worker",
            DIGEST_B,
            alias,
        ),
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

    result = _promote_aliases(fake_bin, state, log, alias="latest")

    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "api": DIGEST_A,
        "worker": DIGEST_B,
    }
    assert all(":latest" in line for line in log.read_text().splitlines())


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
    assert "not advanced as a pair" in result.stderr


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


def test_workflow_is_main_only_publication_without_repository_secrets() -> None:
    text = _workflow()
    validator = _job(text, "build-and-accept")
    publisher = _job(text, "publish-development-images")

    assert "branches: [main]" in text
    assert "workflow_dispatch:" in text
    assert "packages: write" in text
    assert "packages: write" not in validator
    assert "permissions:\n      contents: read\n      packages: write" in publisher
    assert text.count("packages: write") == 1
    assert "contents: read" in text
    assert "environment:" not in text
    assert "id-token: write" not in text
    assert "attestations: write" not in text
    assert "secrets.GITHUB_TOKEN" in _step(text, "Log in to GHCR")
    assert text.count("${{ secrets.") == 1
    assert "refs/remotes/origin/main" in _step(text, "Verify exact main tip")
    assert '"$GITHUB_REF" "$GITHUB_SHA"' in _step(text, "Verify exact main tip")


def test_workflow_builds_scans_and_accepts_oci_archives_before_login() -> None:
    text = _workflow()
    build = _step(text, "Build exact OCI archives")
    load = _step(text, "Load tested images without pulling")
    accept = _step(text, "Scan and accept image-only stack")

    assert "docker buildx build" in build
    assert "--platform linux/amd64" in build
    assert "--target api" in build and "--target worker" in build
    assert "type=oci" in build
    assert "--sbom=true" in build
    assert "--provenance=mode=max" in build
    assert "--build-arg" not in build
    assert "--secret" not in build
    assert "skopeo copy" in load
    assert "oci-archive:" in load
    assert "docker-daemon:vonk-forge-api:dev-local" in load
    assert "docker-daemon:vonk-forge-worker:dev-local" in load
    assert "scripts/verify-dev-image-secrets" in accept
    assert "scripts/dev-image-acceptance" in accept
    assert text.index("Scan and accept image-only stack") < text.index("Log in to GHCR")
    assert "Upload accepted OCI archives" in text
    assert "needs: [build-and-accept]" in text
    assert "Download accepted OCI archives" in text


def test_workflow_publishes_tested_archives_then_renders_immutable_compose() -> None:
    text = _workflow()
    publish = _step(text, "Publish immutable tested images")
    verify = _step(text, "Verify immutable manifests and attestations")
    render = _step(text, "Render digest-pinned Compose artifact")
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
    assert "scripts/render-dev-compose" in render
    assert ":${IMMUTABLE_TAG}@${API_DIGEST}" in render
    assert ":${IMMUTABLE_TAG}@${WORKER_DIGEST}" in render
    assert "path: dist/docker-compose.yml" in upload
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
    assert "previous_api_digest" in alias_helper
    assert "previous_worker_digest" in alias_helper
    assert "trap" in alias_helper
    assert "rerun the failed publication job" in alias_helper
    assert "cancel-in-progress: false" in text
    assert text.count("refs/remotes/origin/main") >= 2
    assert text.index("Recheck exact main before publication") < text.index(
        "Log in to GHCR"
    )
    assert ":latest" not in text
    assert "latest=" not in text
    render = _step(text, "Render digest-pinned Compose artifact")
    assert "$DEV_ALIAS" not in render


def test_every_external_action_is_pinned_to_an_exact_commit() -> None:
    for line in _workflow().splitlines():
        stripped = line.strip()
        if stripped.startswith("uses:"):
            reference = stripped.split("uses:", 1)[1].strip().split()[0]
            assert "@" in reference
            revision = reference.rsplit("@", 1)[1]
            assert len(revision) == 40
            assert all(character in "0123456789abcdef" for character in revision)
