"""Nccl for fabric validation."""

from __future__ import annotations

from .common import (
    CUDA_NVCC,
    FABRIC_WORKER_ALIAS,
    MPI_HOME,
    NCCL_COMMIT,
    NCCL_MIN_GB_PER_SECOND,
    NCCL_TESTS_COMMIT,
    GateError,
    Host,
    NCCLResult,
)
from .parsers import parse_nccl, selected_nccl_hcas
from .runner import Runner


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
    export_lines = "\n".join(
        f"export {key}='{value}'" for key, value in exports.items()
    )
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
    if (
        parsed.bus_bandwidth_gbps is None
        or parsed.bus_bandwidth_gbps < NCCL_MIN_GB_PER_SECOND
    ):
        raise GateError(
            f"NCCL bus bandwidth {parsed.bus_bandwidth_gbps or 0.0:.2f} GB/s "
            f"is below {NCCL_MIN_GB_PER_SECOND:.2f} GB/s"
        )
    selected_hcas = selected_nccl_hcas(output)
    for function in head.rails:
        if function.hca not in selected_hcas:
            raise GateError(f"NCCL did not select {function.hca}")
    return parsed
