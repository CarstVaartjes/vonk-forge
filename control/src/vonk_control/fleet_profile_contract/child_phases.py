"""The phases a profile load's child reports: one closed vocabulary.

A leaf module, so that the fleet-profile contract and the stall record it
carries (``operation_blockers``) name the same type without an import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from vonk_agent_protocol.agent_words import ProfileChildPhase, ProfileReportedPhase

if TYPE_CHECKING:
    FleetProfileChildPhase = Literal[
        "model-download",
        "container-download",
        "container-build",
        "target-copy",
        "runtime-install",
        "start",
        "final-verify",
        "transfer",
        "verify",
        "prepare",
        "cleanup",
        "stop",
        "uninstall",
        "final_verify",
    ]
else:
    FleetProfileChildPhase = Literal[
        tuple(member.value for member in (*ProfileChildPhase, *ProfileReportedPhase))
    ]
