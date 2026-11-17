"""Spark host checks: plain advice derived from the agent's measured host tuning.

Informative only (owner 2026-11-17): never a Fleet warning, an attention item
or an admission input. Each check cites what the agent measured, says what it
costs and names the commands an operator runs with ``sudo``; the platform never
runs them. The desktop checks (graphical boot target, running display manager)
are not here: the helper corrects those itself and Fleet warns while they drift.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints
from vonk_agent_protocol.host_tuning import HostTuning, HostUnit, InstalledUnit

from ..strict_json import StrictModel

Text256 = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Text512 = Annotated[str, StringConstraints(min_length=1, max_length=512)]
MIB = 1024**2
GIB = 1024**3


class HostCheck(StrEnum):
    """Every host check, in the order the clients show them."""

    BLUETOOTH = "bluetooth"
    PRINTING = "printing"
    SNAPD = "snapd"
    DGX_DASHBOARD = "dgx-dashboard"
    FIRMWARE_REFRESH = "firmware-refresh"
    WATERMARK_BOOST = "watermark-boost"
    BOOT_SPLASH = "boot-splash"
    MEMORY_FRAGMENTATION = "memory-fragmentation"


class HostCheckAdvice(StrictModel):
    check: HostCheck
    #: What the agent measured, with its value.
    finding: Text256
    #: What it costs and why, in plain words.
    explanation: Text512
    #: Run in order on the Spark; each ``sudo`` is the operator's.
    commands: list[Text256] = Field(max_length=4)


#: Each service check and the units it covers; the command disables all of them.
SERVICE_UNITS: dict[HostCheck, tuple[HostUnit, ...]] = {
    HostCheck.BLUETOOTH: (HostUnit.BLUETOOTH,),
    HostCheck.PRINTING: (
        HostUnit.CUPS,
        HostUnit.CUPS_SOCKET,
        HostUnit.CUPS_PATH,
        HostUnit.CUPS_BROWSED,
    ),
    HostCheck.SNAPD: (
        HostUnit.SNAPD,
        HostUnit.SNAPD_SOCKET,
        HostUnit.SNAPD_SEEDED,
        HostUnit.SNAPD_SNAP_REPAIR,
    ),
    HostCheck.DGX_DASHBOARD: (HostUnit.DGX_DASHBOARD, HostUnit.DGX_DASHBOARD_ADMIN),
    HostCheck.FIRMWARE_REFRESH: (HostUnit.FWUPD_REFRESH,),
}

_SERVICE_EXPLANATIONS: dict[HostCheck, str] = {
    HostCheck.BLUETOOTH: (
        "A headless Spark has no Bluetooth devices to serve; the service only holds memory."
    ),
    HostCheck.PRINTING: (
        "Printing (CUPS) is unused on a headless Spark, and cups-browsed keeps "
        "looking for network printers."
    ),
    HostCheck.SNAPD: "snapd keeps the snap store running and refreshing.",
    HostCheck.DGX_DASHBOARD: (
        "The DGX Dashboard's periodic update check also wakes fwupd and "
        "PackageKit, taking more memory while it runs. Install updates with apt."
    ),
    HostCheck.FIRMWARE_REFRESH: (
        "It wakes fwupd in the background to refresh firmware metadata. Keep fwupd "
        "installed: it delivers the EC, UEFI, ConnectX-7 and NVMe firmware. Check "
        "by hand with: fwupdmgr refresh && fwupdmgr get-updates"
    ),
}

#: Desktop apps and the runtimes they need, as Ubuntu installs them on a Spark.
_DESKTOP_SNAP = re.compile(
    r"^(bare|core\d*|snapd|snapd-desktop-integration|snap-store|firmware-updater"
    r"|firefox|thunderbird|chromium|gtk-common-themes|gnome-[0-9a-z-]+|mesa-[0-9a-z-]+"
    r"|kf\d+-[0-9a-z-]+)$"
)
_SHOWN_SNAPS = 8

_WATERMARK_COMMANDS = [
    "echo vm.watermark_boost_factor=0 | sudo tee /etc/sysctl.d/90-watermark-boost.conf",
    "sudo sysctl -p /etc/sysctl.d/90-watermark-boost.conf",
]
#: One command: edits only the GRUB files that set splash (/etc/default/grub or
#: a /etc/default/grub.d drop-in), then regenerates the boot configuration.
_SPLASH_COMMAND = (
    "grep -lE '^GRUB_CMDLINE_LINUX(_DEFAULT)?=.*\\bsplash\\b' /etc/default/grub "
    "/etc/default/grub.d/*.cfg 2>/dev/null | sudo xargs -r sed -i -E "
    "'/^GRUB_CMDLINE_LINUX(_DEFAULT)?=/s/ *\\bsplash\\b//' && sudo update-grub"
)
_COMPACT_COMMAND = (
    "sync; echo 1 | sudo tee /proc/sys/vm/drop_caches; "
    "echo 1 | sudo tee /proc/sys/vm/compact_memory"
)


def host_checks(tuning: HostTuning | None) -> list[HostCheckAdvice]:
    """The advice for one Spark; an unknown reading gives no advice."""
    if tuning is None:
        return []
    advice: list[HostCheckAdvice] = []
    installed = {item.unit: item for item in tuning.units or ()}
    for check, units in SERVICE_UNITS.items():
        present = [installed[unit] for unit in units if unit in installed]
        if any(item.active for item in present):
            advice.append(_service(check, present, tuning.snaps))
    factor = tuning.watermark_boost_factor
    if factor is not None and factor > 0:
        advice.append(
            HostCheckAdvice(
                check=HostCheck.WATERMARK_BOOST,
                finding=f"vm.watermark_boost_factor is {factor}.",
                explanation=(
                    "After memory fragments, the kernel raises its free-memory "
                    "watermarks by this factor and reclaims ahead of need; that memory "
                    "is missing from MemAvailable while the boost lasts. 0 turns the "
                    "boost off, now and at every boot."
                ),
                commands=_WATERMARK_COMMANDS,
            )
        )
    if tuning.kernel_splash:
        advice.append(
            HostCheckAdvice(
                check=HostCheck.BOOT_SPLASH,
                finding="The kernel command line has splash.",
                explanation=(
                    "Cosmetic: the boot splash hides kernel messages on the console "
                    "of a headless Spark. Low impact; the change applies at the next boot."
                ),
                commands=[_SPLASH_COMMAND],
            )
        )
    if (free := tuning.free_memory) is not None and free.free_bytes > 0:
        share = _half_up(free.free_2mib_block_bytes * 100 / free.free_bytes)
        advice.append(
            HostCheckAdvice(
                check=HostCheck.MEMORY_FRAGMENTATION,
                finding=(
                    f"{_gib(free.free_2mib_block_bytes)} of {_gib(free.free_bytes)} "
                    f"free is in blocks of 2 MiB or more ({share}%)."
                ),
                explanation=(
                    "The GPU driver takes unified memory in physically contiguous "
                    "blocks of 2 MiB or more, so a load can run short while smaller "
                    "free blocks remain. Compaction regroups free memory: the helper "
                    "compacts once whenever loading starts on this Spark; to compact "
                    "now, run this while no workload is loading."
                ),
                commands=[_COMPACT_COMMAND],
            )
        )
    return advice


def _service(
    check: HostCheck,
    present: Sequence[InstalledUnit],
    snaps: Sequence[str] | None,
) -> HostCheckAdvice:
    """Disables every unit of the check installed on this Spark, active or not
    (an idle socket or path starts its service again); never a missing one."""
    running = [item for item in present if item.active]
    names = ", ".join(item.unit.value for item in running)
    memory = [item.memory_bytes for item in running if item.memory_bytes is not None]
    finding = f"Active: {names}" + (
        f"; {_mib(sum(memory))} in use now." if memory else "."
    )
    explanation = _SERVICE_EXPLANATIONS[check]
    if check is HostCheck.SNAPD:
        explanation += " " + _snaps(snaps)
    return HostCheckAdvice(
        check=check,
        finding=finding,
        explanation=explanation,
        commands=[
            "sudo systemctl disable --now "
            + " ".join(item.unit.value for item in present)
        ],
    )


def _snaps(snaps: Sequence[str] | None) -> str:
    if snaps is None:
        return "The installed snaps are not reported: check snap list before disabling snapd."
    if not snaps:
        return "No snaps are installed."
    other = [name for name in snaps if not _DESKTOP_SNAP.match(name)]
    if other:
        return (
            f"Installed snaps not known as desktop apps: {_names(other)}. "
            "Check that nothing uses them before disabling snapd."
        )
    return f"The installed snaps are desktop apps and their runtimes only: {_names(snaps)}."


def _names(names: Sequence[str]) -> str:
    rest = len(names) - _SHOWN_SNAPS
    return ", ".join(names[:_SHOWN_SNAPS]) + (f" and {rest} more" if rest > 0 else "")


def _mib(value: int) -> str:
    return f"{_half_up(value / MIB)} MiB" if value >= MIB else "under 1 MiB"


def _gib(value: int) -> str:
    tenths = _half_up(value * 10 / GIB)
    return f"{tenths // 10}.{tenths % 10} GiB"


def _half_up(value: float) -> int:
    """.5 rounds up, as everywhere in Fleet words."""
    return int(value + 0.5)
