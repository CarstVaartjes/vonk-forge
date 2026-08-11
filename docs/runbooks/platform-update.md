# Vonk Forge GPU node platform update

This runbook updates the two Founders Edition Vonk Forge GPU nodes through Vonk Forge
Dashboard. It is intentionally worker-first and fail-stopped: validate GPU node 2
completely before changing GPU node 1. Never run a distributed model during this
procedure, and never use ad-hoc `apt` or `fwupdmgr` updates while the Dashboard
path is available.

This document covers the platform plane only. Workload packages (models,
adapters, runtimes, images, checkpoints, environments, and configuration) are
published from the NAS Git/TUF authority and rolled out independently through
the workload package API. Follow [Workload package operations](workload-packages.md)
for that flow; do not use this runbook, SSH, or `agent.update` for an ordinary
new model/runtime release.

The authoritative NVIDIA references checked on 2026-08-01 are:

- [DGX Spark release notes](https://docs.nvidia.com/dgx/dgx-spark/release-notes.html)
- [OS and component update guide](https://docs.nvidia.com/dgx/dgx-spark/os-and-component-update.html)
- [DGX Dashboard access](https://docs.nvidia.com/dgx/dgx-spark/dgx-dashboard.html)
- [NVIDIA container runtime validation](https://docs.nvidia.com/dgx/dgx-spark/nvidia-container-runtime-for-docker.html)
- [DGX Spark system recovery](https://docs.nvidia.com/dgx/dgx-spark/system-recovery.html)
- [May 2026 DGX Spark security bulletin](https://nvidia.custhelp.com/app/answers/detail/a_id/5835)

## Target and observed preparation state

NVIDIA's current-version table applies only to Founders Edition units. Both
machines report `vendor-reported platform identity`, and both already report effective OTA
`7.5.0`. `/etc/vonk-release` retains the factory image as
`VONK_SWBUILD_VERSION=7.2.3`; use `VONK_OTA_VERSION`, not the factory build field,
to determine the installed OTA level.

| Component | NVIDIA current table | GPU node 1 observed | GPU node 2 observed | Gate |
| --- | --- | --- | --- | --- |
| Effective DGX OS OTA | `7.5.0` | `7.5.0` | `7.5.0` | Dashboard says no update is pending |
| Ubuntu | not separately pinned | `24.04.4 LTS` | `24.04.4 LTS` | exact match |
| Kernel | `6.17` family | `6.17.0-1029-nvidia` | `6.17.0-1029-nvidia` | exact match after update |
| NVIDIA driver | `580.159.03` | `580.173.02` | `580.173.02` | do not downgrade; exact node match |
| CUDA Toolkit | `13.0.2` | package `13.0.3-1` | package `13.0.3-1` | do not downgrade; exact node match |
| UEFI | `1.110.13` | Dashboard confirmation pending | Dashboard confirmation pending | Dashboard success/no pending update |
| Embedded Controller | `3.5.8` | fwupd raw `0x03000508` | fwupd raw `0x03000508` | Dashboard success/no pending update |
| USB Power Delivery | `0.5.22` | fwupd raw `0x00000516` | fwupd raw `0x00000516` | Dashboard success/no pending update |
| TPM | `7.516.1` | Dashboard confirmation pending | Dashboard confirmation pending | Dashboard success/no pending update |
| SoC | `2.155.11` | fwupd raw `0x02009b0b` | fwupd raw `0x02009b0b` | Dashboard success/no pending update |
| Docker Engine | not published in the release table | `29.2.1` | `29.2.1` | exact node match |
| Docker Compose | not published in the release table | `5.0.2` | `5.0.2` | exact node match |
| containerd | not published in the release table | `2.2.1` | `2.2.1` | exact node match |
| NVIDIA Container Toolkit | not published in the release table | `1.19.1` | `1.19.1` | exact node match plus GPU-container test |

The installed driver and CUDA package revisions are newer than the values in
NVIDIA's current-version table. Treat that table as the release reference, not
as a downgrade instruction. Accept the installed revisions only when Vonk Forge
Dashboard reports the machine fully updated, and require both nodes to finish
on identical revisions. If Dashboard offers a newer release, record its
release highlights and versions before installing it and replace the target
column in the final update record.

At preparation time both hosts had no `/var/run/reboot-required` marker and
reported the same firmware, OS, driver, and container versions. This is not a
substitute for checking Dashboard for pending updates.

## Recovery and maintenance constraints

Before opening the update controls:

1. Confirm both units use their supplied power adapters and stable power.
2. Stop all applications and containers and save work. No distributed profile
   may be active.
3. Keep GPU node 1 unchanged while validating GPU node 2. If GPU node 2 fails any gate,
   stop; do not update GPU node 1.
4. Have a wired keyboard, display, a 16 GB or larger USB drive, and access to
   NVIDIA's Founders Edition recovery image. NVIDIA's recovery procedure
   reflashes the internal SSD and erases its data, so back up irreplaceable
   data first.
5. Do not interrupt power, reboot, or close the update while installation is
   in progress. Wait for Dashboard to report completion before rebooting if it
   asks for one.
6. After regenerating `/etc/machine-id`, reboot each node once before trusting
   persistent journal checks. A runtime machine-ID change leaves the current
   journald instance writing beneath the old machine-ID directory. Reboot
   GPU node 2 and validate it fully before rebooting GPU node 1.

NVIDIA does not document an in-place firmware downgrade in the update guide.
Treat firmware rollback as unavailable. The published recovery path restores
the system with recovery media and is destructive; it is not an automatic
rollback. On failure, preserve logs, leave the other GPU node unchanged, and use
recovery media or NVIDIA support rather than attempting a downgrade.

## Dashboard access

Dashboard listens on loopback port 11000 on each GPU node. Bind the Mac end of
the tunnel to loopback too; never expose Dashboard directly on the LAN. Use a
distinct local port for each node and keep strict host-key checking enabled.

Open the GPU node 2 tunnel first:

```bash
ssh -N -T \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:11002:127.0.0.1:11000 \
  vonk-node-2
```

While that command remains open, visit <http://127.0.0.1:11002>. Confirm that
the page identifies `node-2297`. Dashboard requires the initial user's sudo
authorization to apply updates; enter that password only in the local
Dashboard prompt, never in chat, a script, or a repository file.

Inspect the available updates and release highlights without starting an
installation. If Dashboard reports no pending update, record that result and
do not force a reinstall. This cluster still requires the one-time sequential
reboot after its machine-ID repair: reboot GPU node 2 first, wait for fresh
key-only SSH and Dashboard access, then validate it. If Dashboard offers an
update, apply it only to GPU node 2, monitor it to completion, and follow its
reboot direction. Do not reboot or start the GPU node 1 update yet.

After GPU node 2 passes every gate, close the GPU node 2 tunnel and use the same
procedure for GPU node 1:

```bash
ssh -N -T \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:11001:127.0.0.1:11000 \
  vonk-node-1
```

Visit <http://127.0.0.1:11001> and confirm `node-3542` before using any update
control.

## Validate one node

Set `host`, `before`, and `after` to GPU node 2 first. If an update was installed,
wait for the machine to reboot and for a fresh key-only SSH session to work.
If Dashboard reported no pending update, do not claim an update occurred. The
one-time reboot after identity regeneration is nevertheless required, and its
new boot ID must differ from the pre-change inventory.

```bash
host=vonk-node-2
before=inventory/raw/node2-pre.json
after=inventory/raw/node2-post-update.json

ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" 'hostname'
ssh -o BatchMode=yes "$host" 'bash -s' \
  < nodes/bin/collect-inventory > "$after"

uv run --with pytest --with jsonschema \
  pytest tests/nodes/test_collect_inventory.py -v \
  --inventory-dir inventory/raw

jq -e --slurpfile before "$before" '
  ([.interfaces[].ifname] | sort)
  == ([$before[0].interfaces[].ifname] | sort)
' "$after"

test "$(jq -r '.boot_id' "$before")" != "$(jq -r '.boot_id' "$after")"
```

The interface comparison must return `true`. An update may change link state
or addresses, but it must not make a physical interface disappear. Record and
investigate any difference before continuing.

Run the host, filesystem, and interface checks:

```bash
ssh -o BatchMode=yes "$host" 'bash -s' <<'REMOTE'
set -euo pipefail
test ! -e /var/run/reboot-required
machine_id="$(cat /etc/machine-id)"
test -d "/var/log/journal/$machine_id"
nvidia-smi -L
nvidia-smi --query-gpu=name,driver_version,temperature.gpu \
  --format=csv,noheader
findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS /
df -B1 /
ip -brief link
lspci -nn -d 15b3:
failed="$(systemctl --failed --no-legend --plain)"
test -z "$failed" || { printf '%s\n' "$failed" >&2; exit 1; }
REMOTE
```

Stage the audited privileged validator from the Mac and compare its local and
remote SHA-256 before running it. First pull the script's immutable CUDA
manifest-list digest while the node is healthy; the validator itself uses
`--pull=never`, so validation cannot silently substitute a moved tag or depend
on registry availability:

```bash
sudo docker pull \
  nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04@sha256:7d2f6a8c2071d911524f95061a0db363e24d27aa51ec831fcccf9e76eb72bc92
```

The validator still intentionally
runs with sudo even though `carst` now belongs to the Docker group: it also
reads the root-only kernel log and validates privileged storage/filesystem
state, failing closed on read errors or storage/filesystem errors.

```bash
scp nodes/bin/validate-platform-update-root \
  "$host:/tmp/validate-platform-update-root"
ssh -o BatchMode=yes "$host" \
  'chmod 0700 /tmp/validate-platform-update-root && sha256sum /tmp/validate-platform-update-root'
shasum -a 256 nodes/bin/validate-platform-update-root
```

The hashes must match. In an interactive SSH session on that GPU node, run one
audited privileged command:

```bash
sudo bash /tmp/validate-platform-update-root
```

It must exit zero, display the GPU and driver, and end with `PASS: GPU
container and current-boot storage checks passed`. A successful image pull
alone is not a pass. Remove the staged script after recording the result.

Do not touch GPU node 1 unless every GPU node 2 command passes and Dashboard remains
reachable. Repeat this section with:

```bash
host=vonk-node-1
before=inventory/raw/node1-pre.json
after=inventory/raw/node1-post-update.json
```

## Capture and compare matched versions

After each node passes, capture the normalized platform facts on the Mac. The
files are temporary comparison artifacts; the final values belong in the
update record below.

```bash
capture_platform() {
  local host="$1"
  local output="$2"

  ssh -o BatchMode=yes "$host" 'bash -s' > "$output" <<'REMOTE'
set -euo pipefail
vonk_ota="$(awk -F= '$1 == "VONK_OTA_VERSION" {gsub(/"/, "", $2); print $2}' /etc/vonk-release)"
kernel="$(uname -r)"
driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1)"
cuda_toolkit="$(dpkg-query -W -f='${Version}' cuda-toolkit-13-0)"
docker="$(dpkg-query -W -f='${Version}' docker-ce)"
compose="$(docker compose version --short)"
containerd="$(dpkg-query -W -f='${Version}' containerd.io)"
nvidia_ctk="$(dpkg-query -W -f='${Version}' nvidia-container-toolkit)"
jq -n \
  --arg vonk_ota "$vonk_ota" \
  --arg kernel "$kernel" \
  --arg driver "$driver" \
  --arg cuda_toolkit "$cuda_toolkit" \
  --arg docker "$docker" \
  --arg compose "$compose" \
  --arg containerd "$containerd" \
  --arg nvidia_ctk "$nvidia_ctk" \
  '{vonk_ota: $vonk_ota, kernel: $kernel, driver: $driver,
    cuda_toolkit: $cuda_toolkit, docker: $docker, compose: $compose,
    containerd: $containerd, nvidia_container_toolkit: $nvidia_ctk}'
REMOTE
}

capture_platform vonk-node-2 /tmp/vonk-node-2-platform.json
capture_platform vonk-node-1 /tmp/vonk-node-1-platform.json
jq -S . /tmp/vonk-node-2-platform.json
jq -S . /tmp/vonk-node-1-platform.json
cmp -s /tmp/vonk-node-2-platform.json /tmp/vonk-node-1-platform.json
```

`cmp` must exit zero. Also compare the Dashboard firmware versions exactly;
the inventory collector does not capture every firmware component. A mismatch
in DGX OS, kernel, driver, CUDA, Docker, Compose, containerd, NVIDIA Container
Toolkit, UEFI, EC, USB PD, TPM, or SoC is a hard stop.

## Update record

Fill this only after both nodes pass. Do not commit placeholder post-update
inventories.

| Field | GPU node 2 (worker) | GPU node 1 (head) |
| --- | --- | --- |
| Dashboard checked at (UTC) | 2026-08-01; exact time not recorded | 2026-08-01; exact time not recorded |
| Dashboard result | `No Available Updates` (user-confirmed) | `No Available Updates` (user-confirmed) |
| Installation completed at (UTC), or `not required` | `not required` | `not required` |
| Reboot completed at (UTC), or `not required` | `2026-08-01T20:39:02Z`; identity-repair reboot | `2026-08-01T20:47:12Z`; identity-repair reboot |
| Effective DGX OS OTA | `7.5.0` | `7.5.0` |
| Kernel | `6.17.0-1029-nvidia` | `6.17.0-1029-nvidia` |
| NVIDIA driver | `580.173.02` | `580.173.02` |
| CUDA Toolkit package | `13.0.3-1` | `13.0.3-1` |
| Docker Engine package | `5:29.2.1-1~ubuntu.24.04~noble` | `5:29.2.1-1~ubuntu.24.04~noble` |
| Docker Compose | `5.0.2` | `5.0.2` |
| containerd package | `2.2.1-1~ubuntu.24.04~noble` | `2.2.1-1~ubuntu.24.04~noble` |
| NVIDIA Container Toolkit package | `1.19.1-1` | `1.19.1-1` |
| UEFI | BIOS `5.36_0ACUM027`; Dashboard current | BIOS `5.36_0ACUM027`; Dashboard current |
| Embedded Controller | fwupd `0x03000508` | fwupd `0x03000508` |
| USB Power Delivery | fwupd GUID `dd1a238a-...`: `0x00000516` | fwupd GUID `dd1a238a-...`: `0x00000516` |
| TPM | not exposed to the running OS; Dashboard current | not exposed to the running OS; Dashboard current |
| SoC | fwupd GUID `b488217b-...`: `0x02009b0b` | fwupd GUID `b488217b-...`: `0x02009b0b` |
| Collector/schema gate | pass after reboot; boot ID changed | pass after reboot; boot ID changed |
| Host GPU gate | pass: NVIDIA GB10, 39 C | pass: NVIDIA GB10, 39 C |
| GPU-container gate | pass: GB10, driver `580.173.02`, CUDA `13.0` | pass: GB10, driver `580.173.02`, CUDA `13.0` |
| Filesystem/kernel-log gate | pass: non-empty log matched running boot | pass: non-empty log matched running boot |
| Interface-presence gate | pass: exact pre/post interface-name set | pass: exact pre/post interface-name set |

Commit `inventory/raw/node2-post-update.json`,
`inventory/raw/node1-post-update.json`, and this completed record together
only after all gates pass.

### Completed comparison

No package or firmware installation was necessary. Both Dashboards reported
no available updates; the only reboots were the required worker-first and
head-second reboots that completed the earlier machine-ID repair.

The two live normalized platform captures matched byte-for-byte across Vonk Forge
build/OTA, Ubuntu, kernel, driver, CUDA Toolkit, Docker Engine/CLI, Compose,
containerd, NVIDIA Container Toolkit, BIOS version/date, and every
fwupd-exposed firmware name, version, and GUID. The normalized post-inventory
platform fields also matched exactly across OS, kernel, total memory, total
swap, root filesystem size, earlyoom state, Compose, physical NVMe size, and
interface names/types/MTUs. Snap loop-device numbers differed because loop
assignment is runtime state; they are not physical platform fields.

TPM firmware is listed in NVIDIA's release table but no TPM device is exposed
to `tpm2_getcap` or fwupd on either running node. Exact local TPM interrogation
was therefore unavailable; both Dashboards independently reported the systems
current with no available update.

## Disable the earlyoom userspace killer

DeepSeek uses most of each GPU node's unified memory. An independently acting
`earlyoom` service could kill a distributed worker before the runtime can
preserve useful diagnostics, so it must not be active or enabled. This check
is deliberately separate from swap and memory-capacity admission controls.

The live pre-change probe on 2026-08-01 found `earlyoom` absent on both nodes:
`systemctl is-enabled` returned `not-found` with exit code 4,
`systemctl is-active` returned `inactive` with exit code 4, `LoadState` was
`not-found`, and `dpkg-query` returned exit code 1. The exact evidence is in
`inventory/reports/earlyoom.json`. Absence is an accepted safe state and is
not rewritten as `disabled`; the package must not be installed merely to
disable it.

Use the audited script to classify the full state. It treats these outcomes
as safe:

- absent: unit `not-found`, package absent, service inactive;
- disabled: loaded package, `disabled` and `inactive` with their documented
  non-zero `systemctl` status codes;
- masked: `masked` or `masked-runtime` and inactive.

A static unit, a package/unit disagreement, or any other combination is an
unexpected state and fails without modification. An installed active or
enabled service requires `--apply`, which stops before disabling and then
re-reads every state. The script is idempotent.

Stage and validate the worker first, comparing its checksum with the local
artifact:

```bash
scp nodes/bin/disable-earlyoom vonk-node-2:/tmp/disable-earlyoom
ssh -o BatchMode=yes vonk-node-2 \
  'chmod 0700 /tmp/disable-earlyoom && sha256sum /tmp/disable-earlyoom'
shasum -a 256 nodes/bin/disable-earlyoom
```

After the hashes match, run this one audited command in an interactive worker
session:

```bash
sudo bash /tmp/disable-earlyoom --apply
```

It must exit zero and print a `PASS` line. Record a fresh after-state with
exact exit codes before staging and repeating the same procedure on
`vonk-node-1`. Never claim an after-state from the pre-change observation.

If an installed service was changed and the decision is explicitly reversed,
restore its recorded prior state rather than using one generic rollback:

```bash
# Prior state: enabled and running
sudo systemctl unmask earlyoom && sudo systemctl enable --now earlyoom

# Prior state: disabled but running
sudo systemctl start earlyoom

# Prior state: masked but running
sudo systemctl unmask earlyoom && sudo systemctl start earlyoom && \
  sudo systemctl mask earlyoom
```

Do not run these commands for the observed absent state; there is nothing to
roll back. After both nodes pass, remove `/tmp/disable-earlyoom` from each
node and mark the evidence document complete.

### Completed earlyoom record

The audited apply path ran on GPU node 2 first and GPU node 1 second. Both executions
classified the service as `absent` and printed `PASS: earlyoom is absent; no
change required`; both commands exited zero. No package, unit, or host
configuration was changed. The exact interactive execution times were not
recorded and are left null rather than inferred.

Independent post-action probes at `2026-08-01T21:42:43Z` on GPU node 2 and
`2026-08-01T21:42:44Z` on GPU node 1 reproduced the before state and exact exit
codes: `LoadState=not-found`/0, `is-enabled=not-found`/4,
`is-active=inactive`/4, and absent package/1. The worker and head therefore
both meet the DeepSeek earlyoom gate without a mutation. The staged script
used SHA-256
`e9a16bce353cf85600b48dc4641db64635035c67328b8f10a2cb9d06d377657f`.

## NAS-to-GPU node platform skew

The NAS Docker services and GPU node worker code are updated as a platform
operation. In the Admin → Updates page, or with the CLI below, compare the
NAS's signed platform target with each authenticated GPU node agent:

```bash
vonkctl admin updates skew --json
vonkctl admin updates plan --target-version 2.0.0 --json
vonkctl admin updates apply --plan-digest PLAN_DIGEST --json
vonkctl admin updates status --json
```

When the NAS is newer, the UI shows the exact target digest, affected node
IDs, compatibility result, canary order, and predecessor. Nothing is sent
until an administrator explicitly confirms the signed plan. The control plane
then fans out `agent.update` over each GPU node's outbound mTLS channel using the
supervisor's A/B slots; SSH is not used for this standard path. A compatible
older agent may continue serving workload packages while the operator reviews
the skew. The workload package path remains independent and must be used for
new models, adapters, runtimes, images, checkpoints, and environments. A
workload only requires a platform update when it declares a genuinely new
privileged ABI or protocol capability.
