#!/usr/bin/env python3
"""Fail-closed acceptance checks for the one-link/two-function Vonk Forge GPU node fabric.

The script deliberately uses the head's ``vonk-node-2-fabric`` SSH alias for
every Node-1-to-node-2 action.  It never enables agent forwarding and never
copies a private key.  NCCL is built natively from pinned NVIDIA sources only
as a documented prerequisite; this validator verifies the completed artifacts
worker-first before the live fabric gates.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shlex
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

repository_src = Path(__file__).resolve().parents[1] / "src"
if str(repository_src) not in sys.path:
    sys.path.insert(0, str(repository_src))

from cluster_profiles.ssh_transport import select_transport_binary

NCCL_VERSION = "v2.30.7-1"
NCCL_COMMIT = "73cf112295c33aee2b895f329f592f2a9b4b0f97"
NCCL_TESTS_COMMIT = "a0b82b2260cf5152b9f8c061bbf7eaf0ba096432"
CUDA_NVCC = "/usr/local/cuda/bin/nvcc"
MPI_HOME = "/usr/lib/aarch64-linux-gnu/openmpi"
FABRIC_WORKER_ALIAS = "vonk-node-2-fabric"
NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ForwardAgent=no", "-o", "ConnectTimeout=10")
PHYSICAL_LINK_MIN_GBPS = 184.0
WRITE_FUNCTION_MIN_GBPS = 98.01
READ_FUNCTION_MIN_GBPS = 72.37
NCCL_MIN_GB_PER_SECOND = 17.44
LATENCY_MESSAGE_BYTES = 8
LATENCY_ITERATIONS = 10000
RDMA_ERROR_COUNTERS = (
    "out_of_buffer",
    "out_of_sequence",
    "duplicate_request",
    "rnr_nak_retry_err",
    "packet_seq_err",
    "implied_nak_seq_err",
    "local_ack_timeout_err",
    "resp_local_length_error",
    "resp_cqe_error",
    "req_cqe_error",
    "req_remote_invalid_request",
    "req_remote_access_errors",
    "resp_remote_access_errors",
    "resp_cqe_flush_error",
    "req_cqe_flush_error",
    "req_transport_retries_exceeded",
    "req_rnr_retries_exceeded",
    "roce_adp_retrans",
    "roce_adp_retrans_to",
)


class GateError(RuntimeError):
    """A failed acceptance gate; no later live gate may run."""


@dataclasses.dataclass(frozen=True)
class NCCLResult:
    passed: bool
    transport: str | None
    bus_bandwidth_gbps: float | None
    reason: str | None = None


@dataclasses.dataclass(frozen=True)
class RDMAResult:
    passed: bool
    bandwidth_gbps: float | None
    reason: str | None = None


@dataclasses.dataclass(frozen=True)
class RDMALatencyResult:
    passed: bool
    message_bytes: int | None = None
    iterations: int | None = None
    minimum_usec: float | None = None
    maximum_usec: float | None = None
    typical_usec: float | None = None
    average_usec: float | None = None
    standard_deviation_usec: float | None = None
    p99_usec: float | None = None
    p999_usec: float | None = None
    reason: str | None = None


@dataclasses.dataclass(frozen=True)
class Rail:
    name: str
    interface: str
    hca: str
    gid_index: int
    fabric_ip: str
    peer_ip: str


@dataclasses.dataclass(frozen=True)
class Host:
    name: str
    ssh_alias: str
    fabric: dict[str, Any]
    rails: tuple[Rail, ...]


def parse_nccl(output: str) -> NCCLResult:
    """Parse NCCL diagnostics and reject socket fallback or absent bandwidth."""
    socket_selected = bool(re.search(r"NET/Socket\b", output, re.IGNORECASE))
    ib_selected = bool(re.search(r"NET/IB\s*:\s*Using\b", output, re.IGNORECASE))
    transport = "Socket" if socket_selected else "IB" if ib_selected else None

    averages = [
        float(match)
        for match in re.findall(
            r"Avg\s+bus\s+bandwidth\s*:\s*([0-9]+(?:\.[0-9]+)?)",
            output,
            re.IGNORECASE,
        )
    ]
    # Standard nccl-tests rows contain: bytes, iterations, type, op, time,
    # algbw, busbw, error, ... . Prefer the suite's final average when present;
    # row values are retained only for older output without that summary.
    row = re.compile(
        r"^\s*\d+\s+\d+\s+\S+\s+\S+\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s+([-+0-9.eE]+)\b",
        re.MULTILINE,
    )
    row_values = [float(match) for match in row.findall(output)]
    bandwidth = averages[-1] if averages else max(row_values) if row_values else None

    if socket_selected:
        return NCCLResult(False, transport, bandwidth, "NCCL selected NET/Socket")
    if not ib_selected:
        return NCCLResult(False, transport, bandwidth, "NCCL did not report NET/IB : Using")
    if bandwidth is None or bandwidth <= 0:
        return NCCLResult(False, transport, bandwidth, "NCCL reported no positive bus bandwidth")
    return NCCLResult(True, transport, bandwidth)


def selected_nccl_hcas(output: str) -> set[str]:
    """Return HCA names from NCCL's actual NET/IB selection diagnostics."""
    selected: set[str] = set()
    for line in output.splitlines():
        if not re.search(r"NET/IB\s*:\s*Using\b", line, re.IGNORECASE):
            continue
        selected.update(re.findall(r"\broce[A-Za-z0-9]+\b", line))
    return selected


