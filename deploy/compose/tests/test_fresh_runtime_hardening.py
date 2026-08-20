from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_ROOT = ROOT / "deploy/compose"
CONTROL_DOCKERFILE = ROOT / "control/Dockerfile"


def _document(name: str) -> dict[str, object]:
    content = yaml.safe_load((COMPOSE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(content, dict)
    return content


def _final_stage(name: str) -> str:
    dockerfile = CONTROL_DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^FROM [^\n]+ AS {re.escape(name)}\n(?P<body>.*?)(?=^FROM |\Z)",
        dockerfile,
    )
    assert match is not None
    return match.group("body")


def test_fresh_postgres_initializes_the_litellm_database() -> None:
    canonical = _document("compose.yaml")
    postgres = canonical["services"]["postgres"]

    assert "litellm-database-password" in postgres["secrets"]
    assert postgres["entrypoint"] == ["/usr/local/bin/vonk-postgres-entrypoint"]
    assert (
        "./postgres/entrypoint.sh:"
        "/usr/local/bin/vonk-postgres-entrypoint:ro"
        in postgres["volumes"]
    )
    assert (
        "./postgres/init-databases.sh:"
        "/docker-entrypoint-initdb.d/10-vonk-forge-databases.sh:ro"
        in postgres["volumes"]
    )
    assert canonical["secrets"]["litellm-database-password"] == {
        "file": (
            "${LITELLM_DATABASE_PASSWORD_FILE:?"
            "set LiteLLM database password secret file}"
        )
    }


def test_caddy_healthcheck_does_not_depend_on_a_virtual_host() -> None:
    canonical = _document("compose.yaml")
    healthcheck = canonical["services"]["caddy"]["healthcheck"]["test"]
    caddyfile = (COMPOSE_ROOT / "Caddyfile").read_text(encoding="utf-8")

    assert healthcheck == [
        "CMD-SHELL",
        "wget -q -O /dev/null http://127.0.0.1:8082/healthz",
    ]
    assert "http://127.0.0.1:8082" in caddyfile


def test_caddy_serves_the_site_controller_certificate() -> None:
    canonical = _document("compose.yaml")
    caddy = canonical["services"]["caddy"]
    caddyfile = (COMPOSE_ROOT / "Caddyfile").read_text(encoding="utf-8")

    assert set(caddy["secrets"]) >= {
        "controller-server-certificate",
        "controller-server-key",
    }
    assert caddyfile.count(
        "tls /run/secrets/controller-server-certificate "
        "/run/secrets/controller-server-key"
    ) == 3


def test_control_images_do_not_install_git_or_ssh() -> None:
    dockerfile = CONTROL_DOCKERFILE.read_text(encoding="utf-8").lower()

    assert "apt-get install" not in dockerfile
    assert "openssh" not in dockerfile
    assert " git" not in dockerfile


def test_api_image_bounds_root_to_preexec_and_seals_source_secret_directory() -> None:
    worker = _final_stage("worker")
    api = _final_stage("api")

    assert "USER 10001:10001" in worker
    assert "api_preexec" not in worker
    assert "CMD" not in worker
    assert "USER 0:0" in api
    assert 'ENTRYPOINT ["python", "-m", "vonk_control.api_preexec"]' in api
    assert 'CMD ["python", "-m", "vonk_control.api"]' in api
    assert "install -d -o 0 -g 0 -m 0700 /run/secrets" in _final_stage("api-root")
