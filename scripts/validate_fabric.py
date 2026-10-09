#!/usr/bin/env python3
"""Fail-closed acceptance checks for the one-link/two-function Vonk Forge GPU node fabric.

The script deliberately uses the head's ``vonk-node-2-fabric`` SSH alias for
every Node-1-to-node-2 action.  It never enables agent forwarding and never
copies a private key.  NCCL is built natively from pinned NVIDIA sources only
as a documented prerequisite; this validator verifies the completed artifacts
worker-first before the live fabric gates.
"""

import sys
from pathlib import Path

repository_scripts = Path(__file__).resolve().parent
if str(repository_scripts) not in sys.path:
    sys.path.insert(0, str(repository_scripts))

repository_src = Path(__file__).resolve().parents[1] / "src"
if str(repository_src) not in sys.path:
    sys.path.insert(0, str(repository_src))

from fabric_validation import (
    CUDA_NVCC,
    FABRIC_WORKER_ALIAS,
    LATENCY_ITERATIONS,
    LATENCY_MESSAGE_BYTES,
    MPI_HOME,
    NCCL_COMMIT,
    NCCL_MIN_GB_PER_SECOND,
    NCCL_TESTS_COMMIT,
    NCCL_VERSION,
    NODE_ID,
    PHYSICAL_LINK_MIN_GBPS,
    RDMA_ERROR_COUNTERS,
    READ_FUNCTION_MIN_GBPS,
    SSH_OPTIONS,
    WRITE_FUNCTION_MIN_GBPS,
    GateError,
    Host,
    NCCLResult,
    Rail,
    RDMALatencyResult,
    RDMAResult,
    Runner,
    capture_rdma_counters,
    command_record,
    latency_command,
    load_hosts,
    main,
    nccl_launch_command,
    nccl_prerequisite_command,
    parse_nccl,
    parse_rdma,
    parse_rdma_counters,
    parse_rdma_latency,
    perftest_command,
    remote_preflight,
    run_aggregate_rdma_write,
    run_nccl,
    run_one_rdma,
    run_one_rdma_latency,
    run_preflights,
    run_rdma,
    run_rdma_latency,
    selected_nccl_hcas,
    validate_consumers,
    validate_counter_delta,
    validate_expected_nodes,
    write_json,
)

__all__ = [
    "CUDA_NVCC",
    "FABRIC_WORKER_ALIAS",
    "LATENCY_ITERATIONS",
    "LATENCY_MESSAGE_BYTES",
    "MPI_HOME",
    "NCCL_COMMIT",
    "NCCL_MIN_GB_PER_SECOND",
    "NCCL_TESTS_COMMIT",
    "NCCL_VERSION",
    "NODE_ID",
    "PHYSICAL_LINK_MIN_GBPS",
    "RDMA_ERROR_COUNTERS",
    "READ_FUNCTION_MIN_GBPS",
    "SSH_OPTIONS",
    "WRITE_FUNCTION_MIN_GBPS",
    "GateError",
    "Host",
    "NCCLResult",
    "RDMALatencyResult",
    "RDMAResult",
    "Rail",
    "Runner",
    "capture_rdma_counters",
    "command_record",
    "latency_command",
    "load_hosts",
    "main",
    "nccl_launch_command",
    "nccl_prerequisite_command",
    "parse_nccl",
    "parse_rdma",
    "parse_rdma_counters",
    "parse_rdma_latency",
    "perftest_command",
    "remote_preflight",
    "run_aggregate_rdma_write",
    "run_nccl",
    "run_one_rdma",
    "run_one_rdma_latency",
    "run_preflights",
    "run_rdma",
    "run_rdma_latency",
    "selected_nccl_hcas",
    "validate_consumers",
    "validate_counter_delta",
    "validate_expected_nodes",
    "write_json",
]

if __name__ == "__main__":
    sys.exit(main())
