from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _normalized(relative: str) -> str:
    return " ".join((ROOT / relative).read_text().split())


def test_install_runbook_requires_registration_boundary_without_live_bootstrap_command() -> None:
    text = _normalized("docs/operations/install-vonk-agent.md").lower()

    for required in (
        "registration is the authority",
        "add spark",
        "next implementation step",
        "not an operator command currently available",
        "manual `agent.toml` editing is unsupported",
    ):
        assert required in text
    assert "sudoedit /etc/vonk-forge-agent/agent.toml" not in text
    assert "generated bootstrap command" not in text


def test_fresh_install_runbook_stops_at_the_registration_boundary() -> None:
    text = _normalized("docs/runbooks/fresh-development-install.md").lower()

    for required in (
        "registration is the authority",
        "add spark",
        "next implementation step",
        "not an operator command currently available",
        "manual `agent.toml` editing is unsupported",
    ):
        assert required in text
    assert "Set the two explicit `:8443` HTTPS origins" not in text
    assert "/etc/vonk-forge-agent/agent.toml" not in text
    assert "generated bootstrap command" not in text


def test_development_agent_workloads_runbook_drops_manual_agent_toml_steps() -> None:
    text = _normalized("docs/runbooks/development-agent-workloads.md").lower()

    for required in (
        "registration is the authority",
        "add spark",
        "next implementation step",
        "not an operator command currently available",
        "manual `agent.toml` editing is unsupported",
    ):
        assert required in text
    assert "/etc/vonk-forge-agent/agent.toml" not in text
    assert "generated bootstrap command" not in text
