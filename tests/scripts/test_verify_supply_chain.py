import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-supply-chain"


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    for path in (
        ".github/workflows/ci.yml",
        ".github/workflows/agent-release.yml",
        ".github/actions/agent-package-build/action.yml",
        ".github/actions/agent-package-compile/action.yml",
        ".github/actions/agent-package-security/action.yml",
        ".github/actions/agent-apt-publish/action.yml",
        ".github/workflows/dev-images.yml",
        ".github/workflows/installer-publication.yml",
        ".github/workflows/installer-setups.yml",
        ".github/workflows/workload-artifacts.yml",
        ".github/release-allowed-signers",
        ".github/dependabot.yml",
        "Cargo.toml",
        "Cargo.lock",
        "pyproject.toml",
        "schemas/install-release-manifest.schema.json",
        "schemas/workload-artifact-build.schema.json",
        "schemas/global/catalog-entity-v1.schema.json",
        "schemas/global/recipe-v1.schema.json",
        "schemas/global/harness-evidence-v1.schema.json",
        "agent_protocol/pyproject.toml",
        ".dockerignore",
        "agent_protocol/uv.lock",
        "control/pyproject.toml",
        "control/uv.lock",
        "control/src/vonk_control/catalog_contract.py",
        "control/src/vonk_control/recipe_contract.py",
        "control/src/vonk_control/catalog_entities.py",
        "control/src/vonk_control/catalog_service.py",
        "control/src/vonk_control/catalog_api.py",
        "control/src/vonk_control/auth.py",
        "control/src/vonk_control/library_contract.py",
        "control/src/vonk_control/recipe_routes.py",
        "control/src/vonk_control/models.py",
        "control/migrations/versions/0001_fleet_library_baseline.py",
        "control/migrations/versions/0002_fleet_node_profile_events.py",
        "control/migrations/versions/0003_agent_reenrollment_grants.py",
        "control/web/package-lock.json",
        "control/Dockerfile",
        "deploy/compose/compose.yaml",
        "deploy/compose/images.lock.json",
        "deploy/compose/Caddyfile",
        "deploy/compose/caddy/entrypoint.sh",
        "deploy/compose/postgres/entrypoint.sh",
        "deploy/compose/postgres/init-databases.sh",
        "deploy/compose/grafana/dashboards/fleet.json",
        "deploy/compose/grafana/dashboards/jobs.json",
        "deploy/compose/grafana/provisioning/dashboards/default.yaml",
        "deploy/compose/grafana/provisioning/datasources/prometheus.yaml",
        "deploy/compose/tailscale/compose.yaml",
        "deploy/compose/tailscale/configure.sh",
        "deploy/compose/tailscale/grants.example.hujson",
        "deploy/compose/hermes-agent/compose.yaml",
        "deploy/compose/hermes-agent/Dockerfile",
        "deploy/compose/hermes-agent/entrypoint.sh",
        "deploy/compose/litellm/config.yaml",
        "deploy/compose/litellm/config_supervisor.py",
        "deploy/compose/litellm/entrypoint.sh",
        "deploy/compose/litellm/bootstrap-config.json",
        "deploy/compose/prometheus/alerts.yaml",
        "deploy/compose/prometheus/prometheus.yml",
        "deploy/compose/registry/config.yml",
        "deploy/compose/step-ca/ca.json",
        "deploy/compose/trust/litellm-cosign.pub",
        "scripts/build-agent-deb",
        "scripts/materialize-agent-tools",
        "scripts/build-agent-package-evidence",
        "scripts/container-release-metadata",
        "scripts/accept-development-image-archive",
        "scripts/agent-package-metadata",
        "scripts/agent-apt-metadata",
        "scripts/agent-apt-state",
        "scripts/dev-image-acceptance-receipt",
        "scripts/dev-image-metadata",
        "scripts/dev-image-publication-capsule",
        "scripts/publish-immutable-image",
        "scripts/promote-image-aliases",
        "scripts/verify-release-tag-authority",
        "scripts/render-dev-compose",
        "scripts/render-install-bootstrap",
        "install/channel",
        "scripts/render-production-compose",
        "scripts/install-release-publication",
        "scripts/refuse-existing-image-version",
        "scripts/validate-container-release-digests",
        "scripts/verify-multiarch-image-manifest",
        "scripts/verify-public-image-inputs",
        "scripts/verify-published-image",
        "scripts/verify-agent-deb",
        "scripts/verify-agent-binaries",
        "scripts/verify-agent-systemd",
        "scripts/verify-supply-chain",
        "scripts/accept-recipe",
        "scripts/run-development-slices",
        "scripts/qualify-recipe",
        "scripts/import-recipe-library",
        "scripts/validate-recipe-library",
        "scripts/workload-artifact-metadata",
        "config/recipe-library-manifest.json",
    ):
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, destination)
    for directory in (
        "config/model-groups",
        "config/models",
        "config/model-versions",
        "config/execution-harnesses",
        "config/runtime-distributions",
        "config/patch-bundles",
        "config/recipes",
        "config/model-targets",
    ):
        shutil.copytree(ROOT / directory, target / directory)
    shutil.copytree(
        ROOT / "agent_protocol/src",
        target / "agent_protocol/src",
    )
    shutil.copytree(ROOT / "rust", target / "rust")
    shutil.copytree(ROOT / "packaging", target / "packaging")
    subprocess.run(
        [SCRIPT, "--root", target, "--generate", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return target


def _rewrite_installed_protocol_wheel(dockerfile: Path, replacement: str) -> None:
    """Mutate the wheel argument in the pip step, independent of other inputs."""

    wheel = "/wheels/vonk_agent_protocol-2.1.0-py3-none-any.whl"
    lines = dockerfile.read_text().splitlines(keepends=True)
    candidates = [
        index
        for index, line in enumerate(lines)
        if line.lstrip().startswith(wheel) and wheel in line
    ]
    assert len(candidates) == 1
    index = candidates[0]
    lines[index] = lines[index].replace(wheel, replacement, 1)
    dockerfile.write_text("".join(lines))


def test_verifier_accepts_locked_offline_evidence(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    result = subprocess.run(
        [SCRIPT, "--root", repository, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert '"ok":true' in result.stdout
    assert "inventory/sbom/agent-protocol.spdx.json" in result.stdout
    assert "inventory/sbom/agent-rust.spdx.json" in result.stdout
    assert "inventory/sbom/agent-python.spdx.json" not in result.stdout
    assert not (repository / "inventory/sbom/agent-python.spdx.json").exists()


def test_verifier_accepts_write_manifest_alias(tmp_path: Path) -> None:
    repository = _copy(tmp_path)

    result = subprocess.run(
        [SCRIPT, "--root", repository, "--write-manifest", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert '"ok":true' in result.stdout


@pytest.mark.parametrize(
    "path",
    (
        ".github/workflows/installer-publication.yml",
        ".github/workflows/installer-setups.yml",
        "schemas/install-release-manifest.schema.json",
        "install/channel",
        "scripts/install-release-publication",
        "scripts/render-install-bootstrap",
    ),
)
def test_supply_chain_manifest_binds_installer_publication_contract(
    tmp_path: Path,
    path: str,
) -> None:
    repository = _copy(tmp_path)
    candidate = repository / path
    candidate.write_bytes(candidate.read_bytes() + b"\n# publication drift\n")

    result = subprocess.run(
        [SCRIPT, "--root", repository, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "manifest" in " ".join(json.loads(result.stdout)["errors"]).lower()


@pytest.mark.parametrize(
    "path",
    (
        ".github/workflows/workload-artifacts.yml",
        "schemas/workload-artifact-build.schema.json",
        "scripts/workload-artifact-metadata",
    ),
)
def test_supply_chain_manifest_binds_workload_artifact_publication_contract(
    tmp_path: Path,
    path: str,
) -> None:
    repository = _copy(tmp_path)
    for required in (
        ".github/workflows/workload-artifacts.yml",
        "schemas/workload-artifact-build.schema.json",
        "scripts/workload-artifact-metadata",
    ):
        candidate = repository / required
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(f"baseline:{required}\n")
    subprocess.run(
        [SCRIPT, "--root", repository, "--generate", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    candidate = repository / path
    candidate.write_bytes(candidate.read_bytes() + b"drift\n")

    result = subprocess.run(
        [SCRIPT, "--root", repository, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "manifest" in " ".join(json.loads(result.stdout)["errors"]).lower()


@pytest.mark.parametrize(
    "path",
    (
        ".github/workflows/agent-release.yml",
        ".github/actions/agent-package-build/action.yml",
        ".github/actions/agent-package-compile/action.yml",
        ".github/actions/agent-package-security/action.yml",
        ".github/actions/agent-apt-publish/action.yml",
        "scripts/agent-package-metadata",
        "scripts/agent-apt-metadata",
        "scripts/agent-apt-state",
        "scripts/verify-agent-binaries",
    ),
)
def test_supply_chain_manifest_binds_agent_package_channel_authority(
    tmp_path: Path,
    path: str,
) -> None:
    repository = _copy(tmp_path)
    manifest = json.loads((repository / "inventory/sbom/manifest.json").read_bytes())
    assert path in manifest["inputs"]

    candidate = repository / path
    candidate.write_bytes(candidate.read_bytes() + b"\n# authority drift\n")
    result = subprocess.run(
        [SCRIPT, "--root", repository, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "manifest" in " ".join(json.loads(result.stdout)["errors"]).lower()


def test_supply_chain_manifest_binds_v1_recipe_catalog_authority(
    tmp_path: Path,
) -> None:
    repository = _copy(tmp_path)
    manifest = json.loads((repository / "inventory/sbom/manifest.json").read_bytes())

    for path in (
        "schemas/global/catalog-entity-v1.schema.json",
        "schemas/global/recipe-v1.schema.json",
        "control/src/vonk_control/catalog_contract.py",
        "control/src/vonk_control/recipe_contract.py",
        "control/src/vonk_control/catalog_api.py",
        "control/src/vonk_control/auth.py",
        "control/src/vonk_control/recipe_routes.py",
        "control/src/vonk_control/models.py",
        "control/migrations/versions/0001_fleet_library_baseline.py",
        "control/migrations/versions/0002_fleet_node_profile_events.py",
        "control/migrations/versions/0003_agent_reenrollment_grants.py",
        "scripts/accept-recipe",
        "scripts/run-development-slices",
        "scripts/qualify-recipe",
        "scripts/import-recipe-library",
        "config/recipe-library-manifest.json",
    ):
        assert path in manifest["inputs"]


@pytest.mark.parametrize(
    "path",
    (
        "control/src/vonk_control/catalog_api.py",
        "control/src/vonk_control/auth.py",
        "control/src/vonk_control/recipe_routes.py",
        "control/src/vonk_control/models.py",
        "control/migrations/versions/0001_fleet_library_baseline.py",
        "control/migrations/versions/0002_fleet_node_profile_events.py",
        "control/migrations/versions/0003_agent_reenrollment_grants.py",
    ),
)
def test_supply_chain_manifest_binds_recipe_authority_edges(
    tmp_path: Path, path: str
) -> None:
    repository = _copy(tmp_path)
    candidate = repository / path
    candidate.write_bytes(candidate.read_bytes() + b"\n# recipe authority drift\n")

    result = subprocess.run(
        [SCRIPT, "--root", repository, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "manifest" in " ".join(json.loads(result.stdout)["errors"]).lower()


def test_verifier_does_not_require_cluster_profiles_to_be_installed(
    tmp_path: Path,
) -> None:
    repository = _copy(tmp_path)

    result = subprocess.run(
        ["/usr/bin/python3", SCRIPT, "--root", repository, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_verifier_rejects_floating_image(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    compose = repository / "deploy/compose/compose.yaml"
    text = compose.read_text()
    locked = "caddy:2.11.4@sha256:844f60b64e4724a5aa8245e019dace0d3f199f7433ce6c57676cb30a920dbad9"
    compose.write_text(text.replace(locked, "caddy:latest"))
    result = subprocess.run(
        [SCRIPT, "--root", repository], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "digest" in result.stderr or "floating" in result.stderr


def test_verifier_rejects_floating_hermes_agent_base(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    dockerfile = repository / "deploy/compose/hermes-agent/Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace(
            "nousresearch/hermes-agent:v2026.7.20@sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a",
            "nousresearch/hermes-agent:v2026.7.20",
        )
    )

    result = subprocess.run(
        [SCRIPT, "--root", repository, "--generate"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "Hermes" in result.stderr and "digest" in result.stderr


@pytest.mark.parametrize("name", ("node", "python", "hermes"))
@pytest.mark.parametrize("malformed", (False, True))
def test_verifier_reports_missing_or_malformed_build_bases_as_json(
    tmp_path: Path, name: str, malformed: bool
) -> None:
    repository = _copy(tmp_path)
    lock_path = repository / "deploy/compose/images.lock.json"
    lock = json.loads(lock_path.read_text())
    if malformed:
        lock["build_bases"][name] = None
    else:
        del lock["build_bases"][name]
    lock_path.write_text(json.dumps(lock))

    result = subprocess.run(
        [SCRIPT, "--root", repository, "--json"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert f"{name} digest-pinned build base is missing or invalid" in payload["errors"]


@pytest.mark.parametrize("value", (None, [], {}))
def test_verifier_reports_non_string_runtime_images_as_json(
    tmp_path: Path, value: object
) -> None:
    repository = _copy(tmp_path)
    lock_path = repository / "deploy/compose/images.lock.json"
    lock = json.loads(lock_path.read_text())
    lock["images"]["caddy"] = value
    lock_path.write_text(json.dumps(lock))

    result = subprocess.run(
        [SCRIPT, "--root", repository, "--json"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "caddy image is not pinned by digest" in payload["errors"]


def test_image_lock_contains_the_pinned_hermes_build_base() -> None:
    lock = json.loads((ROOT / "deploy/compose/images.lock.json").read_text())

    assert lock["build_bases"]["hermes"] == (
        "nousresearch/hermes-agent:v2026.7.20@sha256:"
        "f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a"
    )
    assert "hermes-agent" not in lock["images"]
    assert not any("ai-devbox" in name for name in lock["build_bases"])


def test_image_lock_declares_all_three_release_artifacts() -> None:
    lock = json.loads((ROOT / "deploy/compose/images.lock.json").read_text())

    assert lock["release_images"] == [
        {
            "context": ".",
            "dockerfile": "control/Dockerfile",
            "environment": "CONTROL_API_IMAGE",
            "package": "vonk-forge-api",
            "required": True,
            "target": "api",
        },
        {
            "context": ".",
            "dockerfile": "control/Dockerfile",
            "environment": "CONTROL_WORKER_IMAGE",
            "package": "vonk-forge-worker",
            "required": True,
            "target": "worker",
        },
        {
            "context": "deploy/compose/hermes-agent",
            "dockerfile": "deploy/compose/hermes-agent/Dockerfile",
            "environment": "HERMES_AGENT_IMAGE",
            "package": "vonk-forge-hermes",
            "required": True,
            "target": "managed",
        },
    ]


def test_verifier_accepts_opt_in_hermes_compose_profile() -> None:
    result = subprocess.run(
        [SCRIPT, "--root", ROOT, "--generate", "--json"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_verifier_rejects_stale_sbom_after_lock_change(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    lock = repository / "control/web/package-lock.json"
    lock.write_text(lock.read_text() + "\n")
    result = subprocess.run(
        [SCRIPT, "--root", repository], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "SBOM" in result.stderr or "manifest" in result.stderr


def test_verifier_rejects_protocol_wheel_or_lock_drift(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    source = repository / "agent_protocol/src/vonk_agent_protocol/contracts.py"
    source.write_text(source.read_text() + "\n# package drift\n")

    result = subprocess.run(
        [SCRIPT, "--root", repository], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "wheel" in result.stderr


def test_verifier_rejects_a_missing_protocol_wheel_artifact(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    wheel = repository / "inventory/wheels/vonk_agent_protocol-2.1.0-py3-none-any.whl"
    assert wheel.is_file()
    wheel.unlink()

    result = subprocess.run(
        [SCRIPT, "--root", repository], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "wheel" in result.stderr


def test_verifier_rejects_a_byte_different_protocol_wheel_with_the_same_name_and_version(
    tmp_path: Path,
) -> None:
    repository = _copy(tmp_path)
    wheel = repository / "inventory/wheels/vonk_agent_protocol-2.1.0-py3-none-any.whl"
    wheel.write_bytes(wheel.read_bytes() + b"different bytes")

    result = subprocess.run(
        [SCRIPT, "--root", repository], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "wheel" in result.stderr


def test_protocol_spdx_records_the_verified_wheel_checksum(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    wheel = repository / "inventory/wheels/vonk_agent_protocol-2.1.0-py3-none-any.whl"
    document = json.loads(
        (repository / "inventory/sbom/agent-protocol.spdx.json").read_text()
    )
    protocol = next(
        package
        for package in document["packages"]
        if package["name"] == "vonk-agent-protocol"
    )

    checksum = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert protocol["checksums"] == [{"algorithm": "SHA256", "checksumValue": checksum}]
    wheel_file = next(
        file
        for file in document["files"]
        if file["fileName"]
        == "inventory/wheels/vonk_agent_protocol-2.1.0-py3-none-any.whl"
    )
    assert wheel_file["checksums"] == [
        {"algorithm": "SHA256", "checksumValue": checksum}
    ]
    assert {
        "spdxElementId": protocol["SPDXID"],
        "relationshipType": "GENERATED_FROM",
        "relatedSpdxElement": wheel_file["SPDXID"],
    } in document["relationships"]


def test_verifier_rejects_a_root_dockerignore_change(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    dockerignore = repository / ".dockerignore"
    dockerignore.write_text(dockerignore.read_text() + "\n!control/src/.env\n")

    result = subprocess.run(
        [SCRIPT, "--root", repository], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "manifest" in result.stderr


def test_verifier_rejects_a_protocol_lock_hash_that_does_not_match_the_wheel(
    tmp_path: Path,
) -> None:
    repository = _copy(tmp_path)
    lock = repository / "control/uv.lock"
    wheel = repository / "inventory/wheels/vonk_agent_protocol-2.1.0-py3-none-any.whl"
    wheel_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lock.write_text(lock.read_text().replace(wheel_hash, "0" * 64))

    result = subprocess.run(
        [SCRIPT, "--root", repository, "--generate"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "wheel" in result.stderr


def test_verifier_rejects_a_dockerfile_that_copies_but_does_not_install_the_protocol_wheel(
    tmp_path: Path,
) -> None:
    repository = _copy(tmp_path)
    dockerfile = repository / "control/Dockerfile"
    _rewrite_installed_protocol_wheel(dockerfile, "")

    result = subprocess.run(
        [SCRIPT, "--root", repository, "--generate"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "install" in result.stderr


def test_verifier_accepts_exact_wheel_in_second_pip_install_command(
    tmp_path: Path,
) -> None:
    repository = _copy(tmp_path)
    dockerfile = repository / "control/Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace(
            "RUN python -m pip install --no-cache-dir --prefix=/install \\\n",
            "RUN python -m pip install --disable-pip-version-check setuptools && \\\n"
            "    python -m pip install --no-cache-dir --prefix=/install \\\n",
        )
    )

    result = subprocess.run(
        [SCRIPT, "--root", repository, "--generate"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("operator", ("&&", ";", "||", "|"))
def test_verifier_rejects_a_protocol_wheel_mentioned_only_after_a_shell_operator(
    tmp_path: Path, operator: str
) -> None:
    repository = _copy(tmp_path)
    dockerfile = repository / "control/Dockerfile"
    wheel = "/wheels/vonk_agent_protocol-2.1.0-py3-none-any.whl"
    _rewrite_installed_protocol_wheel(
        dockerfile,
        f"/vonk-cluster-profiles . {operator} test -f {wheel} #",
    )

    result = subprocess.run(
        [SCRIPT, "--root", repository, "--generate"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "install" in result.stderr
