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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .common import NODE_ID, GateError, Host
from .counters import capture_rdma_counters
from .inventory import load_hosts, validate_consumers
from .latency import run_rdma_latency
from .nccl import nccl_prerequisite_command, run_nccl
from .parsers import validate_counter_delta
from .preflight import remote_preflight
from .rdma import run_rdma
from .runner import Runner


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
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="run only non-mutating inventory and host checks",
    )
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
            for key in (
                "NCCL_SOCKET_IFNAME",
                "NCCL_IB_HCA",
                "NCCL_IB_GID_INDEX",
                "TP_SOCKET_IFNAME",
                "GLOO_SOCKET_IFNAME",
            )
        }
        document["selected_nodes"] = [
            f"{node_id}={ssh_alias}" for node_id, ssh_alias in selected_nodes.items()
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
            document["rdma_counters_after"] = capture_rdma_counters(
                runner, head, worker
            )
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
