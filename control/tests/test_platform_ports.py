"""A multi-Spark run serves on the port the Spark firewall authorises."""

import pytest
from vonk_control.platform_ports import (
    HOST_ENDPOINT_PORT,
    RENDEZVOUS_PORT,
    serving_port,
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
def test_a_single_spark_run_keeps_the_declared_port(declared: int) -> None:
    demand = run_port_demand(_document(declared), node_count=1, endpoint_owner=True)

    assert demand.plan_port == declared
    assert demand.required_ports == (declared,)
    assert serving_port(declared, node_count=1) == declared
