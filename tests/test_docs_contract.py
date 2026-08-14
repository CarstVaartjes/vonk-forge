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
DEV_WORKLOADS = ROOT / "docs/runbooks/development-agent-workloads.md"
MIA_TWO_SPARK = ROOT / "docs/runbooks/mia-deepseek-v4-flash.md"
DEV_WORKLOAD_ACCEPTANCE = ROOT / "docs/audits/development-agent-workload-acceptance.md"
DEV_WORKLOADS_DESIGN = (
    ROOT
    / "docs/superpowers/specs/2026-08-10-development-agent-workload-slices-design.md"
)
MODEL_SWITCHING = ROOT / "docs/runbooks/model-switching.md"
MODEL_CAPACITY = ROOT / "docs/model-capacity-overview.md"
DEVELOPMENT_MODEL_SMOKE = ROOT / "docs/audits/development-model-smoke.md"
FRESH_DEVELOPMENT_INSTALL = ROOT / "docs/runbooks/fresh-development-install.md"
PLATFORM_UPDATE = ROOT / "docs/runbooks/platform-update.md"
RUNTIME_RELEASE = ROOT / "docs/runbooks/runtime-release.md"
VONKCTL = ROOT / "docs/runbooks/vonkctl.md"
DOCS_INDEX = ROOT / "docs/README.md"
CONTROL_RECOVERY = ROOT / "docs/runbooks/control-plane-recovery.md"
THREAT_MODEL = ROOT / "docs/security/threat-model.md"

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


def test_readme_describes_the_spark_native_runtime_boundary() -> None:
    text = _normalized_text(README)

    assert "Spark-managed Docker/NVIDIA runtime" in text
    assert "for isolated recipe builds only" in text
    assert "Docker is only required on the NAS" not in text


def test_active_agent_install_examples_keep_pairing_and_controller_inputs_together() -> None:
    agent_configs = []
    for block in _fenced_blocks(INSTALL_AGENT, "toml"):
        config = tomllib.loads(block)
        if {"enrollment_url", "controller_url"} & config.keys():
            agent_configs.append(config)

    assert agent_configs
    for config in agent_configs:
        assert config["enrollment_url"] == "https://<ENROLLMENT_HOSTNAME>:8443/"
        assert config["controller_url"] == "https://<CONTROLLER_HOSTNAME>:8443/"
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


def test_agent_install_documents_explicit_agent_ingress_hosts_and_firewall() -> None:
    text = INSTALL_AGENT.read_text()
    normalized = _normalized_text(INSTALL_AGENT)

    assert "/etc/hosts" in text
    assert (
        "<NAS_MANAGEMENT_IP> <ENROLLMENT_HOSTNAME> <CONTROLLER_HOSTNAME> "
        "<REGISTRY_HOSTNAME>"
    ) in text
    assert "<NODE_MANAGEMENT_CIDR>" in text
    assert "<NAS_MANAGEMENT_IP>:8443" in text
    assert "https://<ENROLLMENT_HOSTNAME>:8443/" in text
    assert "https://<CONTROLLER_HOSTNAME>:8443/" in text
    assert "reject all other sources" in normalized


def test_spark_workload_firewall_is_docker_aware_and_fresh_install_blocking() -> None:
    workloads = _normalized_text(DEV_WORKLOADS)
    install = _normalized_text(INSTALL_AGENT)
    fresh = _normalized_text(FRESH_DEVELOPMENT_INSTALL)

    assert (
        "Docker diverts published traffic before ordinary UFW `INPUT` rules"
        in workloads
    )
    assert "`DOCKER-USER`" in workloads
    assert "`--ctorigdst`" in workloads
    assert "`--ctorigdstport`" in workloads
    assert "vonk-forge-managed-v1" in workloads
    assert "refuses a foreign `VONK-FORGE` chain" in workloads
    assert "non-entrypoint rank publishes its health endpoint only" in workloads
    assert "Published Docker ports bypass ordinary UFW `INPUT` policy" in install
    assert "persistent Docker-aware `DOCKER-USER` policy" in fresh
    assert "vonk-forge-docker-firewall.service" in workloads
    assert "/etc/vonk-forge-agent/docker-firewall.conf" in workloads
    assert "systemctl restart docker" in workloads
    assert "systemctl is-active vonk-forge-docker-firewall.service" in workloads
    assert "recipe_endpoint_port=8000" not in workloads
    assert "original published host port" in workloads
    assert "unlisted Docker-published TCP port" in workloads
    assert "`VONK-FORGE-HOST`" in workloads
    assert "`VONK_HOST_ENDPOINT_PORTS=8888`" in workloads
    assert "`check-host-port`" in workloads


