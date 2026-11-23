"""The attention item for a multi-node run that uses too few RDMA rails.

Each 200 Gb/s port of a Spark appears as two RDMA devices, and the helper
launches NCCL with both (``NCCL_IB_HCA``). If NCCL's own log shows it selected
fewer, the port gives roughly 112 of 200 Gb/s. The agent reports what the log
showed per run (``FabricRails``); a run whose log never showed both lines is
not reported, so nothing is guessed. Advice only: nothing is restarted.
"""

from __future__ import annotations

from vonk_agent_protocol.agent_words import ProfileReasonSeverity
from vonk_agent_protocol.inventory import HostSettings
from vonk_agent_protocol.reason_codes.profiles import ProjectionCode

from .common import ProjectionReason


def fabric_rails_warning(host: HostSettings) -> ProjectionReason | None:
    # The run losing the largest share of its rails is the one to show.
    short = [rails for rails in host.fabric_rails if rails.used < rails.provided]
    if not short:
        return None
    worst = min(short, key=lambda rails: rails.used / rails.provided)
    return ProjectionReason(
        code=ProjectionCode.NETWORK_RAILS_UNDERUSED,
        detail=f"Network using {worst.used} of {worst.provided} rails.",
        severity=ProfileReasonSeverity.WARNING.value,
        recommendation=(
            "NCCL selected fewer RDMA devices than were launched, so a 200 Gb/s "
            "port gives about half its bandwidth. Check the run's NCCL log; "
            "restarting the run repeats the selection."
        ),
    )
