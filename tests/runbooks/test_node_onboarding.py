from pathlib import Path


def test_node_onboarding_runbook_covers_safe_resumable_workflow() -> None:
    text = (Path(__file__).resolve().parents[2] / "docs/runbooks/node-onboarding.md").read_text()
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
        "next implementation step",
        "not an operator command currently available",
        "manual `agent.toml` editing is unsupported",
    ):
        assert phrase.lower() in text.lower()
    assert "generated bootstrap command" not in text.lower()
