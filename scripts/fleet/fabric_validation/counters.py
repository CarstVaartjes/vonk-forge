"""Counters for fabric validation."""

from __future__ import annotations

from .common import Host
from .parsers import parse_rdma_counters
from .runner import Runner


def capture_rdma_counters(runner: Runner, head: Host, worker: Host) -> dict[str, int]:
    """Capture monitored counters worker-first for both active RoCE functions."""
    command = "/usr/bin/rdma statistic show"
    worker_result = runner.worker_via_fabric(command)
    head_result = runner.remote(head.ssh_alias, command)
    snapshot: dict[str, int] = {}
    for host, result in ((worker, worker_result), (head, head_result)):
        counters = parse_rdma_counters(
            result.stdout,
            expected_hcas=tuple(function.hca for function in host.rails),
        )
        snapshot.update(
            {f"{host.name}/{key}": value for key, value in counters.items()}
        )
    return dict(sorted(snapshot.items()))