def test_fresh_spark_install_does_not_claim_nvidia_platform_ownership() -> None:
    fresh = _normalized_text(FRESH_DEVELOPMENT_INSTALL)
    platform = _normalized_text(PLATFORM_UPDATE)
    runtime_release = _normalized_text(RUNTIME_RELEASE)
    legacy = _normalized_text(VONKCTL)

    assert "NVIDIA Sync owns supported cluster networking and node-to-node SSH" in fresh
    assert "must not stop, disable, mask, or install `earlyoom`" in platform
    assert "archived SSH-controller compatibility tool" in runtime_release
    assert "host networking and host IPC are legacy runtime exceptions" in legacy


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


def test_agent_recovery_documents_bounded_start_limit_procedure() -> None:
    lifecycle = _section(INSTALL_AGENT, "Rotation, recovery, and removal")
    fresh = _normalized_text(FRESH_DEVELOPMENT_INSTALL)
    blocks = _fenced_blocks(INSTALL_AGENT, "bash", "sh", "shell")
    recovery = next(
        block
        for block in blocks
        if "systemctl reset-failed vonk-forge-agent.service" in block
    )

    assert "start-limit-hit" in lifecycle
    assert "controller" in lifecycle
    assert "healthy" in lifecycle
    assert "journalctl" in recovery
    assert "systemctl start vonk-forge-agent.service" in recovery
    assert "systemctl is-active vonk-forge-agent.service" in recovery
    assert recovery.index("journalctl") < recovery.index("systemctl reset-failed")
    assert "`start-limit-hit`" in fresh
    assert "install-vonk-agent.md#rotation-recovery-and-removal" in fresh


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

    for name in (
        "agent-ca-certificate",
        "agent-ca-key",
        "agent-proxy-auth",
        "controller-ca",
        "controller-server-certificate",
        "controller-server-key",
        "database-url",
        "git-signing-key",
        "litellm-master-key",
        "litellm-upstream-key",
        "management-cidrs",
        "postgres-password",
    ):
        assert f"├── {name}" in text or f"└── {name}" in text

    assert "--direct-fabric-cidrs '<DIRECT_FABRIC_CIDRS_OR_NONE>'" in text
    assert "the literal `none`" in text
    assert "must not overlap the management network" in _normalized_text(
        FRESH_DEVELOPMENT_INSTALL
    )


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


def test_complete_development_workload_runbook_has_every_operator_phase() -> None:
    required_headings = (
        "Scope and placeholders",
        "Prerequisites and trust boundaries",
        "PKI and NAS project",
        "/etc/hosts and firewall",
        "Package installation and pairing",
        "Inventory preflight",
        "Synthetic lifecycle",
        "Real single-node model",
        "Real multi-node failure and recovery",
        "Restart persistence",
        "Normal stop and uninstall",
        "Rollback and secret rotation",
        "Evidence and clean-room audit",
        "Temporary sudo cleanup",
    )
    text = DEV_WORKLOADS.read_text()

    for heading in required_headings:
        assert f"## {heading}" in text

    assert "scripts/dev-runtime-secrets.py" in text
    assert "scripts/dev-runtime-project" in text
    assert "scripts/dev-admin-token" in text
    assert "docs/operations/agent-package-release.md#install-the-dev-channel" in text
    assert "vonk-agent pair" in text
    assert "scripts/qualify-development-model" in text
    for phase in ("synthetic", "model-single", "model-multinode"):
        assert f"--phase {phase}" in text
    for checkpoint in (
        "inference-ok",
        "rank-failure-observed",
        "route-withdrawn-after-failure",
        "inference-recovered",
    ):
        assert f"--stop-after {checkpoint}" in text


def test_multinode_rendezvous_firewall_is_direct_fabric_only() -> None:
    section = _normalized_text(DEV_WORKLOADS)
    acceptance = _normalized_text(DEV_WORKLOAD_ACCEPTANCE)

    assert "TCP port `29500`" in section
    assert "`<SPARK_2_FABRIC_IP>` to `<SPARK_1_FABRIC_IP>:29500`" in section
    assert "`<SPARK_1_FABRIC_IP>:29500:29500`" in section
    assert "never `29500:29500`" in section
    assert "management or public interface" in section
    assert (
        "positive direct-fabric host probe and negative management/public probes"
        in acceptance
    )


def test_model_smoke_records_public_arm64_fabric_transport_identity() -> None:
    text = _normalized_text(DEVELOPMENT_MODEL_SMOKE)

    assert "Public multi-architecture OCI index" in text
    assert (
        "sha256:fc6dddc4c44b1bfe37f41cae8e67d1693828e8f42a91862816d7953e2c9d3f23"
        in text
    )
    assert "linux/arm64/v8" in text
    assert (
        "sha256:97d3fa0415c6749d4b27849c2bf251ac11fe2ec7d3178a2dae4bbf3bd30056fc"
        in text
    )


