"""A multi-Spark run serves on the port the Spark firewall authorises."""

import re
from pathlib import Path

import pytest
from vonk_control.platform_ports import (
    ENDPOINT_HOST_PORTS,
    HOST_ENDPOINT_PORT,
    RENDEZVOUS_PORT,
)
from vonk_control.run_admission import run_port_demand


def _document(port: int) -> dict[str, object]:
    return {"interfaces": [{"adapter": "openai", "port": port}]}


@pytest.mark.parametrize("declared", [8000, 8080, 30000, HOST_ENDPOINT_PORT])
def test_a_two_spark_run_is_admitted_on_the_firewall_host_endpoint_port(
    declared: int,
) -> None:
    owner = run_port_demand(_document(declared), node_count=2, endpoint_owner=True)
    worker = run_port_demand(_document(declared), node_count=2, endpoint_owner=False)

    assert owner.plan_port == worker.plan_port == HOST_ENDPOINT_PORT
    assert owner.required_ports == (HOST_ENDPOINT_PORT, RENDEZVOUS_PORT)
    assert worker.required_ports == (HOST_ENDPOINT_PORT,)


@pytest.mark.parametrize("declared", [8000, 30000])
def test_a_single_spark_run_may_take_any_authorised_endpoint_host_port(
    declared: int,
) -> None:
    demand = run_port_demand(_document(declared), node_count=1, endpoint_owner=True)

    assert demand.service_candidates == ENDPOINT_HOST_PORTS
    assert demand.plan_port == ENDPOINT_HOST_PORTS[0]
    assert demand.required_ports == (ENDPOINT_HOST_PORTS[0],)


def test_the_spark_setup_program_writes_the_ports_the_controller_allocates() -> None:
    """The firewall the Sparks enrol with authorises exactly these ports.

    The setup program renders its firewall configuration from the same
    ``site-ports.json`` this module reads, and its own tests pin that rendering
    to the literal below. Comparing the literal here catches either side being
    given a second source again. The Rust tree is absent from the Controller
    image, so the check only runs from a repository checkout.
    """

    source = (
        Path(__file__).resolve().parents[2]
        / "rust/crates/vonk-spark-setup/src/firewall.rs"
    )
    if not source.exists():
        pytest.skip("repository checkout required")
    rendered = re.search(
        r'CANONICAL_FIREWALL: &str = "[^"]*?VONK_ENDPOINT_HOST_PORTS=([0-9,]+)'
        r"\\nVONK_HOST_ENDPOINT_PORTS=([0-9,]+)\\nVONK_RENDEZVOUS_PORT=([0-9]+)",
        source.read_text(encoding="utf-8"),
    )
    assert rendered is not None
    endpoint, host_endpoint, rendezvous = rendered.groups()
    assert tuple(map(int, endpoint.split(","))) == ENDPOINT_HOST_PORTS
    assert tuple(map(int, host_endpoint.split(","))) == (HOST_ENDPOINT_PORT,)
    assert int(rendezvous) == RENDEZVOUS_PORT
