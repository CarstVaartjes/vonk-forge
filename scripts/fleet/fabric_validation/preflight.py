"""Preflight for fabric validation."""

from __future__ import annotations

import shlex

from .common import Host
from .runner import Runner


def remote_preflight(runner: Runner, host: Host, *, via_fabric: bool) -> None:
    command = """
set -euo pipefail
test -x /usr/bin/ib_write_bw
test -x /usr/bin/ib_read_bw
test -x /usr/bin/ib_write_lat
test -x /usr/bin/ibv_devinfo
command -v rdma >/dev/null
"""
    for rail in host.rails:
        command += (
            f'test "$(cat /sys/class/net/{shlex.quote(rail.interface)}/speed)" = 200000\n'
            f'test "$(cat /sys/class/net/{shlex.quote(rail.interface)}/mtu)" = 1500\n'
            f"test -r /sys/class/infiniband/{shlex.quote(rail.hca)}/ports/1/gids/{rail.gid_index}\n"
            f"test -r /sys/class/infiniband/{shlex.quote(rail.hca)}/ports/1/gid_attrs/ndevs/{rail.gid_index}\n"
            f'test "$(cat /sys/class/infiniband/{shlex.quote(rail.hca)}/ports/1/gid_attrs/ndevs/{rail.gid_index})" = {shlex.quote(rail.interface)}\n'
            f'test -z "$(ip route show default dev {shlex.quote(rail.interface)})"\n'
            f"timeout 5 ping -n -I {shlex.quote(rail.interface)} -c 1 -W 2 {shlex.quote(rail.peer_ip)}\n"
        )
    if via_fabric:
        runner.worker_via_fabric(command)
    else:
        runner.remote(host.ssh_alias, command)
