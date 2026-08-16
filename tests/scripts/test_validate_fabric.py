"""Regression tests for the offline parsers used by ``validate-fabric``."""

import subprocess
import sys
import threading
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate_fabric.py"


@pytest.fixture
def validate_module():
    """Load the parser from the executable without running a live check."""
    loader = SourceFileLoader("validate_fabric", str(SCRIPT))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


@pytest.fixture
def parse_nccl(validate_module):
    return validate_module.parse_nccl


def test_inventory_uses_two_fabric_functions_per_host(validate_module):
    """The canonical inventory uses function labels, not fictional rails."""
    head, worker = validate_module.load_hosts(ROOT / "inventory" / "cluster.toml")

    assert [function.name for function in head.rails] == ["function100", "function101"]
    assert [function.name for function in worker.rails] == ["function100", "function101"]


def test_expected_fleet_nodes_bind_to_exact_inventory_ssh_aliases(validate_module):
    head, worker = validate_module.load_hosts(ROOT / "inventory" / "cluster.toml")

    selected = validate_module.validate_expected_nodes(
        [
            "spk_0123456789abcdef0123456789abcdef=vonk-node-1",
            "spk_fedcba9876543210fedcba9876543210=vonk-node-2",
        ],
        head,
        worker,
    )

    assert selected == {
        "spk_0123456789abcdef0123456789abcdef": "vonk-node-1",
        "spk_fedcba9876543210fedcba9876543210": "vonk-node-2",
    }
    with pytest.raises(validate_module.GateError, match="inventory SSH aliases"):
        validate_module.validate_expected_nodes(
            [
                "spk_0123456789abcdef0123456789abcdef=vonk-node-2",
                "spk_fedcba9876543210fedcba9876543210=vonk-node-1",
            ],
            head,
            worker,
        )


def test_read_only_preflight_probes_each_exact_peer_on_its_bound_interface(
    validate_module,
):
    head, _worker = aggregate_hosts(validate_module)

    class Runner:
        def __init__(self):
            self.commands = []

        def remote(self, host, command):
            self.commands.append((host, command))

    runner = Runner()
    validate_module.remote_preflight(runner, head, via_fabric=False)

    command = runner.commands[0][1]
    assert "timeout 5 ping -n -I enp1s0f1np1 -c 1 -W 2 192.168.100.11" in command
    assert "timeout 5 ping -n -I enP2p1s0f1np1 -c 1 -W 2 192.168.101.11" in command


def test_local_fabric_boundary_uses_explicit_ssh_override(
    validate_module, monkeypatch
):
    monkeypatch.setenv("VONK_SSH_BIN", "/opt/custom/ssh-wrapper")
    rail = validate_module.Rail(
        "function100",
        "enp1s0f1np1",
        "rocep1s0f1",
        3,
        "192.168.100.10",
        "192.168.100.11",
    )
    head = validate_module.Host("node1", "vonk-node-1", {}, (rail, rail))
    worker = validate_module.Host("node2", "vonk-node-2", {}, (rail, rail))
    runner = validate_module.Runner(head, worker, [])
    commands = []

    def local(command, *, check=True, input_text=None):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    runner.local = local
    runner.remote(head.ssh_alias, "true\n")

    assert commands[0][0] == "/opt/custom/ssh-wrapper"


def test_rejects_tcp_fallback(parse_nccl):
    """A successful process is not evidence of RDMA if NCCL selected sockets."""
    result = parse_nccl("NET/Socket : Using enp...\nAvg bus bandwidth : 11.0")

    assert result.passed is False


def test_accepts_ib_transport(parse_nccl):
    """The acceptance parser records the selected RDMA transport."""
    result = parse_nccl("NET/IB : Using rocep1s0f1\nAvg bus bandwidth : 20.0")

    assert result.transport == "IB"


