"""Latency for fabric validation."""

from __future__ import annotations

import dataclasses
import shlex
import subprocess
import time
from typing import Any

from .common import LATENCY_ITERATIONS, LATENCY_MESSAGE_BYTES, GateError, Host, Rail
from .parsers import parse_rdma_latency
from .runner import Runner


def latency_command(rail: Rail, peer_ip: str, port: int, *, server: bool) -> str:
    """Render the immutable write-latency baseline command."""
    base = (
        f"/usr/bin/ib_write_lat -d {shlex.quote(rail.hca)} -i 1 -x {rail.gid_index} -p {port} "
        f"-F --size {LATENCY_MESSAGE_BYTES} --iters {LATENCY_ITERATIONS}"
    )
    return base if server else f"{base} {shlex.quote(peer_ip)}"


def run_one_rdma_latency(
    runner: Runner,
    server_host: Host,
    client_host: Host,
    server_function: Rail,
    client_function: Rail,
    port: int,
) -> dict[str, Any]:
    """Run one fixed per-function latency distribution and verify both processes."""
    label = (
        f"ib_write_lat:{client_host.name}->{server_host.name}:{server_function.name}"
    )
    server_log = f"/tmp/validate-fabric-ib_write_lat-{port}.log"
    server_status = f"/tmp/validate-fabric-ib_write_lat-{port}.status"
    server_body = (
        f'{latency_command(server_function, "", port, server=True)} > "$1" 2>&1; '
        'exit_code=$?; printf "%s\n" "$exit_code" > "$2"; exit "$exit_code"'
    )
    server_command = (
        f"rm -f {server_log} {server_status}; nohup bash -c {shlex.quote(server_body)} "
        f"validate-fabric-latency {shlex.quote(server_log)} {shlex.quote(server_status)} "
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
        client = client_call(
            latency_command(
                client_function, server_function.fabric_ip, port, server=False
            ),
            check=False,
        )
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
    parsed = parse_rdma_latency(client.stdout + "\n" + client.stderr)
    if not parsed.passed:
        raise GateError(f"{label}: {parsed.reason}")
    metrics = dataclasses.asdict(parsed)
    metrics.pop("passed")
    metrics.pop("reason")
    return {
        "name": label,
        "passed": True,
        **metrics,
        "client_exit_code": client.returncode,
        "server_exit_code": server.returncode,
    }


def run_rdma_latency(runner: Runner, head: Host, worker: Host) -> list[dict[str, Any]]:
    """Record fixed latency distributions on both functions and directions."""
    results: list[dict[str, Any]] = []
    port = 14000
    for head_function, worker_function in zip(head.rails, worker.rails, strict=True):
        results.append(
            run_one_rdma_latency(
                runner, worker, head, worker_function, head_function, port
            )
        )
        port += 1
        results.append(
            run_one_rdma_latency(
                runner, head, worker, head_function, worker_function, port
            )
        )
        port += 1
    return results
