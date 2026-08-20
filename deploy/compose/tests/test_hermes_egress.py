from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_hermes_has_no_general_egress_or_host_firewall_setup_step() -> None:
    compose = yaml.safe_load(
        (ROOT / "deploy/compose/hermes-agent/compose.yaml").read_text()
    )
    service = compose["services"]["hermes-agent"]

    assert set(service["networks"]) == {
        "hermes-inference",
        "tailnet-hermes-edge",
    }
    assert all(compose["networks"][name]["internal"] is True for name in service["networks"])
    assert not (ROOT / "deploy/compose/bin/harden-hermes-egress").exists()
