"""Common for fabric validation."""

from __future__ import annotations

import dataclasses
import re
from typing import Any

NCCL_VERSION = "v2.30.7-1"
NCCL_COMMIT = "73cf112295c33aee2b895f329f592f2a9b4b0f97"
NCCL_TESTS_COMMIT = "a0b82b2260cf5152b9f8c061bbf7eaf0ba096432"
CUDA_NVCC = "/usr/local/cuda/bin/nvcc"
MPI_HOME = "/usr/lib/aarch64-linux-gnu/openmpi"
FABRIC_WORKER_ALIAS = "vonk-node-2-fabric"
NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ForwardAgent=no",
    "-o",
    "ConnectTimeout=10",
)
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