def parse_rdma(output: str) -> RDMAResult:
    """Require an IB/RoCE perftest with a positive average Gb/s measurement."""
    if not re.search(r"Transport type\s*:\s*IB\b", output, re.IGNORECASE):
        return RDMAResult(False, None, "perftest did not report IB transport")
    if not re.search(r"Link type\s*:\s*Ethernet\b", output, re.IGNORECASE):
        return RDMAResult(False, None, "perftest did not report Ethernet/RoCE link")
    if not re.search(r"Mtu\s*:\s*1024\[B\]", output, re.IGNORECASE):
        return RDMAResult(False, None, "perftest did not report 1024-byte RoCE MTU")
    if not re.search(r"GID index\s*:\s*3\b", output, re.IGNORECASE):
        return RDMAResult(False, None, "perftest did not report GID index 3")
    rows = re.compile(r"^\s*\d+\s+\d+\s+[-+0-9.eE]+\s+([-+0-9.eE]+)\s+[-+0-9.eE]+\s*$", re.MULTILINE)
    values = [float(value) for value in rows.findall(output)]
    bandwidth = max(values) if values else None
    if bandwidth is None or bandwidth <= 0:
        return RDMAResult(False, bandwidth, "perftest reported no positive average bandwidth")
    return RDMAResult(True, bandwidth)


def parse_rdma_latency(output: str) -> RDMALatencyResult:
    """Parse the fixed RoCE write-latency distribution in microseconds."""
    if not re.search(r"Transport type\s*:\s*IB\b", output, re.IGNORECASE):
        return RDMALatencyResult(False, reason="latency test did not report IB transport")
    if not re.search(r"Link type\s*:\s*Ethernet\b", output, re.IGNORECASE):
        return RDMALatencyResult(False, reason="latency test did not report Ethernet/RoCE link")
    if not re.search(r"Mtu\s*:\s*1024\[B\]", output, re.IGNORECASE):
        return RDMALatencyResult(False, reason="latency test did not report 1024-byte RoCE MTU")
    if not re.search(r"GID index\s*:\s*3\b", output, re.IGNORECASE):
        return RDMALatencyResult(False, reason="latency test did not report GID index 3")
    row = re.compile(
        r"^\s*(\d+)\s+(\d+)\s+"
        r"([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+"
        r"([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*$",
        re.MULTILINE,
    )
    match = row.search(output)
    if match is None:
        return RDMALatencyResult(False, reason="latency test did not report a distribution row")
    message_bytes, iterations = (int(match.group(1)), int(match.group(2)))
    if message_bytes != LATENCY_MESSAGE_BYTES or iterations != LATENCY_ITERATIONS:
        return RDMALatencyResult(False, reason="latency test parameters did not match the fixed baseline")
    metrics = [float(match.group(index)) for index in range(3, 10)]
    return RDMALatencyResult(True, message_bytes, iterations, *metrics)


