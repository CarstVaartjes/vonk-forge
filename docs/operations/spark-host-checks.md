# Spark host checks

A DGX Spark that only runs Vonk Forge workloads is headless, so some of what
DGX OS starts for a desktop costs memory the recipes count on. The agent
measures a small set of host settings with every inventory and the Controller
explains each one in `vonkctl fleet detail <spark>` and on the web Spark page
(**Host checks**), with the measured value, the commands to change it and the
commands to undo that (**Undo:**). One line above the checks says where to run
them: on the Spark itself, in a terminal or over SSH, as a user with `sudo`;
the Controller never changes a Spark. The web page has a copy button per command.

The checks are informative. They never gate admission or a load, never mark a
Spark as needing attention, and the platform never runs these commands: each
`sudo` is the operator's decision. A reading the agent cannot take (an older
agent, systemd not answering, a file missing) gives no advice rather than a
guess. Advice disappears with the next inventory (about 2-3 minutes: the agent
reports after 120 s, checked every minute at most) once the change has taken
effect: the boot splash only after a reboot, and the memory fragmentation
reading never goes. The Controller owns all of this text,
including the line above the checks and each undo.

The measurements come from `systemctl show` for the units below, `/snap`,
`/proc/sys/vm/watermark_boost_factor`, `/proc/sys/vm/compaction_proactiveness`, `/proc/cmdline` and `/proc/buddyinfo`.
The facts are `HostSettings.tuning` in the agent's inventory
(`agent_protocol/src/vonk_agent_protocol/host_tuning.py`); the advice is
derived in `control/src/vonk_control/fleet_projection/host_checks.py`.

## The checks

A service check is shown when any of its units is active. Its command names
only the units installed on that Spark (systemd `LoadState=loaded`), active or
not, since an idle socket or path unit starts its service again; a unit that is
not installed is never named. The table lists every unit a check covers.