def test_rejects_ib_diagnostic_without_a_using_selection(parse_nccl):
    """Discovery output is not evidence that NCCL selected an RDMA transport."""
    result = parse_nccl("NET/IB : No device found\nAvg bus bandwidth : 19.3")

    assert result.passed is False
    assert result.reason == "NCCL did not report NET/IB : Using"


def test_rejects_nccl_output_without_measured_bandwidth(parse_nccl):
    """NCCL initialization alone does not make an all-reduce an acceptance result."""
    result = parse_nccl("NET/IB : Using rocep1s0f1\n# Out of bounds values")

    assert result.passed is False
    assert result.bus_bandwidth_gbps is None


def test_rejects_non_positive_nccl_bandwidth(parse_nccl):
    """A zero/negative metric is not a successful measured benchmark."""
    result = parse_nccl("NET/IB : Using rocep1s0f1\nAvg bus bandwidth : 0.0")

    assert result.passed is False


def test_nccl_prefers_reported_average_over_faster_individual_row(parse_nccl):
    """The admission floor applies to the final average, not the best message size."""
    output = """
NET/IB : Using rocep1s0f1,roceP2p1s0f1
 8388608 1 float sum 1.0 21.0 20.0 0
Avg bus bandwidth : 17.0
"""

    result = parse_nccl(output)

    assert result.bus_bandwidth_gbps == 17.0


def test_runs_head_rdma_client_through_the_head_alias(validate_module):
    """The bound remote method needs the head alias as its first argument."""
    rail = validate_module.Rail("rail100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11")
    head = validate_module.Host("node1", "vonk-node-1", {}, (rail, rail))
    worker = validate_module.Host("node2", "vonk-node-2", {}, (rail, rail))

    class Runner:
        def __init__(self):
            self.head = head
            self.calls = []

        def remote(self, host, command, *, check=True):
            self.calls.append(("remote", host, command, check))
            if "nohup" in command:
                return SimpleNamespace(stdout="1234\n", stderr="", returncode=0)
            return SimpleNamespace(
                stdout=(
                    "Transport type : IB\nLink type : Ethernet\n"
                    "Mtu : 1024[B]\nGID index : 3\n65536 5000 0.0 88.5 0.1\n"
                ),
                stderr="",
                returncode=0,
            )

        def worker_via_fabric(self, command, *, check=True):
            self.calls.append(("worker", command, check))
            return SimpleNamespace(
                stdout=(
                    "1234\n"
                    if "nohup" in command
                    else "Transport type : IB\nLink type : Ethernet\nMtu : 1024[B]\n"
                    "GID index : 3\n65536 5000 0.0 88.5 0.1\n"
                ),
                stderr="",
                returncode=0,
            )

    result = validate_module.run_one_rdma(Runner(), worker, head, rail, rail, "ib_write_bw", 12000)

    assert result["passed"] is True
    assert result["client_exit_code"] == 0
    assert result["server_exit_code"] == 0


def test_rejects_nonzero_rdma_server_even_with_positive_output(validate_module):
    """The client metric cannot hide a failed perftest server process."""
    rail = validate_module.Rail("rail100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11")
    head = validate_module.Host("node1", "vonk-node-1", {}, (rail, rail))
    worker = validate_module.Host("node2", "vonk-node-2", {}, (rail, rail))
    positive = "Transport type : IB\nLink type : Ethernet\n65536 5000 0.0 88.5 0.1\n"

    class Runner:
        def __init__(self):
            self.head = head

        def remote(self, host, command, *, check=True):
            if "nohup" in command:
                return SimpleNamespace(stdout="1234\n", stderr="", returncode=0)
            return SimpleNamespace(stdout=positive, stderr="server failure", returncode=1)

        def worker_via_fabric(self, command, *, check=True):
            return SimpleNamespace(stdout=positive, stderr="", returncode=0)

    with pytest.raises(validate_module.GateError, match="server exited 1"):
        validate_module.run_one_rdma(Runner(), head, worker, rail, rail, "ib_write_bw", 12000)