def parse_rdma_counters(
    output: str,
    *,
    expected_hcas: tuple[str, ...],
    monitored_counters: tuple[str, ...] = RDMA_ERROR_COUNTERS,
) -> dict[str, int]:
    """Extract every monitored error counter for the active RoCE functions."""
    by_hca: dict[str, dict[str, int]] = {}
    for line in output.splitlines():
        match = re.match(r"^link\s+(\S+)/\d+\s+(.+)$", line.strip())
        if match is None or match.group(1) not in expected_hcas:
            continue
        tokens = match.group(2).split()
        if len(tokens) % 2:
            raise GateError(f"malformed RDMA counter row for {match.group(1)}")
        try:
            by_hca[match.group(1)] = {
                tokens[index]: int(tokens[index + 1]) for index in range(0, len(tokens), 2)
            }
        except ValueError as error:
            raise GateError(f"non-integer RDMA counter for {match.group(1)}") from error
    result: dict[str, int] = {}
    for hca in expected_hcas:
        counters = by_hca.get(hca)
        if counters is None:
            raise GateError(f"RDMA statistics missing active HCA {hca}")
        for counter in monitored_counters:
            if counter not in counters:
                raise GateError(f"RDMA statistics for {hca} missing {counter}")
            result[f"{hca}/{counter}"] = counters[counter]
    return dict(sorted(result.items()))


def validate_counter_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """Reject missing snapshots or any monitored error growth during acceptance."""
    if before.keys() != after.keys():
        missing = sorted(before.keys() ^ after.keys())
        raise GateError(f"RDMA counter snapshots differ: {', '.join(missing)}")
    deltas = {key: after[key] - before[key] for key in sorted(before)}
    for key, delta in deltas.items():
        if delta < 0:
            raise GateError(f"RDMA counter {key} decreased during the run")
        if delta > 0:
            raise GateError(f"RDMA counter {key} grew from {before[key]} to {after[key]}")
    return deltas


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
                raise GateError(f"inventory hosts.{name}.fabric.{function_name} is not a table")
            required = ("interface", "hca", "gid_index", "fabric_ip", "peer_ip")
            missing = [key for key in required if key not in function]
            if missing:
                raise GateError(f"inventory hosts.{name}.fabric.{function_name} missing {', '.join(missing)}")
            rails.append(Rail(function_name, **{key: function[key] for key in required}))
        if len(rails) != 2:
            raise GateError(f"inventory hosts.{name} must describe exactly two fabric functions")
        hosts.append(Host(name, raw["ssh_alias"], fabric, tuple(rails)))
    return hosts[0], hosts[1]


def validate_consumers(head: Host, worker: Host) -> None:
    """Reject a stale inventory before it can select different HCAs/GIDs."""
    if [rail.name for rail in head.rails] != [rail.name for rail in worker.rails]:
        raise GateError("GPU node fabric-function names do not match")
    for left, right in zip(head.rails, worker.rails, strict=True):
        if (left.interface, left.hca, left.gid_index) != (right.interface, right.hca, right.gid_index):
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
                raise GateError(f"{host.name} {variable} does not match the two recorded functions")
        if any(rail.gid_index != expected["NCCL_IB_GID_INDEX"] for rail in host.rails):
            raise GateError(f"{host.name} uses different GID indices across functions")