def test_model_commands_bind_private_qualification_and_runtime_evidence() -> None:
    blocks = _fenced_blocks(DEV_WORKLOADS, "bash", "sh", "shell")

    for phase in ("model-single", "model-multinode"):
        command = next(block for block in blocks if f"--phase {phase}" in block)
        assert (
            "--qualification-file "
            "'<EVIDENCE_DIRECTORY>/model-qualification.json'"
        ) in command

    normalized = _normalized_text(DEV_WORKLOADS)
    assert "private qualification SHA-256" in normalized
    assert "build and distribution evidence" in normalized
    assert "per-node runtime artifact evidence" in normalized
    assert "retained by the acceptance runner" in normalized


def test_latest_mia_runbook_is_exact_reproducible_and_secret_free() -> None:
    text = MIA_TWO_SPARK.read_text()
    normalized = _normalized_text(MIA_TWO_SPARK)

    for value in (
        "f752cd04ab30f2cf42077dd8811a5e1e682d63e7",
        "9e165c30e2704aec5d9d593cce3eebd58bbef1cb",
        "166898660330",
        "mia-mit",
        "VONK_HOST_ENDPOINT_PORTS=8888",
        "check-host-port 8888",
        "--recipe config/recipes/development/mia-deepseek-v4-flash.json",
        "--phase model-multinode",
        "--stop-after inference-ok",
    ):
        assert value in text
    assert "No Hugging Face token" in normalized
    assert "rank 1 runs headless" in normalized.lower()
    assert "failed installation" in normalized.lower()
    assert "one exact retry" in normalized.lower()
    assert "same installation identity" in normalized.lower()
    assert "immutable model and image caches" in normalized.lower()
    assert "derives the fabric interface directly from `/sys/class/infiniband`" in normalized
    assert "does not depend on `iproute2` inside the runtime image" in normalized


def test_secret_docs_separate_local_backup_from_exact_nas_projection() -> None:
    runbook = _normalized_text(DEV_WORKLOADS)
    design = _normalized_text(DEV_WORKLOADS_DESIGN)

    assert "exactly 21 local source files" in runbook
    assert "exactly 17 deployment files" in runbook
    assert "four local-only files" in runbook
    assert "15-file" in runbook
    assert "add-only" in runbook

    # This dated design records the accepted boundary at that historical slice.
    assert "17 local source files" in design
    assert "14 deployment secret/config files" in design
    assert "exactly 14 deployment files" in design
    assert "`controller-ca-key`" in design
    assert "must not be copied to the NAS" in design


def test_design_records_intentionally_database_free_litellm_runtime() -> None:
    design = _normalized_text(DEV_WORKLOADS_DESIGN)

    assert "LiteLLM effective configuration is intentionally database-free" in design
    assert "No database URL is projected to LiteLLM" in design
    assert "LiteLLM database URL is derived" not in design


def test_complete_runbook_keeps_access_loopback_tokens_private_and_evidence_local() -> None:
    text = DEV_WORKLOADS.read_text()
    normalized = _normalized_text(DEV_WORKLOADS)

    assert "-L <LOCAL_API_PORT>:127.0.0.1:8080" in text
    assert "-L <LOCAL_INFERENCE_PORT>:127.0.0.1:4000" in text
    assert "--admin-token-file" in text
    assert "--inference-token-file" in text
    assert "mode `0600`" in normalized
    assert ".state/development-acceptance/" in text
    assert "must not be committed" in normalized
    assert "never print secret values" in normalized.lower()
    for block in _fenced_blocks(DEV_WORKLOADS, "bash", "sh", "shell"):
        assert "cat " not in block
        assert "Get-Content" not in block


def test_complete_runbook_documents_exact_cleanup_and_recovery_boundaries() -> None:
    text = DEV_WORKLOADS.read_text()
    normalized = _normalized_text(DEV_WORKLOADS)

    assert "pull/redeploy" in normalized
    assert "named volumes" in normalized
    assert "pinned Compose" in normalized
    assert "normal API stop/uninstall" in normalized
    assert "/etc/sudoers.d/vonktemp" in text
    assert "/etc/sudoers.d/99-vonk-codex-temporary" in text
    assert "sudo -n true" in text
    assert "PASSWORD_REQUIRED" in text


def test_complete_runbook_uses_current_browser_secret_generation_contract() -> None:
    text = DEV_WORKLOADS.read_text()
    normalized = _normalized_text(DEV_WORKLOADS)

    assert "--tailscale-oauth-client-id-file" in text
    assert "--tailscale-oauth-client-secret-file" in text
    assert "exactly 21 local source files" in normalized
    assert "exactly 17 deployment files" in normalized
    for local_only in (
        "`admin-password`",
        "`controller-ca-key`",
        "`git-signing-key.pub`",
        "`host-runtime-grant-public-key`",
    ):
        assert local_only in text
    assert "--upgrade-browser-access" in text
    assert "Pull** then **Redeploy" in text