def test_rejects_rdma_component_below_declared_floor(validate_module):
    """A positive result is not enough when it misses the accepted floor."""
    rail = validate_module.Rail("rail100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11")
    head = validate_module.Host("node1", "vonk-node-1", {}, (rail, rail))
    worker = validate_module.Host("node2", "vonk-node-2", {}, (rail, rail))
    positive_but_low = (
        "Transport type : IB\nLink type : Ethernet\nMtu : 1024[B]\n"
        "GID index : 3\n65536 5000 0.0 97.0 0.1\n"
    )

    class Runner:
        def __init__(self):
            self.head = head

        def remote(self, host, command, *, check=True):
            if "nohup" in command:
                return SimpleNamespace(stdout="1234\n", stderr="", returncode=0)
            return SimpleNamespace(stdout=positive_but_low, stderr="", returncode=0)

        def worker_via_fabric(self, command, *, check=True):
            return SimpleNamespace(
                stdout="1234\n" if "nohup" in command else positive_but_low,
                stderr="",
                returncode=0,
            )

    with pytest.raises(validate_module.GateError, match="below 98.01"):
        validate_module.run_one_rdma(
            Runner(),
            worker,
            head,
            rail,
            rail,
            "ib_write_bw",
            12000,
            minimum_bandwidth_gbps=98.01,
        )


def aggregate_hosts(validate_module):
    head_rails = (
        validate_module.Rail("function100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11"),
        validate_module.Rail("function101", "enP2p1s0f1np1", "roceP2p1s0f1", 3, "192.168.101.10", "192.168.101.11"),
    )
    worker_rails = (
        validate_module.Rail("function100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.11", "192.168.100.10"),
        validate_module.Rail("function101", "enP2p1s0f1np1", "roceP2p1s0f1", 3, "192.168.101.11", "192.168.101.10"),
    )
    return (
        validate_module.Host("node1", "vonk-node-1", {}, head_rails),
        validate_module.Host("node2", "vonk-node-2", {}, worker_rails),
    )


def test_aggregate_requires_concurrent_components(validate_module):
    """Two qualifying functions must actually overlap to prove physical-link bandwidth."""
    head, worker = aggregate_hosts(validate_module)
    barrier = threading.Barrier(2)
    durations = []

    def component(*args, **kwargs):
        barrier.wait(timeout=1)
        durations.append(kwargs.get("duration_seconds"))
        return {
            "name": f"component-{args[6]}",
            "passed": True,
            "bandwidth_gbps": 92.5,
            "started_monotonic": 10.0,
            "finished_monotonic": 12.0,
        }

    result = validate_module.run_aggregate_rdma_write(
        SimpleNamespace(), worker, head, base_port=13000, run_component=component
    )

    assert result["aggregate_bandwidth_gbps"] == 185.0
    assert result["overlap_seconds"] == 2.0
    assert len(result["components"]) == 2
    assert durations == [5, 5]


def test_aggregate_rejects_non_overlapping_components(validate_module):
    """Sequentially adding two function results must never claim 200 Gb/s-class service."""
    head, worker = aggregate_hosts(validate_module)

    def component(*args, **kwargs):
        port = args[6]
        return {
            "name": f"component-{port}",
            "passed": True,
            "bandwidth_gbps": 100.0,
            "started_monotonic": 10.0 if port == 13000 else 12.0,
            "finished_monotonic": 11.0 if port == 13000 else 13.0,
        }

    with pytest.raises(validate_module.GateError, match="did not overlap"):
        validate_module.run_aggregate_rdma_write(
            SimpleNamespace(), worker, head, base_port=13000, run_component=component
        )


def test_aggregate_rejects_bandwidth_below_nvidia_floor(validate_module):
    """Concurrent traffic below NVIDIA's 184 Gb/s floor blocks distributed models."""
    head, worker = aggregate_hosts(validate_module)

    def component(*args, **kwargs):
        return {
            "name": f"component-{args[6]}",
            "passed": True,
            "bandwidth_gbps": 91.5,
            "started_monotonic": 10.0,
            "finished_monotonic": 12.0,
        }

    with pytest.raises(validate_module.GateError, match="below 184.00"):
        validate_module.run_aggregate_rdma_write(
            SimpleNamespace(), worker, head, base_port=13000, run_component=component
        )


