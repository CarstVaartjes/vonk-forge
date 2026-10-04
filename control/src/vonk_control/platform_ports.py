"""Ports the platform, not a recipe, decides for a multi-Spark run.

A two-Spark workload shares the host network, so its engine is not behind a
Docker publication. The root-owned Spark firewall written at enrollment names
the only host port such an engine may serve on, and the only rendezvous port
the peers may reach. Both are fixed site constants, so the Controller owns the
choice: a recipe's declared port describes the engine in isolation and is only
honoured where Docker maps it, on a single Spark. The choice is compiled into
the installed plan (endpoint and engine command), because the agent compares
that plan with every start request.
"""

from __future__ import annotations

# Mirrors the Spark firewall's VONK_RENDEZVOUS_PORT and VONK_HOST_ENDPOINT_PORTS.
RENDEZVOUS_PORT = 29500
HOST_ENDPOINT_PORT = 8888


def serving_port(declared: int, *, node_count: int) -> int:
    """The port the endpoint owner serves on for this node count."""

    return HOST_ENDPOINT_PORT if node_count > 1 else declared


def rebind_port_argument(command: list[str], declared: int, port: int) -> list[str]:
    """Point an explicit ``--port <declared>`` in an engine command at ``port``."""

    return [
        str(port)
        if value == str(declared) and index and command[index - 1] == "--port"
        else value
        for index, value in enumerate(command)
    ]
