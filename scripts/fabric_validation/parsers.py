"""Parsers for fabric validation."""

from __future__ import annotations

import re

from .common import (
    LATENCY_ITERATIONS,
    LATENCY_MESSAGE_BYTES,
    RDMA_ERROR_COUNTERS,
    GateError,
    NCCLResult,
    RDMALatencyResult,
    RDMAResult,
)


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
        return NCCLResult(
            False, transport, bandwidth, "NCCL did not report NET/IB : Using"
        )
    if bandwidth is None or bandwidth <= 0:
        return NCCLResult(
            False, transport, bandwidth, "NCCL reported no positive bus bandwidth"
        )
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
    rows = re.compile(
        r"^\s*\d+\s+\d+\s+[-+0-9.eE]+\s+([-+0-9.eE]+)\s+[-+0-9.eE]+\s*$", re.MULTILINE
    )
    values = [float(value) for value in rows.findall(output)]
    bandwidth = max(values) if values else None
    if bandwidth is None or bandwidth <= 0:
        return RDMAResult(
            False, bandwidth, "perftest reported no positive average bandwidth"
        )
    return RDMAResult(True, bandwidth)


def parse_rdma_latency(output: str) -> RDMALatencyResult:
    """Parse the fixed RoCE write-latency distribution in microseconds."""
    if not re.search(r"Transport type\s*:\s*IB\b", output, re.IGNORECASE):
        return RDMALatencyResult(
            False, reason="latency test did not report IB transport"
        )
    if not re.search(r"Link type\s*:\s*Ethernet\b", output, re.IGNORECASE):
        return RDMALatencyResult(
            False, reason="latency test did not report Ethernet/RoCE link"
        )
    if not re.search(r"Mtu\s*:\s*1024\[B\]", output, re.IGNORECASE):
        return RDMALatencyResult(
            False, reason="latency test did not report 1024-byte RoCE MTU"
        )
    if not re.search(r"GID index\s*:\s*3\b", output, re.IGNORECASE):
        return RDMALatencyResult(
            False, reason="latency test did not report GID index 3"
        )
    row = re.compile(
        r"^\s*(\d+)\s+(\d+)\s+"
        r"([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+"
        r"([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*$",
        re.MULTILINE,
    )
    match = row.search(output)
    if match is None:
        return RDMALatencyResult(
            False, reason="latency test did not report a distribution row"
        )
    message_bytes, iterations = (int(match.group(1)), int(match.group(2)))
    if message_bytes != LATENCY_MESSAGE_BYTES or iterations != LATENCY_ITERATIONS:
        return RDMALatencyResult(
            False, reason="latency test parameters did not match the fixed baseline"
        )
    metrics = [float(match.group(index)) for index in range(3, 10)]
    return RDMALatencyResult(
        passed=True,
        message_bytes=message_bytes,
        iterations=iterations,
        minimum_usec=metrics[0],
        maximum_usec=metrics[1],
        typical_usec=metrics[2],
        average_usec=metrics[3],
        standard_deviation_usec=metrics[4],
        p99_usec=metrics[5],
        p999_usec=metrics[6],
    )


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
                tokens[index]: int(tokens[index + 1])
                for index in range(0, len(tokens), 2)
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


def validate_counter_delta(
    before: dict[str, int], after: dict[str, int]
) -> dict[str, int]:
    """Reject missing snapshots or any monitored error growth during acceptance."""
    if before.keys() != after.keys():
        missing = sorted(before.keys() ^ after.keys())
        raise GateError(f"RDMA counter snapshots differ: {', '.join(missing)}")
    deltas = {key: after[key] - before[key] for key in sorted(before)}
    for key, delta in deltas.items():
        if delta < 0:
            raise GateError(f"RDMA counter {key} decreased during the run")
        if delta > 0:
            raise GateError(
                f"RDMA counter {key} grew from {before[key]} to {after[key]}"
            )
    return deltas