def test_nccl_rejects_bus_bandwidth_below_regression_floor(validate_module):
    """NET/IB selection alone cannot hide a material NCCL regression."""
    rail = validate_module.Rail("function100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11")
    fabric = {
        "NCCL_SOCKET_IFNAME": "=enp1s0f1np1,enP2p1s0f1np1",
        "NCCL_IB_HCA": "=rocep1s0f1:1,roceP2p1s0f1:1",
        "NCCL_IB_GID_INDEX": 3,
        "TP_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
        "GLOO_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
    }
    head = validate_module.Host("node1", "vonk-node-1", fabric, (rail, rail))
    worker = validate_module.Host("node2", "vonk-node-2", fabric, (rail, rail))

    class Runner:
        def remote(self, host, command, *, check=True):
            return SimpleNamespace(
                stdout="NET/IB : Using rocep1s0f1\nAvg bus bandwidth : 17.0",
                stderr="",
                returncode=0,
            )

        def worker_via_fabric(self, command, *, check=True):
            return SimpleNamespace(stdout="", stderr="", returncode=0)

    with pytest.raises(validate_module.GateError, match="below 17.44"):
        validate_module.run_nccl(Runner(), head, worker)


def test_nccl_rejects_missing_active_hca(validate_module):
    """A distributed run cannot pass while using only half of the physical-link functions."""
    head, worker = aggregate_hosts(validate_module)
    fabric = {
        "NCCL_SOCKET_IFNAME": "=enp1s0f1np1,enP2p1s0f1np1",
        "NCCL_IB_HCA": "=rocep1s0f1:1,roceP2p1s0f1:1",
        "NCCL_IB_GID_INDEX": 3,
        "TP_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
        "GLOO_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
    }
    head = validate_module.Host(head.name, head.ssh_alias, fabric, head.rails)
    worker = validate_module.Host(worker.name, worker.ssh_alias, fabric, worker.rails)

    class Runner:
        def remote(self, host, command, *, check=True):
            return SimpleNamespace(
                stdout=(
                    "NCCL_IB_HCA=rocep1s0f1,roceP2p1s0f1\n"
                    "NET/IB : Using rocep1s0f1\n"
                    "Avg bus bandwidth : 19.3"
                ),
                stderr="",
                returncode=0,
            )

        def worker_via_fabric(self, command, *, check=True):
            return SimpleNamespace(stdout="", stderr="", returncode=0)

    with pytest.raises(validate_module.GateError, match="did not select roceP2p1s0f1"):
        validate_module.run_nccl(Runner(), head, worker)


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        ("Mtu : 2048[B]\nGID index : 3", "1024-byte RoCE MTU"),
        ("Mtu : 1024[B]\nGID index : 2", "GID index 3"),
    ],
)
def test_rejects_rdma_bandwidth_with_wrong_roce_path(validate_module, header, reason):
    """Bandwidth is invalid when perftest did not use the pinned RoCE path."""
    output = (
        "Transport type : IB\n"
        "Link type : Ethernet\n"
        f"{header}\n"
        "65536 5000 0.0 108.9 0.1\n"
    )

    result = validate_module.parse_rdma(output)

    assert result.passed is False
    assert reason in result.reason


