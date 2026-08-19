from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _normalized(relative: str) -> str:
    return " ".join((ROOT / relative).read_text().split())


def test_install_runbook_documents_the_fleet_bootstrap_boundary() -> None:
    text = _normalized("docs/operations/install-vonk-agent.md").lower()

    for required in (
        "registration is the authority",
        "add spark",
        "/var/lib/vonk-forge/supervisor/current/vonk-agent bootstrap",
        "--controller-endpoint",
        "--enrollment-endpoint",
        "--ca-fingerprint",
        "manual `agent.toml` editing is unsupported",
        "sudo vonk-agent-upgrade",
    ):
        assert required in text
    assert "sudoedit /etc/vonk-forge-agent/agent.toml" not in text
    assert "not an operator command currently available" not in text


def test_fresh_install_runbook_documents_the_fleet_bootstrap_boundary() -> None:
    text = _normalized("docs/runbooks/fresh-development-install.md").lower()

    for required in (
        "registration is the authority",
        "add spark",
        "/var/lib/vonk-forge/supervisor/current/vonk-agent bootstrap",
        "manual `agent.toml` editing is unsupported",
        "sudo vonk-agent-upgrade",
    ):
        assert required in text
    assert "not an operator command currently available" not in text


def test_development_agent_workloads_runbook_documents_bootstrap_without_manual_toml() -> None:
    text = _normalized("docs/runbooks/development-agent-workloads.md").lower()

    for required in (
        "registration is the authority",
        "add spark",
        "/var/lib/vonk-forge/supervisor/current/vonk-agent bootstrap",
        "manual `agent.toml` editing is unsupported",
    ):
        assert required in text
    assert "not an operator command currently available" not in text
