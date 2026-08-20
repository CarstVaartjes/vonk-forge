from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_ROOT = ROOT / "deploy/compose"
CONTROL_DOCKERFILE = ROOT / "control/Dockerfile"


def _document(name: str) -> dict[str, object]:
    content = yaml.safe_load((COMPOSE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(content, dict)
    return content


def test_fresh_postgres_initializes_the_litellm_database() -> None:
    canonical = _document("compose.yaml")
    postgres = canonical["services"]["postgres"]

    assert "litellm-database-password" in postgres["secrets"]
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