| Check | Shown when | Why | Change (`sudo`) | Undo |
| --- | --- | --- | --- | --- |
| Desktop | the default boot target is `graphical.target` or the display manager runs | a desktop session holds about 1 GB | none: the helper switches the Spark to `multi-user.target` and stops the display manager itself; Fleet warns while it drifts | see [Remove Vonk Forge](uninstall.md#1h-check-and-revert-the-headless-boot-target) |
| Bluetooth | `bluetooth.service` is active | no Bluetooth devices to serve | `sudo systemctl disable --now bluetooth.service` | `sudo systemctl enable --now bluetooth.service` |
| Printing | any of `cups.service`, `cups.socket`, `cups.path`, `cups-browsed.service` is active | printing is unused; cups-browsed keeps looking for network printers | `sudo systemctl disable --now cups.service cups.socket cups.path cups-browsed.service` | `sudo systemctl enable --now cups.service cups.socket cups.path cups-browsed.service` |
| snapd | any of `snapd.service`, `snapd.socket`, `snapd.seeded.service`, `snapd.snap-repair.timer` is active | the snap store keeps running and refreshing; on a stock Spark the snaps are desktop apps (Firefox, Thunderbird, Snap Store, Firmware Updater) and their runtimes. The advice lists the installed snaps and names any it does not know as desktop apps: check `snap list` first | `sudo systemctl disable --now snapd.service snapd.socket snapd.seeded.service snapd.snap-repair.timer` | `sudo systemctl enable --now snapd.service snapd.socket snapd.seeded.service snapd.snap-repair.timer` |
| DGX Dashboard | `dgx-dashboard.service` or `dgx-dashboard-admin.service` is active | its periodic update check also wakes fwupd and PackageKit and takes more memory while it runs; install updates with apt | `sudo systemctl disable --now dgx-dashboard.service dgx-dashboard-admin.service` | `sudo systemctl enable --now dgx-dashboard.service dgx-dashboard-admin.service` |
| Firmware refresh | `fwupd-refresh.timer` is active | it wakes fwupd in the background. Keep fwupd installed: it delivers the EC, UEFI, ConnectX-7 and NVMe firmware; check by hand with `fwupdmgr refresh && fwupdmgr get-updates` | `sudo systemctl disable --now fwupd-refresh.timer` | `sudo systemctl enable --now fwupd-refresh.timer` |
| Watermark boost | `vm.watermark_boost_factor` is above 0 (Ubuntu's default is 15000) | after memory fragments the kernel raises its free-memory watermarks and reclaims ahead of need; that memory is missing from MemAvailable while the boost lasts | `echo vm.watermark_boost_factor=0 \| sudo tee /etc/sysctl.d/90-watermark-boost.conf` then `sudo sysctl -p /etc/sysctl.d/90-watermark-boost.conf` | `sudo rm -f /etc/sysctl.d/90-watermark-boost.conf` then `sudo sysctl -w vm.watermark_boost_factor=<the measured value>` (the advice fills in the value it measured) |
| Proactive compaction | `vm.compaction_proactiveness` is above 0 (the kernel default is 20) | the kernel compacts memory in the background; a Spark workload holds most of the unified memory, so this stalls the serving path and multi-Spark collectives. 0 stops only the background compaction; compaction on demand and the helper's compaction when loading starts still work | `echo vm.compaction_proactiveness=0 \| sudo tee /etc/sysctl.d/90-no-proactive-compaction.conf` then `sudo sysctl -p /etc/sysctl.d/90-no-proactive-compaction.conf` | `sudo rm -f /etc/sysctl.d/90-no-proactive-compaction.conf` then `sudo sysctl -w vm.compaction_proactiveness=<the measured value>` (the advice fills in the value it measured) |
| Boot splash (info) | the kernel command line has `splash` | cosmetic: it hides kernel messages on the console; applies after a reboot | `grep -lZE '^GRUB_CMDLINE_LINUX(_DEFAULT)?=.*[ "]splash([ "]\|$)' /etc/default/grub /etc/default/grub.d/*.cfg 2>/dev/null \| sudo xargs -0 -r sed -i -E '/^GRUB_CMDLINE_LINUX(_DEFAULT)?=/{:a;s/([ "])splash([ "]\|$)/\1\2/;ta}' && sudo update-grub` (removes only the exact word `splash` from the `GRUB_CMDLINE_LINUX` and `GRUB_CMDLINE_LINUX_DEFAULT` lines of `/etc/default/grub` and its `grub.d` drop-ins; `splash=...` stays) | `sudo sed -i -E '/^GRUB_CMDLINE_LINUX_DEFAULT="/{/[ "]splash[ "]/!s/^(GRUB_CMDLINE_LINUX_DEFAULT=")/\1splash /}' /etc/default/grub && sudo update-grub` (adds `splash` to the `GRUB_CMDLINE_LINUX_DEFAULT` line of `/etc/default/grub` unless it is there; drop-ins that extend `$GRUB_CMDLINE_LINUX_DEFAULT`, as on DGX OS, pick it up from there, so do not also add it back to the drop-in); takes effect after a reboot |
| Memory fragmentation (info) | always, as a reading | the GPU driver takes unified memory in physically contiguous blocks of 2 MiB or more, so a load can run short while smaller free blocks remain. The reading is free memory against the part of it in blocks of at least 2 MiB (from `/proc/buddyinfo`, at the kernel's page size). The helper compacts once whenever loading starts (below) | to compact now, while no workload is loading: `sync; echo 1 \| sudo tee /proc/sys/vm/drop_caches; echo 1 \| sudo tee /proc/sys/vm/compact_memory` | nothing to undo |

Measured on both Sparks on 2026-11-17 after 3.5 weeks of uptime: every unit
above was active, the watermark boost was 15000, `splash` was set, and of
118 GiB and 117 GiB free only 77 GiB (66%) and 71 GiB (60%) were in blocks of
2 MiB or more.

## Compaction when loading starts

The privileged helper compacts memory itself, without an operator: the first
page-cache drop ask of a load phase (no workload asked in the last
`LOADING_PHASE_WINDOW`, 25 s) syncs, drops the page cache and then writes `1`
to `/proc/sys/vm/compact_memory`. Every later ask in that phase, such as the
other workloads of a profile load or the five-second drop cadence while they
load, answers from that one result; an ask while that compaction still runs gets
none, never an earlier phase's. The drop runs first, so compaction regroups
the freed cache pages too and never migrates pages about to be dropped. Every
load path (profile load, single
recipe, job re-activation, sweep) asks the helper before its container starts,
so each gets it.

It is bounded and never refuses a start. The helper waits at most
`MEMORY_COMPACTION_TIMEOUT` (5 s). The budget of a phase's first ask is the 2 s
sync plus 5 s compaction, leaving 8 s of the agent's 15 s reply bound for the
drop write and the `/proc` reads, which have no bound of their own. A longer
compaction is reported `timed-out`, the kernel finishes it, and no second one
starts while it runs. A host whose sandbox refuses the write reports
`not-permitted`. The result (outcome, duration, free memory and free memory in
2 MiB blocks before and after) rides the page-cache drop report into the
start's preload or failure diagnostics, and the helper journal logs it as
`memory compaction outcome=... duration_ms=... free_2mib_bytes/free_bytes
before=... after=...`.

Compaction moves movable pages, so workloads already serving on the Spark can
stall briefly while it runs. It is not skipped for them yet: measure the
reported duration on a Spark that serves while another workload starts
before adding a rule.

## Related choices that are not host checks

- The helper drops the page cache before a workload loads with
  `drop_caches` `1` (page cache only), not `3`: measured on both Sparks
  (2026-10-27), dentries and inodes add little while the starting container
  needs them for its opens.
- Every container-launching path holds a load phase: a run start, a rank
  launch and a job (keyed by its job id, from just before its container starts
  until it exits). On GB10 the driver does not reclaim page cache for CUDA
  context creation, so a job that read its model files first would otherwise
  fail with `NVRM: Out of memory` (observed 2026-11-20, 143 GB of safetensors).
- A loader "sponge" and `TORCH_DIST_USE_NCCL2=0` (eager NCCL communicators
  returning memory on a head rank) are engine and recipe settings, owned by
  the recipe, not host settings.
