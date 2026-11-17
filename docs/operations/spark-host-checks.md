# Spark host checks

A DGX Spark that only runs Vonk Forge workloads is headless, so some of what
DGX OS starts for a desktop costs memory the recipes count on. The agent
measures a small set of host settings with every inventory and the Controller
explains each one in `vonkctl fleet detail <spark>` and on the web Spark page
(**Host checks**), with the measured value and the commands to change it.

The checks are informative. They never gate admission or a load, never mark a
Spark as needing attention, and the platform never runs these commands: each
`sudo` is the operator's decision. A reading the agent cannot take (an older
agent, systemd not answering, a file missing) gives no advice rather than a
guess. Advice disappears with the next inventory (about two minutes) after the
change.

The measurements come from `systemctl show` for the units below, `/snap`,
`/proc/sys/vm/watermark_boost_factor`, `/proc/cmdline` and `/proc/buddyinfo`.
The facts are `HostSettings.tuning` in the agent's inventory
(`agent_protocol/src/vonk_agent_protocol/host_tuning.py`); the advice is
derived in `control/src/vonk_control/fleet_projection/host_checks.py`.

## The checks

| Check | Shown when | Why | Change (`sudo`) | Undo |
| --- | --- | --- | --- | --- |
| Desktop | the default boot target is `graphical.target` or the display manager runs | a desktop session holds about 1 GB | none: the helper switches the Spark to `multi-user.target` and stops the display manager itself; Fleet warns while it drifts | see [Remove Vonk Forge](uninstall.md#1h-check-and-revert-the-headless-boot-target) |
| Bluetooth | `bluetooth.service` is active | no Bluetooth devices to serve | `sudo systemctl disable --now bluetooth.service` | `sudo systemctl enable --now bluetooth.service` |
| Printing | any of `cups.service`, `cups.socket`, `cups.path`, `cups-browsed.service` is active | printing is unused; cups-browsed keeps looking for network printers | `sudo systemctl disable --now cups.service cups.socket cups.path cups-browsed.service` | `sudo systemctl enable --now cups.service cups.socket cups.path cups-browsed.service` |
| snapd | any of `snapd.service`, `snapd.socket`, `snapd.seeded.service`, `snapd.snap-repair.timer` is active | the snap store keeps running and refreshing; on a stock Spark the snaps are desktop apps (Firefox, Thunderbird, Snap Store, Firmware Updater) and their runtimes. The advice lists the installed snaps and names any it does not know as desktop apps: check `snap list` first | `sudo systemctl disable --now snapd.service snapd.socket snapd.seeded.service snapd.snap-repair.timer` | `sudo systemctl enable --now snapd.service snapd.socket snapd.seeded.service snapd.snap-repair.timer` |
| DGX Dashboard | `dgx-dashboard.service` or `dgx-dashboard-admin.service` is active | its periodic update check also wakes fwupd and PackageKit and takes more memory while it runs; install updates with apt | `sudo systemctl disable --now dgx-dashboard.service dgx-dashboard-admin.service` | `sudo systemctl enable --now dgx-dashboard.service dgx-dashboard-admin.service` |
| Firmware refresh | `fwupd-refresh.timer` is active | it wakes fwupd in the background. Keep fwupd installed: it delivers the EC, UEFI, ConnectX-7 and NVMe firmware; check by hand with `fwupdmgr refresh && fwupdmgr get-updates` | `sudo systemctl disable --now fwupd-refresh.timer` | `sudo systemctl enable --now fwupd-refresh.timer` |
| Watermark boost | `vm.watermark_boost_factor` is above 0 (Ubuntu's default is 15000) | after memory fragments the kernel raises its free-memory watermarks and reclaims ahead of need; that memory is missing from MemAvailable while the boost lasts | `echo vm.watermark_boost_factor=0 \| sudo tee /etc/sysctl.d/90-watermark-boost.conf` then `sudo sysctl -p /etc/sysctl.d/90-watermark-boost.conf` | `sudo rm /etc/sysctl.d/90-watermark-boost.conf` then `sudo sysctl -w vm.watermark_boost_factor=15000` |
| Boot splash (info) | the kernel command line has `splash` | cosmetic: it hides kernel messages on the console; applies at the next boot | `sudo sed -i -E '/^GRUB_CMDLINE_LINUX_DEFAULT=/s/ *\bsplash\b//' /etc/default/grub` then `sudo update-grub` | add `splash` back to `GRUB_CMDLINE_LINUX_DEFAULT` in `/etc/default/grub`, then `sudo update-grub` |
| Memory fragmentation (info) | always, as a reading | the GPU driver takes unified memory in physically contiguous blocks of 2 MiB or more, so a load can run short while smaller free blocks remain. The reading is free memory against the part of it in blocks of at least 2 MiB (from `/proc/buddyinfo`, at the kernel's page size) | while no workload is loading: `sync; echo 1 \| sudo tee /proc/sys/vm/drop_caches; echo 1 \| sudo tee /proc/sys/vm/compact_memory` | nothing to undo |

Measured on both Sparks on 2026-11-17 after 3.5 weeks of uptime: every unit
above was active, the watermark boost was 15000, `splash` was set, and of
118 GiB and 117 GiB free only 77 GiB (66%) and 71 GiB (60%) were in blocks of
2 MiB or more.

## Related choices that are not host checks

- The helper drops the page cache before a workload loads with
  `drop_caches` `1` (page cache only), not `3`: measured on both Sparks
  (2026-10-27), dentries and inodes add little while the starting container
  needs them for its opens.
- A loader "sponge" and `TORCH_DIST_USE_NCCL2=0` (eager NCCL communicators
  returning memory on a head rank) are engine and recipe settings, owned by
  the recipe, not host settings.
