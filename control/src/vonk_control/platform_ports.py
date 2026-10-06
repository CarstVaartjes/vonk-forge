"""Host ports the platform, not a recipe, decides for every run.

The root-owned Spark firewall written at enrollment authorises a fixed set of
host ports, so a recipe's declared port (the engine's own default) cannot be
used on the host as it is:

* A single-Spark endpoint is published by Docker. The Controller allocates one
  of the firewall's endpoint host ports for the run and the recipe's own port
  stays the container port (``-p host:container``).
* A multi-Spark workload shares the host network, so there is no publication
  to map: the engine itself must listen on the firewall's host endpoint port,
  and the Controller compiles that port into the installed plan (endpoint and
  engine command), because the agent compares that plan with every start.

The ports live in ``resources/site-ports.json``, which the Spark setup program
compiles into the firewall configuration it writes. This module reads the same
file, so the two cannot drift.
"""

from __future__ import annotations

from importlib.resources import files

from vonk_agent_protocol.installer_setup import SitePorts


def _site_ports() -> SitePorts:
    return SitePorts.model_validate_json(
        files("vonk_control.resources")
        .joinpath("site-ports.json")
        .read_text(encoding="utf-8")
    )


_PORTS = _site_ports()
# Authorised Docker-published endpoint host ports, in allocation order.
ENDPOINT_HOST_PORTS = tuple(_PORTS.endpoint_host_ports)
# The one host port a host-networked (multi-Spark) endpoint may listen on.
HOST_ENDPOINT_PORT = _PORTS.host_endpoint_ports[0]
RENDEZVOUS_PORT = _PORTS.rendezvous_port


def serving_port(declared: int, *, node_count: int) -> int:
    """The port a multi-node engine listens on, or the declared container port."""

    return HOST_ENDPOINT_PORT if node_count > 1 else declared


def service_host_port_candidates(declared: int, *, node_count: int) -> tuple[int, ...]:
    """Host ports a run's endpoint may take, in allocation order.

    A multi-node run listens on the host network, so its candidate is the one
    firewall-authorised port. A single-node run is published by Docker and may
    take any authorised endpoint host port; ``declared`` is only its
    container port.
    """

    if node_count > 1:
        return (serving_port(declared, node_count=node_count),)
    return ENDPOINT_HOST_PORTS


def rebind_port_argument(command: list[str], declared: int, port: int) -> list[str]:
    """Point an explicit ``--port <declared>`` in an engine command at ``port``."""

    return [
        str(port)
        if value == str(declared) and index and command[index - 1] == "--port"
        else value
        for index, value in enumerate(command)
    ]
