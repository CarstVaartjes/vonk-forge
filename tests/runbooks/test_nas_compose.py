from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_README = ROOT / "deploy/compose/README.md"
COMPOSE = ROOT / "deploy/compose/compose.yaml"
IMAGE_LOCK = ROOT / "deploy/compose/images.lock.json"
ENVIRONMENT = ROOT / "deploy/compose/.env.example"
SUPPLY_CHAIN = ROOT / "docs/runbooks/supply-chain.md"
CONTROL_BOOTSTRAP = ROOT / "docs/runbooks/control-plane-bootstrap.md"
CONTROL_RECOVERY = ROOT / "docs/runbooks/control-plane-recovery.md"
HERMES_RUNBOOK = ROOT / "docs/runbooks/hermes-agent.md"
TAILSCALE_RUNBOOK = ROOT / "docs/runbooks/tailscale.md"
AGENT_PKI_RUNBOOK = ROOT / "docs/runbooks/agent-pki.md"
PRODUCTION_FILE_VARIABLES = (
    "DATABASE_URL_FILE",
    "POSTGRES_PASSWORD_FILE",
    "TOKEN_SIGNING_KEY_FILE",
    "METRICS_TOKEN_FILE",
    "GIT_SIGNING_KEY_FILE",
    "WORKER_API_TOKEN_FILE",
    "GRAFANA_ADMIN_PASSWORD_FILE",
    "LITELLM_MASTER_KEY_FILE",
    "LITELLM_UPSTREAM_KEY_FILE",
    "LITELLM_DATABASE_URL_FILE",
    "AGENT_CLIENT_CA_FILE",
    "AGENT_INTERMEDIATE_CERTIFICATE_FILE",
    "AGENT_PROXY_AUTH_FILE",
    "AGENT_CA_CREDENTIAL_FILE",
    "AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE",
    "STEP_CA_CONFIG_FILE",
    "STEP_CA_ROOT_CERTIFICATE_FILE",
    "STEP_CA_INTERMEDIATE_KEY_FILE",
    "STEP_CA_PASSWORD_FILE",
    "TAILSCALE_OAUTH_CLIENT_ID_FILE",
    "TAILSCALE_OAUTH_CLIENT_SECRET_FILE",
    "HERMES_API_KEY_FILE",
)


def test_nas_compose_readme_is_the_complete_operator_entry_point() -> None:
    text = COMPOSE_README.read_text()
    for required in (
        "ghcr.io/carstvaartjes/vonk-forge-api",
        "ghcr.io/carstvaartjes/vonk-forge-worker",
        "ghcr.io/carstvaartjes/vonk-forge-hermes",
        "NAS_LAN_IP=10.0.0.2",
        "compose.step-ca.yaml",
        "`:latest` is evaluation/discovery only",
        "Set package visibility to Public",
        "not the Docker bridge",
        "not the public WAN address",
        "VONK_CONTAINER_RELEASES_ENABLED",
        "VONK_PLATFORM_RELEASES_ENABLED",
        "No images are currently being published",
        "Dependabot cannot publish",
        "operator_user=$(id -un)",
        "/srv/vonk-forge/control-host",
        "/srv/vonk-forge/control-identity",
        "/srv/vonk-forge/site",
        "upgrade --target-name",
        "recover --apply",
        "rollback --generation",
        "verified OCI deployment bundle",
        "never executes Compose from the repository checkout",
        "At least 32 bytes.",
        "At least 16 non-whitespace characters.",
        "10001:10001",
        "10002:10001",
        "65534:65534",
        "1100:1100",
        "control.vonk-forge.lan is not a LAN-accessible human endpoint",
        "setfacl -R -m u:\"$operator_user\":rwX,u:10001:rwX,m::rwX",
        "d:u:\"$operator_user\":rwx,d:u:10001:rwx,d:m::rwx",
        "CONTROL_API writes `.git`",
    ):
        assert required in text
    for variable in PRODUCTION_FILE_VARIABLES:
        assert variable in text
    assert "\nsudo git clone " not in text
    assert "cd /srv/vonk-forge/repository/deploy/compose" not in text
    assert "docker compose --env-file" not in text
    assert "deploy/compose/bin/backup-control-plane" not in text
    assert "deploy/compose/bin/restore-control-plane" not in text


def test_environment_requires_three_release_images_without_duplicate_networks() -> None:
    text = ENVIRONMENT.read_text()
    assert "CONTROL_API_IMAGE=ghcr.io/carstvaartjes/vonk-forge-api:" in text
    assert "CONTROL_WORKER_IMAGE=ghcr.io/carstvaartjes/vonk-forge-worker:" in text
    assert "HERMES_AGENT_IMAGE=ghcr.io/carstvaartjes/vonk-forge-hermes:" in text
    assert text.count("VONK_MANAGEMENT_CIDRS=") == 1
    assert text.count("VONK_DIRECT_FABRIC_CIDRS=") == 1