def test_parses_fixed_rdma_write_latency_distribution(validate_module):
    """The cluster baseline retains tail latency rather than only an average."""
    output = """
                    RDMA_Write Latency Test
 Transport type : IB
 Mtu             : 1024[B]
 Link type       : Ethernet
 GID index       : 3
 #bytes #iterations    t_min[usec]    t_max[usec]  t_typical[usec]    t_avg[usec]    t_stdev[usec]   99% percentile[usec]   99.9% percentile[usec]
 8       10000          1.91           4.80         1.98               1.98            0.02            2.05                    2.19
"""

    result = validate_module.parse_rdma_latency(output)

    assert result.passed is True
    assert result.message_bytes == 8
    assert result.iterations == 10000
    assert result.minimum_usec == 1.91
    assert result.maximum_usec == 4.80
    assert result.typical_usec == 1.98
    assert result.average_usec == 1.98
    assert result.standard_deviation_usec == 0.02
    assert result.p99_usec == 2.05
    assert result.p999_usec == 2.19


def test_rejects_latency_without_roce_transport(validate_module):
    """A numeric latency row over the wrong transport cannot establish the baseline."""
    output = """
 Transport type : TCP
 Link type : Ethernet
 8 10000 1.91 4.80 1.98 1.98 0.02 2.05 2.19
"""

    result = validate_module.parse_rdma_latency(output)

    assert result.passed is False
    assert result.reason == "latency test did not report IB transport"


def test_parses_only_expected_hca_error_counters(validate_module):
    """Counter evidence excludes inactive functions while requiring every monitored key."""
    output = """
link rocep1s0f0/1 packet_seq_err 99 local_ack_timeout_err 99 roce_adp_retrans 99
link rocep1s0f1/1 packet_seq_err 0 local_ack_timeout_err 1 roce_adp_retrans 2
"""

    result = validate_module.parse_rdma_counters(
        output,
        expected_hcas=("rocep1s0f1",),
        monitored_counters=("packet_seq_err", "local_ack_timeout_err", "roce_adp_retrans"),
    )

    assert result == {
        "rocep1s0f1/local_ack_timeout_err": 1,
        "rocep1s0f1/packet_seq_err": 0,
        "rocep1s0f1/roce_adp_retrans": 2,
    }


def test_rejects_missing_rdma_error_counter(validate_module):
    """A truncated counter snapshot is not accepted as an all-zero result."""
    with pytest.raises(validate_module.GateError, match="missing packet_seq_err"):
        validate_module.parse_rdma_counters(
            "link rocep1s0f1/1 local_ack_timeout_err 0",
            expected_hcas=("rocep1s0f1",),
            monitored_counters=("packet_seq_err", "local_ack_timeout_err"),
        )


def test_rejects_growing_rdma_error_counter(validate_module):
    """Any new sequence, retry, or timeout error fails the live acceptance run."""
    before = {
        "node1/rocep1s0f1/packet_seq_err": 0,
        "node1/rocep1s0f1/local_ack_timeout_err": 2,
    }
    after = {
        "node1/rocep1s0f1/packet_seq_err": 1,
        "node1/rocep1s0f1/local_ack_timeout_err": 2,
    }

    with pytest.raises(validate_module.GateError, match="packet_seq_err grew from 0 to 1"):
        validate_module.validate_counter_delta(before, after)


def test_accepts_unchanged_rdma_error_counters(validate_module):
    """Pre-existing counters are recorded but only growth during the run is rejected."""
    before = {"node1/rocep1s0f1/packet_seq_err": 3}
    after = {"node1/rocep1s0f1/packet_seq_err": 3}

    assert validate_module.validate_counter_delta(before, after) == {
        "node1/rocep1s0f1/packet_seq_err": 0
    }


def test_latency_command_pins_baseline_parameters(validate_module):
    """Every future latency comparison uses the same verb, payload, and sample count."""
    rail = validate_module.Rail("function100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11")

    command = validate_module.latency_command(
        rail, "192.168.100.11", 14000, server=False
    )

    assert command == (
        "/usr/bin/ib_write_lat -d rocep1s0f1 -i 1 -x 3 -p 14000 "
        "-F --size 8 --iters 10000 192.168.100.11"
    )


