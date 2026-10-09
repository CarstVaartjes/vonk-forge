"""Inventory for fabric validation."""

from __future__ import annotations

import tomllib
from pathlib import Path

from .common import GateError, Host, Rail


def load_hosts(inventory_path: Path) -> tuple[Host, Host]:
    with inventory_path.open("rb") as handle:
        inventory = tomllib.load(handle)
    hosts: list[Host] = []
    for name in ("node1", "node2"):
        raw = inventory.get("hosts", {}).get(name)
        if not isinstance(raw, dict):
            raise GateError(f"inventory has no hosts.{name}")
        fabric = raw.get("fabric")
        if not isinstance(fabric, dict):
            raise GateError(f"inventory has no hosts.{name}.fabric")
        rails: list[Rail] = []
        for function_name, function in sorted(
            (key, value) for key, value in fabric.items() if key.startswith("function")
        ):
            if not isinstance(function, dict):
                raise GateError(
                    f"inventory hosts.{name}.fabric.{function_name} is not a table"
                )
            required = ("interface", "hca", "gid_index", "fabric_ip", "peer_ip")
            missing = [key for key in required if key not in function]
            if missing:
                raise GateError(
                    f"inventory hosts.{name}.fabric.{function_name} missing {', '.join(missing)}"
                )
            rails.append(
                Rail(function_name, **{key: function[key] for key in required})
            )
        if len(rails) != 2:
            raise GateError(
                f"inventory hosts.{name} must describe exactly two fabric functions"
            )
        hosts.append(Host(name, raw["ssh_alias"], fabric, tuple(rails)))
    return hosts[0], hosts[1]


def validate_consumers(head: Host, worker: Host) -> None:
    """Reject a stale inventory before it can select different HCAs/GIDs."""
    if [rail.name for rail in head.rails] != [rail.name for rail in worker.rails]:
        raise GateError("GPU node fabric-function names do not match")
    for left, right in zip(head.rails, worker.rails, strict=True):
        if (left.interface, left.hca, left.gid_index) != (
            right.interface,
            right.hca,
            right.gid_index,
        ):
            raise GateError(f"mismatched HCA/GID consumers on {left.name}")
        if left.peer_ip != right.fabric_ip or right.peer_ip != left.fabric_ip:
            raise GateError(f"mismatched fabric peer IPs on {left.name}")
    interfaces = ",".join(rail.interface for rail in head.rails)
    hcas = ",".join(f"{rail.hca}:1" for rail in head.rails)
    expected = {
        "NCCL_SOCKET_IFNAME": f"={interfaces}",
        "NCCL_IB_HCA": f"={hcas}",
        "NCCL_IB_GID_INDEX": head.rails[0].gid_index,
        "TP_SOCKET_IFNAME": interfaces,
        "GLOO_SOCKET_IFNAME": interfaces,
    }
    for host in (head, worker):
        for variable, value in expected.items():
            if host.fabric.get(variable) != value:
                raise GateError(
                    f"{host.name} {variable} does not match the two recorded functions"
                )
        if any(rail.gid_index != expected["NCCL_IB_GID_INDEX"] for rail in host.rails):
            raise GateError(f"{host.name} uses different GID indices across functions")
