from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/render-dev-compose"
TEMPLATE = ROOT / "deploy/compose/compose.dev.images.yaml"
COMMIT = "a" * 40
DIGEST = "b" * 64
API_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-{COMMIT}@sha256:{DIGEST}"
WORKER_IMAGE = (
    f"ghcr.io/carstvaartjes/vonk-forge-worker:dev-sha-{COMMIT}@sha256:{DIGEST}"
)
API_DEV_IMAGE = "ghcr.io/carstvaartjes/vonk-forge-api:dev"
WORKER_DEV_IMAGE = "ghcr.io/carstvaartjes/vonk-forge-worker:dev"
PINNED_BASELINE = f'x-pinned-expected-commit: "{COMMIT}"'
EXPECTED_SECRET_NAMES = {
    "admin-password-verifier",
    "agent-ca-certificate",
    "agent-ca-key",
    "agent-proxy-auth",
    "controller-ca",
    "controller-server-certificate",
    "controller-server-key",
    "database-url",
    "git-signing-key",
    "host-runtime-grant-private-key",
    "litellm-database-password",
    "litellm-master-key",
    "litellm-upstream-key",
    "management-cidrs",
    "postgres-password",
    "token-signing-key",
    "tailscale-oauth-client-id",
    "tailscale-oauth-client-secret",
}
PORT_INTERPOLATIONS = {
    "VONK_AGENT_PORT": "${VONK_AGENT_PORT:-8443}",
    "VONK_DEV_INFERENCE_PORT": "${VONK_DEV_INFERENCE_PORT:-4000}",
    "VONK_DEV_PORT": "${VONK_DEV_PORT:-8080}",
}