def command_record(command: list[str], completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "command": shlex.join(command),
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


class Runner:
    def __init__(self, head: Host, worker: Host, evidence: list[dict[str, Any]]):
        self.head = head
        self.worker = worker
        self.evidence = evidence
        self.ssh_bin = select_transport_binary("ssh")

    def local(
        self, command: list[str], *, check: bool = True, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(command, input=input_text, capture_output=True, text=True, check=False)
        self.evidence.append(command_record(command, completed))
        if check and completed.returncode:
            raise GateError(f"command failed ({completed.returncode}): {shlex.join(command)}")
        return completed

    def remote(self, host: str, shell_command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        # Supplying the body on stdin avoids OpenSSH's lossy joining of remote
        # argv and makes multi-line safety checks unambiguous.
        return self.local([self.ssh_bin, *SSH_OPTIONS, host, "bash", "-s"], check=check, input_text=shell_command)

    def worker_via_fabric(self, shell_command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        nested = "printf %s " + shlex.quote(shell_command) + " | ssh "
        nested += " ".join(shlex.quote(item) for item in (*SSH_OPTIONS, FABRIC_WORKER_ALIAS))
        nested += " bash -s"
        return self.remote(self.head.ssh_alias, nested, check=check)


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
            f"test \"$(cat /sys/class/net/{shlex.quote(rail.interface)}/speed)\" = 200000\n"
            f"test \"$(cat /sys/class/net/{shlex.quote(rail.interface)}/mtu)\" = 1500\n"
            f"test -r /sys/class/infiniband/{shlex.quote(rail.hca)}/ports/1/gids/{rail.gid_index}\n"
            f"test -r /sys/class/infiniband/{shlex.quote(rail.hca)}/ports/1/gid_attrs/ndevs/{rail.gid_index}\n"
            f"test \"$(cat /sys/class/infiniband/{shlex.quote(rail.hca)}/ports/1/gid_attrs/ndevs/{rail.gid_index})\" = {shlex.quote(rail.interface)}\n"
            f"test -z \"$(ip route show default dev {shlex.quote(rail.interface)})\"\n"
            f"timeout 5 ping -n -I {shlex.quote(rail.interface)} -c 1 -W 2 {shlex.quote(rail.peer_ip)}\n"
        )
    if via_fabric:
        runner.worker_via_fabric(command)
    else:
        runner.remote(host.ssh_alias, command)


def perftest_command(
    tool: str,
    rail: Rail,
    peer_ip: str,
    port: int,
    *,
    server: bool,
    duration_seconds: int | None = None,
) -> str:
    run_length = f"--duration {duration_seconds}" if duration_seconds is not None else "--iters 5000"
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
    def call_on(host: Host, command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
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
        server_call(f"kill {server_pid} 2>/dev/null || true; rm -f {server_log} {server_status}", check=False)
        raise
    if client.returncode:
        raise GateError(f"{label} client exited {client.returncode}")
    if server.returncode:
        raise GateError(f"{label} server exited {server.returncode}")
    parsed = parse_rdma(client.stdout + "\n" + client.stderr + "\n" + server.stdout + "\n" + server.stderr)
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
    if len(components) != 2 or any(not component.get("passed") for component in components):
        raise GateError("aggregate RDMA requires two successful component results")
    overlap_seconds = min(component["finished_monotonic"] for component in components) - max(
        component["started_monotonic"] for component in components
    )
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
        minimum = WRITE_FUNCTION_MIN_GBPS if tool == "ib_write_bw" else READ_FUNCTION_MIN_GBPS
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
    label = f"ib_write_lat:{client_host.name}->{server_host.name}:{server_function.name}"
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

    def call_on(host: Host, command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
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
            latency_command(client_function, server_function.fabric_ip, port, server=False),
            check=False,
        )
        server = server_call(collect_server, check=False)
    except BaseException:
        server_call(f"kill {server_pid} 2>/dev/null || true; rm -f {server_log} {server_status}", check=False)
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
            run_one_rdma_latency(runner, worker, head, worker_function, head_function, port)
        )
        port += 1
        results.append(
            run_one_rdma_latency(runner, head, worker, head_function, worker_function, port)
        )
        port += 1
    return results


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
        snapshot.update({f"{host.name}/{key}": value for key, value in counters.items()})
    return dict(sorted(snapshot.items()))


def nccl_prerequisite_command() -> str:
    """Read-only checks for the documented completed native NCCL build."""
    return f"""set -euo pipefail
check_completed_checkout() {{
  directory="$1" repository="$2" revision="$3"
  test -d "$directory"
  test ! -L "$directory"
  test -d "$directory/.git"
  test ! -L "$directory/.git"
  test "$(git -C "$directory" remote get-url origin)" = "$repository"
  test "$(git -C "$directory" rev-parse HEAD)" = "$revision"
}}
test -x {CUDA_NVCC}
{CUDA_NVCC} --version
command -v git >/dev/null
command -v mpirun >/dev/null
test "$(dpkg-query -W -f='${{db:Status-Status}} ${{Version}}' libopenmpi-dev)" = "installed 4.1.6-7ubuntu2"
test "$(dpkg-query -W -f='${{db:Status-Status}} ${{Version}}' openmpi-bin)" = "installed 4.1.6-7ubuntu2"
check_completed_checkout "$HOME/nccl" https://github.com/NVIDIA/nccl.git {NCCL_COMMIT}
check_completed_checkout "$HOME/nccl-tests" https://github.com/NVIDIA/nccl-tests.git {NCCL_TESTS_COMMIT}
test -r "$HOME/nccl/build/lib/libnccl.so"
test -x "$HOME/nccl-tests/build/all_reduce_perf"
"""


def nccl_launch_command(head: Host, worker: Host) -> str:
    """Launch a two-rank all-reduce only through the restricted fabric alias."""
    fabric = head.fabric
    exports = {
        "NCCL_DEBUG": "INFO",
        "NCCL_SOCKET_IFNAME": fabric["NCCL_SOCKET_IFNAME"],
        "NCCL_IB_HCA": fabric["NCCL_IB_HCA"],
        "NCCL_IB_GID_INDEX": str(fabric["NCCL_IB_GID_INDEX"]),
        "TP_SOCKET_IFNAME": fabric["TP_SOCKET_IFNAME"],
        "GLOO_SOCKET_IFNAME": fabric["GLOO_SOCKET_IFNAME"],
        "OMPI_MCA_oob_tcp_if_include": fabric["TP_SOCKET_IFNAME"],
        "OMPI_MCA_btl_tcp_if_include": fabric["TP_SOCKET_IFNAME"],
    }
    export_lines = "\n".join(f"export {key}='{value}'" for key, value in exports.items())
    x_args = " ".join(f"-x {key}" for key in exports)
    return f"""set -euo pipefail
export CUDA_HOME=/usr/local/cuda
export MPI_HOME={MPI_HOME}
export NCCL_HOME="$HOME/nccl/build"
export LD_LIBRARY_PATH="$NCCL_HOME/lib:$CUDA_HOME/lib64:$MPI_HOME/lib:${{LD_LIBRARY_PATH:-}}"
{export_lines}
test -x "$HOME/nccl-tests/build/all_reduce_perf"
mpirun -np 2 -H localhost:1,{FABRIC_WORKER_ALIAS}:1 \\
  --mca plm_rsh_agent "ssh -o BatchMode=yes -o ForwardAgent=no -o StrictHostKeyChecking=yes" \\
  {x_args} -x LD_LIBRARY_PATH \\
  "$HOME/nccl-tests/build/all_reduce_perf" -b 8M -e 1G -f 2 -g 1 -c 1
"""


def run_nccl(runner: Runner, head: Host, worker: Host) -> NCCLResult:
    # The worker prerequisite is deliberately first. No source staging, sudo,
    # agent forwarding, management-plane host list, or shared key is involved.
    runner.worker_via_fabric(nccl_prerequisite_command())
    runner.remote(head.ssh_alias, nccl_prerequisite_command())
    result = runner.remote(head.ssh_alias, nccl_launch_command(head, worker))
    output = result.stdout + "\n" + result.stderr
    parsed = parse_nccl(output)
    if not parsed.passed:
        raise GateError(parsed.reason or "NCCL all-reduce failed")
    if parsed.bus_bandwidth_gbps is None or parsed.bus_bandwidth_gbps < NCCL_MIN_GB_PER_SECOND:
        raise GateError(
            f"NCCL bus bandwidth {parsed.bus_bandwidth_gbps or 0.0:.2f} GB/s "
            f"is below {NCCL_MIN_GB_PER_SECOND:.2f} GB/s"
        )
    selected_hcas = selected_nccl_hcas(output)
    for function in head.rails:
        if function.hca not in selected_hcas:
            raise GateError(f"NCCL did not select {function.hca}")
    return parsed


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def run_preflights(runner: Runner, head: Host, worker: Host) -> None:
    """Check the worker first at every remote prerequisite boundary."""
    remote_preflight(runner, worker, via_fabric=True)
    remote_preflight(runner, head, via_fabric=False)


def validate_expected_nodes(
    values: list[str], head: Host, worker: Host
) -> dict[str, str]:
    """Bind two selected Fleet IDs to the inventory's ordered SSH aliases."""

    selected: dict[str, str] = {}
    for value in values:
        node_id, separator, ssh_alias = value.partition("=")
        if (
            separator != "="
            or NODE_ID.fullmatch(node_id) is None
            or not ssh_alias
            or node_id in selected
            or ssh_alias in selected.values()
        ):
            raise GateError("selected Fleet node mapping is invalid")
        selected[node_id] = ssh_alias
    if list(selected.values()) != [head.ssh_alias, worker.ssh_alias]:
        raise GateError("selected Fleet nodes do not match inventory SSH aliases")
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true", help="run only non-mutating inventory and host checks")
    parser.add_argument(
        "--expected-node",
        action="append",
        required=True,
        help="selected Fleet node-id=inventory SSH alias; repeat head then worker",
    )
    parser.add_argument(
        "--nccl-preflight-only",
        action="store_true",
        help="also verify native NCCL/MPI prerequisites on worker then head without staging sources",
    )
    args = parser.parse_args(argv)

    evidence: list[dict[str, Any]] = []
    document: dict[str, Any] = {
        "schema_version": 2,
        "captured_at": datetime.now(UTC).isoformat(),
        "evidence_scope": "live_runtime_verification",
        "status": "failed",
        "rdma": [],
        "latency": [],
        "rdma_counters_before": None,
        "rdma_counters_after": None,
        "rdma_counter_deltas": None,
        "nccl": None,
        "commands": evidence,
    }
    try:
        head, worker = load_hosts(args.inventory)
        selected_nodes = validate_expected_nodes(args.expected_node, head, worker)
        validate_consumers(head, worker)
        document["inventory"] = str(args.inventory)
        document["resolved_consumers"] = {
            key: head.fabric[key]
            for key in ("NCCL_SOCKET_IFNAME", "NCCL_IB_HCA", "NCCL_IB_GID_INDEX", "TP_SOCKET_IFNAME", "GLOO_SOCKET_IFNAME")
        }
        document["selected_nodes"] = [
            f"{node_id}={ssh_alias}"
            for node_id, ssh_alias in selected_nodes.items()
        ]
        runner = Runner(head, worker, evidence)
        run_preflights(runner, head, worker)
        if args.preflight_only:
            document["status"] = "preflight_passed"
            document["evidence_scope"] = "live_read_only_preflight"
            return 0
        if args.nccl_preflight_only:
            runner.worker_via_fabric(nccl_prerequisite_command())
            runner.remote(head.ssh_alias, nccl_prerequisite_command())
            document["status"] = "nccl_preflight_passed"
            document["evidence_scope"] = "live_read_only_preflight"
            return 0
        document["rdma_counters_before"] = capture_rdma_counters(runner, head, worker)
        traffic_error: GateError | None = None
        try:
            document["rdma"] = run_rdma(runner, head, worker)
            document["latency"] = run_rdma_latency(runner, head, worker)
            nccl = run_nccl(runner, head, worker)
            document["nccl"] = dataclasses.asdict(nccl)
            if not nccl.passed:
                raise GateError(nccl.reason or "NCCL failed")
        except GateError as error:
            traffic_error = error
        try:
            document["rdma_counters_after"] = capture_rdma_counters(runner, head, worker)
            document["rdma_counter_deltas"] = validate_counter_delta(
                document["rdma_counters_before"], document["rdma_counters_after"]
            )
        except GateError as error:
            if traffic_error is None:
                traffic_error = error
            else:
                document["counter_failure"] = str(error)
        if traffic_error is not None:
            raise traffic_error
        document["status"] = "passed"
        return 0
    except GateError as error:
        document["failure"] = str(error)
        return 1
    finally:
        write_json(args.output, document)


if __name__ == "__main__":
    sys.exit(main())
