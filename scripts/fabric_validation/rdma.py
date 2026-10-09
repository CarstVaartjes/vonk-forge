"""Rdma for fabric validation."""

from __future__ import annotations

import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .common import (
    PHYSICAL_LINK_MIN_GBPS,
    READ_FUNCTION_MIN_GBPS,
    WRITE_FUNCTION_MIN_GBPS,
    GateError,
    Host,
    Rail,
)
from .parsers import parse_rdma
from .runner import Runner


def perftest_command(
    tool: str,
    rail: Rail,
    peer_ip: str,
    port: int,
    *,
    server: bool,
    duration_seconds: int | None = None,
) -> str:
    run_length = (
        f"--duration {duration_seconds}"
        if duration_seconds is not None
        else "--iters 5000"
    )
    base = (
        f"/usr/bin/{tool} -d {shlex.quote(rail.hca)} -i 1 -x {rail.gid_index} -p {port} "
        f"-F --report_gbits --size 65536 {run_length}"
    )
    return base if server else f"{base} {shlex.quote(peer_ip)}"


def run_one_rdma(
    runner: Runner,
    server_host: Host,
    client_host: Host,
    server_rail: Rail,
    client_rail: Rail,
    tool: str,
    port: int,
    *,
    minimum_bandwidth_gbps: float = 0.0,
    duration_seconds: int | None = None,
) -> dict[str, Any]:
    label = f"{tool}:{client_host.name}->{server_host.name}:{server_rail.name}"
    server_log = f"/tmp/validate-fabric-{tool}-{port}.log"
    server_status = f"/tmp/validate-fabric-{tool}-{port}.status"
    server_body = (
        f'{perftest_command(tool, server_rail, "", port, server=True, duration_seconds=duration_seconds)} > "$1" 2>&1; '
        'exit_code=$?; printf "%s\\n" "$exit_code" > "$2"; exit "$exit_code"'
    )
    server_command = (
        f"rm -f {server_log} {server_status}; nohup bash -c {shlex.quote(server_body)} "
        f"validate-fabric-perftest {shlex.quote(server_log)} {shlex.quote(server_status)} "
        "</dev/null >/dev/null 2>&1 & echo $!"
    )

    def call_on(
        host: Host, command: str, *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if host is runner.head:
            return runner.remote(host.ssh_alias, command, check=check)
        return runner.worker_via_fabric(command, check=check)

    server_call = lambda command, check=True: call_on(server_host, command, check=check)
    client_call = lambda command, check=True: call_on(client_host, command, check=check)
    server_pid = server_call(server_command).stdout.strip()
    if not server_pid.isdigit():
        raise GateError(f"{label} did not return a perftest server PID")
    collect_server = f"""set -u
for _ in $(seq 1 300); do
  [ -s {shlex.quote(server_status)} ] && break
  kill -0 {server_pid} 2>/dev/null || break
  sleep 0.1
done
if [ ! -s {shlex.quote(server_status)} ]; then
  kill {server_pid} 2>/dev/null || true
  cat {shlex.quote(server_log)} 2>/dev/null || true
  rm -f {shlex.quote(server_log)} {shlex.quote(server_status)}
  exit 124
fi
exit_code="$(cat {shlex.quote(server_status)})"
cat {shlex.quote(server_log)}
rm -f {shlex.quote(server_log)} {shlex.quote(server_status)}
case "$exit_code" in
  ''|*[!0-9]*) exit 125 ;;
esac
exit "$exit_code"
    """
    try:
        time.sleep(1)
        started_monotonic = time.monotonic()
        client = client_call(
            perftest_command(
                tool,
                client_rail,
                server_rail.fabric_ip,
                port,
                server=False,
                duration_seconds=duration_seconds,
            ),
            check=False,
        )
        finished_monotonic = time.monotonic()
        server = server_call(collect_server, check=False)
    except BaseException:
        server_call(
            f"kill {server_pid} 2>/dev/null || true; rm -f {server_log} {server_status}",
            check=False,
        )
        raise
    if client.returncode:
        raise GateError(f"{label} client exited {client.returncode}")
    if server.returncode:
        raise GateError(f"{label} server exited {server.returncode}")
    parsed = parse_rdma(
        client.stdout
        + "\n"
        + client.stderr
        + "\n"
        + server.stdout
        + "\n"
        + server.stderr
    )
    if not parsed.passed:
        raise GateError(f"{label}: {parsed.reason}")
    if parsed.bandwidth_gbps is None or parsed.bandwidth_gbps < minimum_bandwidth_gbps:
        raise GateError(
            f"{label} bandwidth {parsed.bandwidth_gbps or 0.0:.2f} Gb/s "
            f"is below {minimum_bandwidth_gbps:.2f} Gb/s"
        )
    return {
        "name": label,
        "passed": True,
        "bandwidth_gbps": parsed.bandwidth_gbps,
        "minimum_bandwidth_gbps": minimum_bandwidth_gbps,
        "started_monotonic": started_monotonic,
        "finished_monotonic": finished_monotonic,
        "client_exit_code": client.returncode,
        "server_exit_code": server.returncode,
    }


def run_aggregate_rdma_write(
    runner: Runner,
    server_host: Host,
    client_host: Host,
    *,
    base_port: int,
    run_component=run_one_rdma,
) -> dict[str, Any]:
    """Run both RoCE functions concurrently and enforce physical-link bandwidth."""
    pairs = list(zip(server_host.rails, client_host.rails, strict=True))
    if len(pairs) != 2:
        raise GateError("aggregate RDMA requires exactly two RoCE functions")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                run_component,
                runner,
                server_host,
                client_host,
                server_rail,
                client_rail,
                "ib_write_bw",
                base_port + index,
                minimum_bandwidth_gbps=0.0,
                duration_seconds=5,
            )
            for index, (server_rail, client_rail) in enumerate(pairs)
        ]
        components = [future.result() for future in futures]
    if len(components) != 2 or any(
        not component.get("passed") for component in components
    ):
        raise GateError("aggregate RDMA requires two successful component results")
    overlap_seconds = min(
        component["finished_monotonic"] for component in components
    ) - max(component["started_monotonic"] for component in components)
    if overlap_seconds <= 0:
        raise GateError("aggregate RDMA component intervals did not overlap")
    aggregate = sum(float(component["bandwidth_gbps"]) for component in components)
    if aggregate < PHYSICAL_LINK_MIN_GBPS:
        raise GateError(
            f"aggregate RDMA write {client_host.name}->{server_host.name} "
            f"{aggregate:.2f} Gb/s is below {PHYSICAL_LINK_MIN_GBPS:.2f} Gb/s"
        )
    return {
        "name": f"ib_write_bw:aggregate:{client_host.name}->{server_host.name}",
        "passed": True,
        "aggregate_bandwidth_gbps": aggregate,
        "minimum_bandwidth_gbps": PHYSICAL_LINK_MIN_GBPS,
        "overlap_seconds": overlap_seconds,
        "components": components,
    }


def run_rdma(runner: Runner, head: Host, worker: Host) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    port = 12000
    for tool in ("ib_write_bw", "ib_read_bw"):
        minimum = (
            WRITE_FUNCTION_MIN_GBPS if tool == "ib_write_bw" else READ_FUNCTION_MIN_GBPS
        )
        for head_rail, worker_rail in zip(head.rails, worker.rails, strict=True):
            results.append(
                run_one_rdma(
                    runner,
                    worker,
                    head,
                    worker_rail,
                    head_rail,
                    tool,
                    port,
                    minimum_bandwidth_gbps=minimum,
                )
            )
            port += 1
            results.append(
                run_one_rdma(
                    runner,
                    head,
                    worker,
                    head_rail,
                    worker_rail,
                    tool,
                    port,
                    minimum_bandwidth_gbps=minimum,
                )
            )
            port += 1
    results.append(run_aggregate_rdma_write(runner, worker, head, base_port=13000))
    results.append(run_aggregate_rdma_write(runner, head, worker, base_port=13100))
    return results
