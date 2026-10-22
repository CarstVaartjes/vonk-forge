"""Informative headless recommendation, owned by the Fleet projection only."""

from vonk_agent_protocol import ProjectionCode
from vonk_agent_protocol.agent_words import ProfileReasonSeverity
from vonk_agent_protocol.inventory import HostSettings

from .common import ProjectionReason

# NVIDIA October 2026 SoC release; a DMI UEFI string is not this version.
MINIMUM_FIRMWARE = 0x02009E0F
MINIMUM_CARVEOUT_BYTES = 512 * 1024**2
UEFI_ADVICE = (
    "Set Advanced → Advanced Menu → Display Reserved Memory to 0.5 GB "
    "(fixed); save and reboot."
)
FIRMWARE_RESET_ADVICE = (
    "If nvidia-smi shows ERR! or 'GPU requires reset' after a warm reboot, power-cycle."
)


def display_memory_advice(host: HostSettings | None) -> ProjectionReason | None:
    if host is None or host.display_manager_active or host.graphical_default_target:
        return None
    observation = host.display_memory
    if observation is None:
        return None
    carveout = observation.carveout_bytes
    # Unknown must never be treated as the default 2 GB or as minimum.
    if carveout is None or carveout <= MINIMUM_CARVEOUT_BYTES:
        return None
    firmware = observation.soc_firmware_version
    if firmware is None:
        recommendation = (
            "Check SoC firmware: fwupdmgr get-devices (needs ≥0x02009e0f). "
            + UEFI_ADVICE
        )
    elif firmware < MINIMUM_FIRMWARE:
        recommendation = (
            "Update SoC firmware (fwupdmgr update), "
            "then "
            + UEFI_ADVICE[0].lower()
            + UEFI_ADVICE[1:]
            + " "
            + FIRMWARE_RESET_ADVICE
        )
    else:
        recommendation = UEFI_ADVICE
    return ProjectionReason(
        code=ProjectionCode.HOST_DISPLAY_MEMORY,
        detail=(
            f"Headless Spark display reserves {carveout} bytes; minimum is 0.5 GB. "
            "Fixed 0.5 GB releases 1.5 GiB from 2 GB regardless of a monitor at boot; "
            "Auto booted headless also counts as configured."
        ),
        severity=ProfileReasonSeverity.WARNING.value,
        recommendation=recommendation,
    )