def test_runs_fixed_latency_and_records_distribution(validate_module):
    """The live latency wrapper checks both process exits and returns parsed metrics."""
    rail = validate_module.Rail("function100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11")
    head = validate_module.Host("node1", "vonk-node-1", {}, (rail, rail))
    worker = validate_module.Host("node2", "vonk-node-2", {}, (rail, rail))
    latency = """
 Transport type : IB
 Mtu : 1024[B]
 Link type : Ethernet
 GID index : 3
 8 10000 1.91 4.80 1.98 1.98 0.02 2.05 2.19
"""

    class Runner:
        def __init__(self):
            self.head = head

        def remote(self, host, command, *, check=True):
            if "nohup" in command:
                return SimpleNamespace(stdout="4321\n", stderr="", returncode=0)
            return SimpleNamespace(stdout=latency, stderr="", returncode=0)

        def worker_via_fabric(self, command, *, check=True):
            return SimpleNamespace(
                stdout="4321\n" if "nohup" in command else latency,
                stderr="",
                returncode=0,
            )

    result = validate_module.run_one_rdma_latency(
        Runner(), worker, head, rail, rail, 14000
    )

    assert result["name"] == "ib_write_lat:node1->node2:function100"
    assert result["p99_usec"] == 2.05
    assert result["client_exit_code"] == 0
    assert result["server_exit_code"] == 0


def test_counter_capture_is_worker_first_and_scoped_to_active_hcas(validate_module):
    """Snapshots use both active functions on both hosts without management-plane counters."""
    head, worker = aggregate_hosts(validate_module)
    pairs = " ".join(f"{name} 0" for name in validate_module.RDMA_ERROR_COUNTERS)
    output = f"link rocep1s0f1/1 {pairs}\nlink roceP2p1s0f1/1 {pairs}\n"

    class Runner:
        def __init__(self):
            self.calls = []

        def remote(self, host, command, *, check=True):
            self.calls.append("head")
            return SimpleNamespace(stdout=output, stderr="", returncode=0)

        def worker_via_fabric(self, command, *, check=True):
            self.calls.append("worker")
            return SimpleNamespace(stdout=output, stderr="", returncode=0)

    runner = Runner()
    snapshot = validate_module.capture_rdma_counters(runner, head, worker)

    assert runner.calls == ["worker", "head"]
    assert len(snapshot) == 4 * len(validate_module.RDMA_ERROR_COUNTERS)
    assert all(key.startswith(("node1/", "node2/")) for key in snapshot)


def test_native_nccl_prerequisites_require_the_pinned_completed_build(validate_module):
    """The validator verifies the documented host-native result without staging it."""
    command = validate_module.nccl_prerequisite_command()

    assert "https://github.com/NVIDIA/nccl.git" in command
    assert "73cf112295c33aee2b895f329f592f2a9b4b0f97" in command
    assert "a0b82b2260cf5152b9f8c061bbf7eaf0ba096432" in command
    assert "/usr/local/cuda/bin/nvcc" in command
    assert "libnccl.so" in command
    assert "all_reduce_perf" in command
    assert "docker" not in command
    assert "sudo" not in command


def test_native_nccl_launch_uses_restricted_fabric_transport(validate_module):
    """MPI launch cannot weaken the dedicated Spark1-to-Spark2 SSH boundary."""
    head_rail = validate_module.Rail("rail100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11")
    worker_rail = validate_module.Rail("rail100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.11", "192.168.100.10")
    fabric = {
        "NCCL_SOCKET_IFNAME": "=enp1s0f1np1,enP2p1s0f1np1",
        "NCCL_IB_HCA": "=rocep1s0f1:1,roceP2p1s0f1:1",
        "NCCL_IB_GID_INDEX": 3,
        "TP_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
        "GLOO_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
    }
    head = validate_module.Host("node1", "vonk-node-1", fabric, (head_rail, head_rail))
    worker = validate_module.Host("node2", "vonk-node-2", fabric, (worker_rail, worker_rail))

    command = validate_module.nccl_launch_command(head, worker)

    assert "mpirun -np 2 -H localhost:1,vonk-node-2-fabric:1" in command
    assert "$HOME/nccl-tests/build/all_reduce_perf" in command
    assert "NCCL_DEBUG='INFO'" in command
    assert "NCCL_SOCKET_IFNAME='=enp1s0f1np1,enP2p1s0f1np1'" in command
    assert "NCCL_IB_HCA='=rocep1s0f1:1,roceP2p1s0f1:1'" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "ForwardAgent=no" in command
    assert "NET/Socket" not in command
    assert "StrictHostKeyChecking=no" not in command
    assert "192.168.1.211" not in command
    assert "192.168.1.212" not in command