def test_related_guides_link_to_the_complete_development_acceptance_path() -> None:
    for path in (DEV_NAS, NODE_ONBOARDING, MODEL_SWITCHING, MODEL_CAPACITY):
        assert "development-agent-workloads.md" in path.read_text()


def test_operator_entry_points_make_private_browser_login_the_normal_path() -> None:
    for path in (README, DOCS_INDEX, COMPOSE_README):
        text = _normalized_text(path)
        assert "stable private Tailscale HTTPS" in text
        assert "svc:vonk-forge" in text
        assert "browser" in text
        assert "without an SSH or PowerShell tunnel" in text
        assert "development-nas-installation.md#open-the-stable-browser-url" in text


def test_fresh_install_finishes_with_direct_login_and_both_nodes_visible() -> None:
    text = FRESH_DEVELOPMENT_INSTALL.read_text()
    normalized = _normalized_text(FRESH_DEVELOPMENT_INSTALL)

    assert "stable private Tailscale HTTPS Service URL" in normalized
    assert "no Windows hosts-file entry" in normalized
    assert "Log in as exact subject `admin`" in normalized
    assert "both Sparks" in normalized
    assert "Logout" in normalized
    assert "normal browser access does not require an SSH tunnel" in normalized

    proof_parts = re.split(
        r"^## (?:\d+\. )?Prove the installation$",
        text,
        maxsplit=1,
        flags=re.MULTILINE,
    )
    assert len(proof_parts) == 2
    proof = proof_parts[1]
    assert "deterministic acceptance" in " ".join(proof.split())
    assert "ssh -N" in proof


def test_hosts_entries_are_only_for_agent_side_management_names() -> None:
    fresh = _normalized_text(FRESH_DEVELOPMENT_INSTALL)

    assert "/etc/hosts" in fresh
    assert "NAS and every GPU node" in fresh
    assert "enrollment, agent, and registry names" in fresh
    assert "never add the Tailscale browser name" in fresh


def test_security_and_recovery_docs_split_network_and_application_authority() -> None:
    threat = _normalized_text(THREAT_MODEL)
    recovery = _normalized_text(CONTROL_RECOVERY)

    for required in (
        "tailnet reachability and Vonk Forge authentication are independent gates",
        "administrator verifier",
        "opaque session digests",
        "Tailscale OAuth",
    ):
        assert required in threat
    for required in (
        "OAuth compromise",
        "Tailscale state loss",
        "administrator password loss",
        "break-glass loopback",
    ):
        assert required in recovery
    assert "--rotate-tailscale-oauth" in recovery
    assert "--tailscale-oauth-rotation-id" in recovery
    assert "hash-only sidecar receipt history" in recovery
    assert "Stale UUIDs" in recovery
    assert "trusted offline operator boundary" in threat


def test_threat_model_documents_helper_network_observation_boundary() -> None:
    threat = _normalized_text(THREAT_MODEL)

    for required in (
        "observes host interface addresses through `AF_NETLINK`",
        "`IPAddressDeny=any`",
        "no `AF_INET` or `AF_INET6` sockets",
        "read-only iptables policy verification requires `CAP_NET_ADMIN`",
    ):
        assert required in threat


def test_development_updates_do_not_replace_the_production_trust_boundary() -> None:
    combined = " ".join(
        _normalized_text(path)
        for path in (README, COMPOSE_README, DEV_NAS, CONTROL_RECOVERY)
    )

    assert "Pull then Redeploy" in combined
    assert "mutable `:dev`" in combined
    assert "production" in combined
    assert "digest-pinned" in combined
    assert "host updater" in combined or "host-updater" in combined


def test_quick_start_does_not_present_a_tunnel_as_normal_browser_access() -> None:
    quick_start = _section(README, "Quick start")
    fresh_before_acceptance = re.split(
        r"^## (?:\d+\. )?Prove the installation$",
        FRESH_DEVELOPMENT_INSTALL.read_text(),
        maxsplit=1,
        flags=re.MULTILINE,
    )[0]

    forbidden = (
        "use an SSH tunnel to access the UI",
        "open an SSH tunnel for the UI",
        "keep this tunnel open to use the UI",
        "SSH forwarding is the normal operator path",
    )
    for text in (quick_start, fresh_before_acceptance):
        assert "ssh -N" not in text
        assert "-L 18080:127.0.0.1:8080" not in text
        for claim in forbidden:
            assert claim not in text
