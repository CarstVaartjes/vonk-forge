"""Fail-closed acceptance checks for the one-link/two-function Vonk Forge GPU node fabric.

The script deliberately uses the head's ``vonk-node-2-fabric`` SSH alias for
every Node-1-to-node-2 action.  It never enables agent forwarding and never
copies a private key.  NCCL is built natively from pinned NVIDIA sources only
as a documented prerequisite; this validator verifies the completed artifacts
worker-first before the live fabric gates.
"""

from .cli import main as main
from .cli import run_preflights as run_preflights
from .cli import validate_expected_nodes as validate_expected_nodes
from .cli import write_json as write_json
from .common import CUDA_NVCC as CUDA_NVCC
from .common import FABRIC_WORKER_ALIAS as FABRIC_WORKER_ALIAS
from .common import LATENCY_ITERATIONS as LATENCY_ITERATIONS
from .common import LATENCY_MESSAGE_BYTES as LATENCY_MESSAGE_BYTES
from .common import MPI_HOME as MPI_HOME
from .common import NCCL_COMMIT as NCCL_COMMIT
from .common import NCCL_MIN_GB_PER_SECOND as NCCL_MIN_GB_PER_SECOND
from .common import NCCL_TESTS_COMMIT as NCCL_TESTS_COMMIT
from .common import NCCL_VERSION as NCCL_VERSION
from .common import NODE_ID as NODE_ID
from .common import PHYSICAL_LINK_MIN_GBPS as PHYSICAL_LINK_MIN_GBPS
from .common import RDMA_ERROR_COUNTERS as RDMA_ERROR_COUNTERS
from .common import READ_FUNCTION_MIN_GBPS as READ_FUNCTION_MIN_GBPS
from .common import SSH_OPTIONS as SSH_OPTIONS
from .common import WRITE_FUNCTION_MIN_GBPS as WRITE_FUNCTION_MIN_GBPS
from .common import GateError as GateError
from .common import Host as Host
from .common import NCCLResult as NCCLResult
from .common import Rail as Rail
from .common import RDMALatencyResult as RDMALatencyResult
from .common import RDMAResult as RDMAResult
from .counters import capture_rdma_counters as capture_rdma_counters
from .inventory import load_hosts as load_hosts
from .inventory import validate_consumers as validate_consumers
from .latency import latency_command as latency_command
from .latency import run_one_rdma_latency as run_one_rdma_latency
from .latency import run_rdma_latency as run_rdma_latency
from .nccl import nccl_launch_command as nccl_launch_command
from .nccl import nccl_prerequisite_command as nccl_prerequisite_command
from .nccl import run_nccl as run_nccl
from .parsers import parse_nccl as parse_nccl
from .parsers import parse_rdma as parse_rdma
from .parsers import parse_rdma_counters as parse_rdma_counters
from .parsers import parse_rdma_latency as parse_rdma_latency
from .parsers import selected_nccl_hcas as selected_nccl_hcas
from .parsers import validate_counter_delta as validate_counter_delta
from .preflight import remote_preflight as remote_preflight
from .rdma import perftest_command as perftest_command
from .rdma import run_aggregate_rdma_write as run_aggregate_rdma_write
from .rdma import run_one_rdma as run_one_rdma
from .rdma import run_rdma as run_rdma
from .runner import Runner as Runner
from .runner import command_record as command_record