def test_native_prerequisite_checks_each_openmpi_package(validate_module):
    """The required OpenMPI packages are verified independently."""
    command = validate_module.nccl_prerequisite_command()

    assert "libopenmpi-dev)" in command
    assert "openmpi-bin)" in command


def test_worker_preflight_precedes_head_preflight(validate_module):
    """Every remote prerequisite gate starts with GPU node 2 via the fabric alias."""
    rail = validate_module.Rail("rail100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11")
    head = validate_module.Host("node1", "vonk-node-1", {}, (rail, rail))
    worker = validate_module.Host("node2", "vonk-node-2", {}, (rail, rail))

    class Runner:
        def __init__(self):
            self.calls = []

        def remote(self, host, command, *, check=True):
            self.calls.append("head")

        def worker_via_fabric(self, command, *, check=True):
            self.calls.append("worker")

    runner = Runner()
    validate_module.run_preflights(runner, head, worker)

    assert runner.calls == ["worker", "head"]


def test_remote_preflight_pins_physical_link_speed_and_interface_mtu(validate_module):
    """Admission checks the exact physical-link state rather than file presence."""
    rail = validate_module.Rail(
        "function100",
        "enp1s0f1np1",
        "rocep1s0f1",
        3,
        "192.168.100.10",
        "192.168.100.11",
    )
    host = validate_module.Host("node1", "vonk-node-1", {}, (rail,))

    class Runner:
        def __init__(self):
            self.command = ""

        def remote(self, ssh_alias, command, *, check=True):
            self.command = command

    runner = Runner()
    validate_module.remote_preflight(runner, host, via_fabric=False)

    assert '/sys/class/net/enp1s0f1np1/speed)" = 200000' in runner.command
    assert '/sys/class/net/enp1s0f1np1/mtu)" = 1500' in runner.command


def test_nccl_validation_is_worker_first_and_launch_only(validate_module):
    """Completed native artifacts are checked, not rebuilt, before the collective."""
    rail = validate_module.Rail("rail100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11")
    fabric = {"NCCL_SOCKET_IFNAME": "=enp1s0f1np1", "NCCL_IB_HCA": "=rocep1s0f1:1", "NCCL_IB_GID_INDEX": 3, "TP_SOCKET_IFNAME": "enp1s0f1np1", "GLOO_SOCKET_IFNAME": "enp1s0f1np1"}
    head = validate_module.Host("node1", "vonk-node-1", fabric, (rail, rail))
    worker = validate_module.Host("node2", "vonk-node-2", fabric, (rail, rail))

    class Runner:
        def __init__(self):
            self.calls = []

        def remote(self, host, command, *, check=True):
            self.calls.append(("head", command))
            return SimpleNamespace(stdout="NET/IB : Using rocep1s0f1,roceP2p1s0f1\nAvg bus bandwidth : 19.3", stderr="", returncode=0)

        def worker_via_fabric(self, command, *, check=True):
            self.calls.append(("worker", command))
            return SimpleNamespace(stdout="", stderr="", returncode=0)

    runner = Runner()
    validate_module.run_nccl(runner, head, worker)

    assert [host for host, _ in runner.calls] == ["worker", "head", "head"]
    assert all("ensure_checkout" not in command for _, command in runner.calls)
