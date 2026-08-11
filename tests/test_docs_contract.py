import re
import tomllib
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

GENERIC_ONBOARDING_DOCS = (
    README,
    INSTALL_AGENT,
    NODE_ONBOARDING,
    AGENT_PKI,
    DEV_NAS,
    COMPOSE_README,
    ARCHITECTURE,
    SUPPLY_CHAIN,
)


def _normalized_text(path: Path) -> str:
    return " ".join(path.read_text().split())


def _fenced_blocks(path: Path, *languages: str) -> list[str]:
    blocks = re.findall(
        r"^[ \t]*```([^\n]*)\n(.*?)^[ \t]*```$",
        path.read_text(),
        flags=re.MULTILINE | re.DOTALL,
    )
    return [body for language, body in blocks if language.strip() in languages]


def _section(path: Path, heading: str) -> str:
    text = path.read_text()
    start = text.index(f"## {heading}")
    following_heading = re.search(r"^## ", text[start + len(heading) + 3 :], re.MULTILINE)
    if following_heading is None:
        return text[start:]
    end = start + len(heading) + 3 + following_heading.start()
    return text[start:end]


def _ordered_steps(section: str) -> list[str]:
    steps: list[str] = []
    for line in section.splitlines():
        item = re.match(r"^\d+\.\s+(.*)$", line)
        if item:
            steps.append(item.group(1))
        elif steps and line.startswith("   "):
            steps[-1] += f" {line.strip()}"
    return steps


def test_active_agent_install_examples_keep_pairing_and_controller_inputs_together() -> None:
    agent_configs = []
    for block in _fenced_blocks(INSTALL_AGENT, "toml"):
        config = tomllib.loads(block)
        if {"enrollment_url", "controller_url"} & config.keys():
            agent_configs.append(config)

    assert agent_configs
    for config in agent_configs:
        assert config["enrollment_url"] == "https://<ENROLLMENT_HOSTNAME>/"
        assert config["controller_url"] == "https://<CONTROLLER_HOSTNAME>/"
        assert config["ca_path"] == "/etc/vonk-forge-agent/controller-ca.pem"
        assert config["ca_sha256"] == "<64_LOWERCASE_HEX_FROM_SHA256SUM>"
        assert config["node_id"] == "<NODE_ID>"


def test_agent_ca_fingerprint_is_derived_from_der_and_used_without_command_output_noise() -> None:
    install_blocks = _fenced_blocks(INSTALL_AGENT, "bash", "sh", "shell")
    fingerprint_block = next(
        block for block in install_blocks if "openssl x509" in block
    )
    text = _normalized_text(INSTALL_AGENT)

    assert re.search(
        r"(?:^|\n)openssl x509 -in controller-ca\.pem -outform DER \| sha256sum(?:\n|$)",
        fingerprint_block,
    )
    assert "<64-lowercase-hex> -" in text
    assert "Copy only the first field into `ca_sha256`" in text
    assert "DER SHA-256" in text


def test_agent_urls_have_distinct_pairing_and_post_identity_roles() -> None:
    sentences = re.split(r"(?<=[.!])\s+", _normalized_text(INSTALL_AGENT))

    assert any(
        "`enrollment_url`" in sentence
        and "only" in sentence
        and "`pair`" in sentence
        for sentence in sentences
    )
    assert any(
        "`controller_url`" in sentence
        and "only after" in sentence
        and "certificate" in sentence
        and "authenticated service" in sentence
        for sentence in sentences
    )


def test_onboarding_preserves_the_one_use_grant_pair_approve_pair_sequence() -> None:
    steps = _ordered_steps(_section(NODE_ONBOARDING, "Install and pair the agent"))

    assert "one-use" in steps[0] and "grant" in steps[0]
    assert "`vonk-agent pair`" in steps[1]
    assert steps[2].startswith("Approve the pending enrollment")
    assert "Repeat the same `pair` command" in steps[3]


def test_generic_path_covers_secret_safe_backup_identity_recovery_and_package_removal() -> None:
    onboarding = _section(NODE_ONBOARDING, "Clean-machine prerequisites")
    agent_lifecycle = _section(INSTALL_AGENT, "Rotation, recovery, and removal")
    pki_recovery = _section(AGENT_PKI, "Backup and restore consistency")
    nas_recovery = _section(DEV_NAS, "Rotation and recovery")
    removal_blocks = _fenced_blocks(INSTALL_AGENT, "bash", "sh", "shell")

    assert "1Password" in onboarding
    assert "do not reveal or copy the private key" in onboarding
    assert "fresh one-use grant" in agent_lifecycle
    assert "new local key plus a new certificate" in agent_lifecycle
    assert any(
        "systemctl disable --now" in block and "apt remove vonk-forge-agent" in block
        for block in removal_blocks
    )
    assert "same verified backup generation" in " ".join(pki_recovery.split())
    assert "Never commit a backup" in nas_recovery


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


def test_generic_docs_exclude_task_11_site_constants_and_physical_acceptance_results() -> None:
    combined = "\n".join(path.read_text() for path in GENERIC_ONBOARDING_DOCS)
    forbidden = (
        "192.168.1.231",
        "dgx-spark-1",
        "dgx-spark-2",
        "current acceptance run",
        "physical Spark acceptance",
        "NAS Compose checksum",
    )

    for site_fact in forbidden:
        assert site_fact not in combined

    for path in GENERIC_ONBOARDING_DOCS:
        for block in _fenced_blocks(path, "bash", "sh", "shell"):
            assert not re.search(r"/volume\d+(?:/|$)", block)


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
