from pathlib import Path


def test_node_onboarding_runbook_covers_safe_resumable_workflow() -> None:
    text = " ".join(
        (Path(__file__).resolve().parents[2] / "docs/runbooks/node-onboarding.md")
        .read_text()
        .split()
    )
    for phrase in (
        "trusted first contact",
        "physical console",
        "--apply",
        "resume",
        "emit-record",
        "PostgreSQL enrollment is authoritative",
        "topology",
        "recovery",
        "does not modify Git",
        "add spark",
        "vonk-agent bootstrap",
        "manual `agent.toml` editing is unsupported",
    ):
        assert phrase.lower() in text.lower()
    assert "not an operator command currently available" not in text.lower()