def _renderer():
    loader = importlib.machinery.SourceFileLoader("render_dev_compose", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _rendered_pinned(tmp_path: Path) -> tuple[object, Path]:
    output = tmp_path / "docker-compose.yml"
    renderer = _renderer()
    renderer.render(TEMPLATE, output, API_IMAGE, WORKER_IMAGE, COMMIT)
    return renderer, output


def _rendered_dev(tmp_path: Path) -> tuple[object, Path]:
    output = tmp_path / "docker-compose.dev.yml"
    renderer = _renderer()
    renderer.render(
        TEMPLATE,
        output,
        API_DEV_IMAGE,
        WORKER_DEV_IMAGE,
        channel="dev",
    )
    return renderer, output


def _copy_template(tmp_path: Path, text: str) -> Path:
    template = tmp_path / "template.yaml"
    template.write_text(text, encoding="utf-8")
    return template


def test_pinned_render_uses_one_verified_cohort_identity_for_every_service(
    tmp_path: Path,
) -> None:
    _, output = _rendered_pinned(tmp_path)

    text = output.read_text(encoding="utf-8")
    assert not text.startswith("name:")
    assert text.count(API_IMAGE) == 1
    assert text.count(WORKER_IMAGE) == 1
    assert PINNED_BASELINE in text
    assert "__VONK_EXPECTED_COMMIT__" not in text
    assert "VONK_DEV_EXPECTED_COMMIT" not in text
    for service in (
        "dev-bootstrap",
        "migrate",
        "control-api",
        "control-worker",
    ):
        environment = _compose_service(output, service)["environment"]
        assert environment["VONK_DEV_SELECTED_COHORT_FILE"] == "/cohort/selected.json"
    assert "__VONK_" not in text


def test_dev_render_is_bare_mutable_and_removes_pinned_compatibility_input(
    tmp_path: Path,
) -> None:
    _, output = _rendered_dev(tmp_path)

    text = output.read_text(encoding="utf-8")
    assert not text.startswith("name:")
    assert text.count(API_DEV_IMAGE) == 1
    assert text.count(WORKER_DEV_IMAGE) == 1
    assert "@sha256:" not in "\n".join(
        line for line in text.splitlines() if "vonk-forge-" in line
    )
    assert COMMIT not in text
    assert "VONK_DEV_EXPECTED_COMMIT" not in text
    assert "x-pinned-expected-commit" not in text
    assert "Compatibility input for the current pinned renderer" not in text
    assert "__VONK_" not in text


def test_litellm_loopback_port_terminates_at_caddy_lease_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VONK_DEV_INFERENCE_PORT", "45678")
    _, output = _rendered_dev(tmp_path)

    litellm = _compose_service(output, "litellm")
    caddy = _compose_service(output, "caddy")
    assert set(litellm["networks"]) == {
        "cluster-egress",
        "litellm-data",
        "litellm-edge",
    }
    assert litellm.get("ports") in (None, [])
    assert caddy["ports"] == [
        {
            "mode": "ingress",
            "target": 8443,
            "published": "8443",
            "protocol": "tcp",
        },
        {
            "mode": "ingress",
            "target": 8081,
            "published": "45678",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        },
    ]
    assert "litellm-edge" in caddy["networks"]


def _compose_service(output: Path, service: str) -> dict[str, object]:
    rendered = json.loads(
        subprocess.run(
            ("docker", "compose", "-f", str(output), "config", "--format", "json"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    return rendered["services"][service]


def test_dev_render_forbids_a_commit_input(tmp_path: Path) -> None:
    output = tmp_path / "docker-compose.dev.yml"

    with pytest.raises(ValueError, match="forbidden"):
        _renderer().render(
            TEMPLATE,
            output,
            API_DEV_IMAGE,
            WORKER_DEV_IMAGE,
            COMMIT,
            channel="dev",
        )

    assert not output.exists()


@pytest.mark.parametrize(
    "api_image",
    (
        f"ghcr.io/carstvaartjes/vonk-forge-api:dev@sha256:{DIGEST}",
        "ghcr.io/carstvaartjes/vonk-forge-api:latest",
        "ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-" + COMMIT,
        "ghcr.io/carstvaartjes/vonk-forge-api:v1.2.3",
        "ghcr.io/example/vonk-forge-api:dev",
        "${CALLER_IMAGE:-ghcr.io/carstvaartjes/vonk-forge-api:dev}",
    ),
)
def test_dev_render_rejects_every_non_bare_public_dev_reference(
    tmp_path: Path, api_image: str
) -> None:
    output = tmp_path / "docker-compose.dev.yml"

    with pytest.raises(ValueError):
        _renderer().render(
            TEMPLATE,
            output,
            api_image,
            WORKER_DEV_IMAGE,
            channel="dev",
        )

    assert not output.exists()


@pytest.mark.parametrize(
    "token",
    (
        "__VONK_API_IMAGE__",
        "__VONK_WORKER_IMAGE__",
        "__VONK_EXPECTED_COMMIT__",
    ),
)
def test_render_rejects_duplicate_renderer_tokens_before_staging(
    tmp_path: Path, token: str
) -> None:
    template = _copy_template(
        tmp_path,
        TEMPLATE.read_text(encoding="utf-8") + f"\nx-duplicate: {token}\n",
    )
    output = tmp_path / "docker-compose.yml"
    output.write_text("previous output\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly once"):
        _renderer().render(template, output, API_IMAGE, WORKER_IMAGE, COMMIT)

    assert output.read_text(encoding="utf-8") == "previous output\n"
    assert list(tmp_path.glob(".render-dev-compose-*")) == []


@pytest.mark.parametrize(
    "expression", ("${CALLER_VALUE:-resolved-by-compose}", "$CALLER_VALUE")
)
def test_render_rejects_extra_compose_interpolation_even_when_caller_resolves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expression: str
) -> None:
    template = _copy_template(
        tmp_path,
        TEMPLATE.read_text(encoding="utf-8") + f'\nx-extra: "{expression}"\n',
    )
    output = tmp_path / "docker-compose.yml"
    output.write_text("previous output\n", encoding="utf-8")
    monkeypatch.setenv("CALLER_VALUE", "caller-controlled")

    with pytest.raises(ValueError, match="interpolation"):
        _renderer().render(template, output, API_IMAGE, WORKER_IMAGE, COMMIT)

    assert output.read_text(encoding="utf-8") == "previous output\n"
    assert list(tmp_path.glob(".render-dev-compose-*")) == []


@pytest.mark.parametrize(
    ("original", "replacement", "name"),
    (
        ("${VONK_DEV_PORT:-8080}", "${VONK_DEV_PORT-8080}", "VONK_DEV_PORT"),
        ("${VONK_DEV_PORT:-8080}", "${VONK_DEV_PORT:-8080", "VONK_DEV_PORT"),
        (
            "${VONK_DEV_PORT:-8080}",
            "${VONK_DEV_PORT:-8080}:${VONK_DEV_PORT:-8080}",
            "VONK_DEV_PORT",
        ),
        (
            "${VONK_AGENT_PORT:-8443}",
            "${VONK_AGENT_PORT-8443}",
            "VONK_AGENT_PORT",
        ),
        (
            "${VONK_AGENT_PORT:-8443}",
            "${VONK_AGENT_PORT:-8443",
            "VONK_AGENT_PORT",
        ),
        (
            "${VONK_AGENT_PORT:-8443}",
            "${VONK_AGENT_PORT:-8443}:${VONK_AGENT_PORT:-8443}",
            "VONK_AGENT_PORT",
        ),
    ),
)
def test_render_rejects_malformed_or_duplicate_documented_port_interpolation(
    tmp_path: Path, original: str, replacement: str, name: str
) -> None:
    template = _copy_template(
        tmp_path,
        TEMPLATE.read_text(encoding="utf-8").replace(original, replacement),
    )
    output = tmp_path / "docker-compose.yml"
    output.write_text("previous output\n", encoding="utf-8")

    with pytest.raises(ValueError, match=name):
        _renderer().render(template, output, API_IMAGE, WORKER_IMAGE, COMMIT)

    assert output.read_text(encoding="utf-8") == "previous output\n"
    assert list(tmp_path.glob(".render-dev-compose-*")) == []


@pytest.mark.parametrize(
    ("api_image", "worker_image", "commit"),
    [
        (
            "ghcr.io/carstvaartjes/vonk-forge-api:latest@sha256:" + DIGEST,
            WORKER_IMAGE,
            COMMIT,
        ),
        (
            "ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-" + COMMIT,
            WORKER_IMAGE,
            COMMIT,
        ),
        (
            "ghcr.io/carstvaartjes/vonk-forge-api:v1.2.3@sha256:" + DIGEST,
            WORKER_IMAGE,
            COMMIT,
        ),
        (
            "ghcr.io/example/vonk-forge-api:dev-sha-" + COMMIT + "@sha256:" + DIGEST,
            WORKER_IMAGE,
            COMMIT,
        ),
        (API_IMAGE.replace(COMMIT, "c" * 40), WORKER_IMAGE, COMMIT),
        (API_IMAGE, WORKER_IMAGE, "A" * 40),
    ],
)
def test_render_rejects_invalid_inputs_without_replacing_output(
    tmp_path: Path, api_image: str, worker_image: str, commit: str
) -> None:
    output = tmp_path / "docker-compose.yml"
    output.write_text("previous output\n", encoding="utf-8")

    with pytest.raises(ValueError):
        _renderer().render(TEMPLATE, output, api_image, worker_image, commit)

    assert output.read_text(encoding="utf-8") == "previous output\n"
    assert list(tmp_path.glob(".render-dev-compose-*")) == []


def test_render_rejects_source_output_identity_and_unresolved_template_tokens(
    tmp_path: Path,
) -> None:
    renderer = _renderer()
    with pytest.raises(ValueError):
        renderer.render(TEMPLATE, TEMPLATE, API_IMAGE, WORKER_IMAGE, COMMIT)

    template = _copy_template(
        tmp_path,
        TEMPLATE.read_text(encoding="utf-8") + "\nx-unknown: __VONK_UNKNOWN__\n",
    )
    output = tmp_path / "docker-compose.yml"
    output.write_text("previous output\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unresolved"):
        renderer.render(template, output, API_IMAGE, WORKER_IMAGE, COMMIT)

    assert output.read_text(encoding="utf-8") == "previous output\n"


def test_render_preserves_output_and_removes_staging_when_compose_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = _renderer()
    output = tmp_path / "docker-compose.yml"
    output.write_text("previous output\n", encoding="utf-8")

    def fail_validation(stage: Path) -> None:
        assert (stage.parent / "secrets" / "postgres-password").is_file()
        raise subprocess.CalledProcessError(1, ["docker", "compose", "config", "-q"])

    monkeypatch.setattr(renderer, "_validate_compose", fail_validation)

    with pytest.raises(subprocess.CalledProcessError):
        renderer.render(TEMPLATE, output, API_IMAGE, WORKER_IMAGE, COMMIT)

    assert output.read_text(encoding="utf-8") == "previous output\n"
    assert list(tmp_path.glob(".render-dev-compose-*")) == []


def test_compose_validation_uses_only_path_and_fixed_dev_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = _renderer()
    stage = tmp_path / "docker-compose.yml"
    stage.write_text("services: {}\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def capture_run(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(renderer.subprocess, "run", capture_run)
    monkeypatch.setenv("COMPOSE_FILE", "/attacker/compose.yaml")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "caller-project")
    monkeypatch.setenv("VONK_DEV_PORT", "65535")
    monkeypatch.setenv("VONK_DEV_INFERENCE_PORT", "65534")
    monkeypatch.setenv("VONK_AGENT_PORT", "9443")

    renderer._validate_compose(stage)

    assert captured["env"] == {
        "PATH": os.environ["PATH"],
        "VONK_AGENT_PORT": "8443",
        "VONK_DEV_INFERENCE_PORT": "4000",
        "VONK_DEV_PORT": "8080",
    }


def test_render_stages_every_compose_secret_with_nonsecret_synthetic_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = _renderer()
    output = tmp_path / "docker-compose.yml"
    captured: dict[str, object] = {}

    def inspect_validation(stage: Path) -> None:
        secrets_root = stage.parent / "secrets"
        captured["validation_root"] = stage.parent
        captured["secrets"] = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(secrets_root.iterdir())
        }

    monkeypatch.setattr(renderer, "_validate_compose", inspect_validation)

    renderer.render(TEMPLATE, output, API_IMAGE, WORKER_IMAGE, COMMIT)

    assert set(captured["secrets"]) == EXPECTED_SECRET_NAMES
    for value in captured["secrets"].values():
        assert value
        assert "PRIVATE KEY" not in value
    assert not Path(captured["validation_root"]).exists()


def test_repeated_render_is_byte_stable(tmp_path: Path) -> None:
    output = tmp_path / "docker-compose.yml"
    renderer = _renderer()

    renderer.render(TEMPLATE, output, API_IMAGE, WORKER_IMAGE, COMMIT)
    first = output.read_bytes()

    renderer.render(TEMPLATE, output, API_IMAGE, WORKER_IMAGE, COMMIT)

    assert output.read_bytes() == first


def test_cleanup_failure_preserves_output_and_cleans_staging_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = _renderer()
    output = tmp_path / "docker-compose.yml"
    output.write_text("previous output\n", encoding="utf-8")
    real_rmtree = renderer.shutil.rmtree
    attempts = 0

    def fail_once(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic cleanup failure")
        real_rmtree(path)

    monkeypatch.setattr(renderer.shutil, "rmtree", fail_once)

    with pytest.raises(OSError, match="synthetic cleanup failure"):
        renderer.render(TEMPLATE, output, API_IMAGE, WORKER_IMAGE, COMMIT)

    assert attempts == 2
    assert output.read_text(encoding="utf-8") == "previous output\n"
    assert list(tmp_path.glob(".render-dev-compose-*")) == []


def test_rendered_compose_is_image_only_and_has_exact_runtime_boundaries(
    tmp_path: Path,
) -> None:
    _, output = _rendered_pinned(tmp_path)
    rendered = json.loads(
        subprocess.run(
            ("docker", "compose", "-f", str(output), "config", "--format", "json"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    services = rendered["services"]

    assert all("build" not in service for service in services.values())
    assert services["dev-bootstrap"]["image"] == API_IMAGE
    assert services["migrate"]["image"] == API_IMAGE
    assert services["dev-auth-init"]["image"] == API_IMAGE
    assert services["control-api"]["image"] == API_IMAGE
    assert services["control-worker"]["image"] == WORKER_IMAGE
    for service in (
        "dev-bootstrap",
        "migrate",
        "control-api",
        "control-worker",
    ):
        environment = services[service]["environment"]
        assert environment["VONK_DEV_SELECTED_COHORT_FILE"] == "/cohort/selected.json"
        assert "VONK_DEV_EXPECTED_COMMIT" not in environment
        assert "VONK_DEV_API_IMAGE" not in environment
        assert "VONK_DEV_WORKER_IMAGE" not in environment
    assert services["control-api"]["environment"]["VONK_DEPLOYMENT_BRANCH"] == "deploy"
    assert (
        services["control-worker"]["environment"]["VONK_DEPLOYMENT_BRANCH"] == "deploy"
    )

    volumes = {
        service_name: {
            volume["target"]: volume for volume in service.get("volumes", [])
        }
        for service_name, service in services.items()
    }
    assert volumes["control-api"]["/repository"].get("read_only", False) is False
    assert volumes["control-worker"]["/repository"]["read_only"] is True
    assert all(
        volume.get("type") != "bind" or volume["target"] != "/repository"
        for service in services.values()
        for volume in service.get("volumes", [])
    )
    assert {secret["source"] for secret in services["postgres"]["secrets"]} == {
        "postgres-password"
    }
    assert {secret["source"] for secret in services["dev-bootstrap"]["secrets"]} == (
        EXPECTED_SECRET_NAMES - {"postgres-password"}
    )
    assert services["migrate"].get("secrets", []) == []
    assert services["control-api"].get("secrets", []) == [
        {"source": "controller-ca", "target": "/run/secrets/controller-ca"}
    ]
    assert volumes["control-api"]["/run/secrets"] == {
        "type": "volume",
        "source": "dev-api-secrets",
        "target": "/run/secrets",
        "read_only": True,
        "volume": {},
    }
    assert "secrets" not in services["control-worker"]
    assert volumes["control-api"]["/run/secrets"]["source"].endswith("dev-api-secrets")
    init_migrate_secrets = volumes["dev-bootstrap"]["/migrate-secrets"]
    migrate_secrets = volumes["migrate"]["/run/secrets"]
    assert init_migrate_secrets["type"] == "volume"
    assert migrate_secrets["type"] == "volume"
    assert migrate_secrets["source"] == init_migrate_secrets["source"]
    assert migrate_secrets["source"].endswith("dev-migrate-secrets")
    assert migrate_secrets["read_only"] is True
    assert migrate_secrets["source"] != volumes["control-api"]["/run/secrets"]["source"]
    assert (
        migrate_secrets["source"] != volumes["control-worker"]["/run/secrets"]["source"]
    )
    assert volumes["control-worker"]["/run/secrets"]["source"].endswith(
        "dev-worker-secrets"
    )
    assert volumes["dev-auth-init"]["/auth-secrets"] == {
        "type": "volume",
        "source": "dev-auth-secrets",
        "target": "/auth-secrets",
        "read_only": True,
        "volume": {},
    }
    assert volumes["tailscale-gateway"]["/run/secrets"] == {
        "type": "volume",
        "source": "dev-tailscale-secrets",
        "target": "/run/secrets",
        "read_only": True,
        "volume": {},
    }
    assert services["dev-auth-init"].get("secrets", []) == []
    assert services["tailscale-gateway"].get("secrets", []) == []
    assert set(services["dev-auth-init"]["networks"]) == {"data"}
    assert services["control-api"]["depends_on"]["dev-auth-init"]["condition"] == (
        "service_completed_successfully"
    )


def test_mutable_and_pinned_outputs_share_secret_projection_topology_without_values(
    tmp_path: Path,
) -> None:
    _, pinned_output = _rendered_pinned(tmp_path)
    mutable_root = tmp_path / "mutable"
    mutable_root.mkdir()
    _, mutable_output = _rendered_dev(mutable_root)

    def topology(output: Path) -> dict[str, object]:
        rendered = json.loads(
            subprocess.run(
                ("docker", "compose", "-f", str(output), "config", "--format", "json"),
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        services = rendered["services"]
        return {
            "dev-bootstrap-volumes": services["dev-bootstrap"]["volumes"],
            "dev-auth-init": {
                key: value
                for key, value in services["dev-auth-init"].items()
                if key not in {"image", "pull_policy"}
            },
            "tailscale-gateway-volumes": services["tailscale-gateway"]["volumes"],
            "tailscale-gateway-environment": services["tailscale-gateway"][
                "environment"
            ],
            "api-dependencies": services["control-api"]["depends_on"],
            "volumes": set(rendered["volumes"]),
        }

    assert topology(pinned_output) == topology(mutable_output)
    for output in (pinned_output, mutable_output):
        text = output.read_text(encoding="utf-8")
        assert "synthetic-tailscale-client-id" not in text
        assert "synthetic-tailscale-client-secret" not in text
        assert "synthetic-admin-password" not in text


def test_mutable_compose_uses_exact_refs_and_always_pulls_every_first_party_service(
    tmp_path: Path,
) -> None:
    _, output = _rendered_dev(tmp_path)
    rendered = json.loads(
        subprocess.run(
            ("docker", "compose", "-f", str(output), "config", "--format", "json"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    services = rendered["services"]
    api_services = {
        "dev-cohort-reset",
        "dev-api-cohort",
        "dev-cohort-verify",
        "dev-bootstrap",
        "migrate",
        "dev-auth-init",
        "control-api",
    }
    worker_services = {"dev-worker-cohort", "control-worker"}

    assert all(services[name]["image"] == API_DEV_IMAGE for name in api_services)
    assert all(services[name]["image"] == WORKER_DEV_IMAGE for name in worker_services)
    assert all(
        services[name]["pull_policy"] == "always"
        for name in api_services | worker_services
    )
    dev_init_environment = services["dev-bootstrap"]["environment"]
    assert (
        dev_init_environment["VONK_DEV_SELECTED_COHORT_FILE"] == "/cohort/selected.json"
    )
    assert "VONK_DEV_EXPECTED_COMMIT" not in dev_init_environment
    assert "VONK_DEV_API_IMAGE" not in dev_init_environment
    assert "VONK_DEV_WORKER_IMAGE" not in dev_init_environment


def test_template_is_development_only_and_never_mentions_local_acceptance() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "VONK_DEV_LOCAL_ACCEPTANCE" not in text
    assert "file:///" not in text
    assert "build:" not in text
    assert "context:" not in text
    assert "../" not in text
    assert {
        name for name, value in PORT_INTERPOLATIONS.items() if text.count(value) == 1
    } == set(PORT_INTERPOLATIONS)