def test_default_grafana_image_matches_the_audited_lock() -> None:
    expected = (
        "grafana/grafana:13.0.2@sha256:"
        "5dad0df181cb644a14e13617b913b261a54f7d4fd4510721dba420929f35bea2"
    )
    lock = json.loads(IMAGE_LOCK.read_text())

    assert lock["images"]["grafana"] == expected
    assert expected in COMPOSE.read_text()


def test_supply_chain_describes_three_target_release_or_nonpublishing_diagnostics() -> None:
    text = SUPPLY_CHAIN.read_text()
    for required in (
        "CONTROL_API_IMAGE",
        "CONTROL_WORKER_IMAGE",
        "HERMES_AGENT_IMAGE",
        "stable SemVer version-tag push",
        "vonk-forge-hermes",
        "local diagnostic only",
    ):
        assert required in text
    assert "Publish both immutable control images" not in text


def test_pull_only_bootstrap_delegates_to_the_host_updater_sequence() -> None:
    text = CONTROL_BOOTSTRAP.read_text()

    assert "../../deploy/compose/README.md#install-and-first-selection" in text
    assert "upgrade --target-name" in text
    assert "verified generation" in text
    assert "never executes Compose from the repository checkout" in text
    assert "docker compose --env-file" not in text


def test_control_recovery_uses_only_the_journaled_host_boundary() -> None:
    text = CONTROL_RECOVERY.read_text()

    for required in (
        "HostBackupBoundary",
        "/srv/vonk-forge/control-host",
        "recover --apply",
        "rollback --generation",
        "hash-chained journal",
        "root-owned age recipients file",
        "exact backup receipt",
    ):
        assert required in text
    assert "backup-control-plane" not in text
    assert "restore-control-plane" not in text
    assert "ENCRYPT_COMMAND" not in text
    assert "DECRYPT_COMMAND" not in text
    assert "docker compose" not in text


def test_tailscale_runbook_documents_the_exact_private_browser_service() -> None:
    text = TAILSCALE_RUNBOOK.read_text()
    normalized = " ".join(text.split())
    policy = normalized.split("## Identity and access policy", 1)[1].split(
        "## Secrets and unattended startup", 1
    )[0]
    setup = policy.split("Before creating credentials or policy", 1)[1]

    for required in (
        "Trust credentials",
        "Services → Advertise → Define a Service",
        "Credential → OAuth",
        "MagicDNS",
        "HTTPS certificates",
        "`auth_keys` write scope",
        "`tag:vonk-gateway`",
        "`svc:vonk-forge`",
        "`tcp:443`",
        "HTTPS 443 -> http://caddy:8080",
        "HTTPS-only",
        "Funnel",
        "application administrator login",
        "stable Service URL",
    ):
        assert required in normalized
    assert "PASTE_TAILSCALE" not in text
    assert "No Windows hosts-file entry" in normalized
    assert setup.index("MagicDNS") < setup.index(
        "`svc:vonk-forge`"
    ) < setup.index("Merge the reviewed sections")
    verification = normalized.split("## Verification", 1)[1].split(
        "## Drain, revocation, and recovery", 1
    )[0]
    assert "Development must report exactly one Service" in verification
    assert "Production must report exactly three Services" in verification


def test_hermes_secret_mode_matches_the_authoritative_nas_guide() -> None:
    runbook = HERMES_RUNBOOK.read_text()
    guide = COMPOSE_README.read_text()

    assert "chmod 0400 /srv/vonk-forge/secrets/hermes-api-key" in runbook
    assert "root-owned mode `0400`" in runbook
    assert "`root:root 0400`" in guide
    assert "root-owned mode `0600`" not in runbook


def test_inference_runbooks_route_hermes_through_the_caddy_lease_edge() -> None:
    hermes = " ".join(HERMES_RUNBOOK.read_text().split())

    assert (
        "OpenAI-compatible base URL: http://caddy:8081/v1" in HERMES_RUNBOOK.read_text()
    )
    assert "Hermes sends model requests only to LiteLLM" not in hermes

    for path in (HERMES_RUNBOOK, CONTROL_BOOTSTRAP):
        text = path.read_text()
        normalized = " ".join(text.split())

        assert "litellm:4000" not in text
        assert "`hermes-inference`" in normalized
        assert "`caddy:8081/v1`" in normalized
        assert "`litellm-edge`" in normalized


def test_auxiliary_service_runbooks_use_the_installed_maintenance_boundary() -> None:
    for path in (HERMES_RUNBOOK, TAILSCALE_RUNBOOK, AGENT_PKI_RUNBOOK):
        text = path.read_text()
        assert "vonk-control-offline maintenance" in text
        assert "cd deploy/compose" not in text
        assert "docker compose --" not in text

    hermes = HERMES_RUNBOOK.read_text()
    assert "--generation REPLACE_GENERATION_FROM_PLAN --apply" in hermes
    assert "/generations/$active/bin/harden-hermes-egress" in hermes
