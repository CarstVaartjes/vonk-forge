from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

README = ROOT / "README.md"
INSTALL_AGENT = ROOT / "docs/operations/install-vonk-agent.md"
NODE_ONBOARDING = ROOT / "docs/runbooks/node-onboarding.md"
AGENT_PKI = ROOT / "docs/runbooks/agent-pki.md"
DEV_NAS = ROOT / "docs/runbooks/development-nas-installation.md"
COMPOSE_README = ROOT / "deploy/compose/README.md"
ARCHITECTURE = ROOT / "docs/architecture-overview.md"
SUPPLY_CHAIN = ROOT / "docs/runbooks/supply-chain.md"


def _normalized_text(path: Path) -> str:
    return " ".join(path.read_text().split())


def test_active_agent_install_examples_keep_pairing_and_controller_inputs_together() -> None:
    text = INSTALL_AGENT.read_text()

    assert 'enrollment_url = "https://<ENROLLMENT_HOSTNAME>/"' in text
    assert 'controller_url = "https://<CONTROLLER_HOSTNAME>/"' in text
    assert 'ca_path = "/etc/vonk-forge-agent/controller-ca.pem"' in text
    assert "controller-ca.pem" in text
    assert "openssl x509" in text
    assert "-outform DER" in text
    assert "sha256sum" in text
    assert "DER SHA-256" in text


def test_generic_onboarding_uses_hosts_placeholders_for_nas_and_agent_nodes() -> None:
    combined = " ".join(
        path.read_text()
        for path in (INSTALL_AGENT, NODE_ONBOARDING, AGENT_PKI, DEV_NAS, ARCHITECTURE)
    )

    assert "/etc/hosts" in combined
    assert "<NAS_MANAGEMENT_IP>" in combined
    assert "<ENROLLMENT_HOSTNAME>" in combined
    assert "<CONTROLLER_HOSTNAME>" in combined
    assert "<REGISTRY_HOSTNAME>" in combined
    assert "192.168.1.231" not in combined


def test_development_nas_contract_keeps_only_compose_file_and_secrets_directory() -> None:
    text = _normalized_text(DEV_NAS)

    assert "Its contents must be exactly:" in text
    assert "├── docker-compose.yml" in text
    assert "└── secrets/" in text
    assert "No `current/`, source tree, Dockerfiles, or `.env` file belongs in this project." in text


def test_development_and_production_docs_separate_mutable_dev_from_authoritative_production() -> None:
    readme = _normalized_text(README)
    compose = _normalized_text(COMPOSE_README)
    supply_chain = _normalized_text(SUPPLY_CHAIN)

    assert "operator-pulled/redeployed" in readme
    assert "mutable `:dev`" in readme
    assert "`latest` is informational only" in readme
    assert "host-updater" in readme
    assert "authoritative" in readme

    assert "trusted host updater" in compose
    assert "`:latest` is evaluation/discovery only" in compose
    assert "selected from one complete release asset" in compose

    assert "`latest` is informational only" in supply_chain
    assert "host-updater" in supply_chain
