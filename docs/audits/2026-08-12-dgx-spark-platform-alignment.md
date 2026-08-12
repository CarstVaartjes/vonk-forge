# DGX Spark platform-alignment audit — 2026-08-12

This audit compares both development GPU nodes and the Vonk Forge runtime with
NVIDIA's supported DGX Spark operating model. It is a read-only assessment;
the observations below did not reconfigure Docker, the NVIDIA driver,
containerd, firmware, Netplan, or the direct fabric.

## Result

Both Sparks remain healthy and on the same supported platform baseline. The
material mismatch was in Vonk Forge: accepted workloads were still being
started with rootless Podman inside the system agent service. DGX Spark ships
and documents Docker Engine plus NVIDIA Container Toolkit as its supported GPU
container path. Vonk now retains rootless Podman only as an untrusted source
build sandbox and routes accepted workload import/start/stop through a narrow,
controller-signed Docker helper.

The change does not grant the agent Docker access. `vonk-agent` stays outside
the Docker group and cannot read `/run/docker.sock`. The root helper accepts
only canonical, expiring requests bound to node, job, operation, attempt,
fence, action, and SHA-256. It compiles fixed Docker arguments, verifies the
imported Linux/ARM64 image and numeric non-root user, and rejects privileged
mode, host networking, arbitrary mounts/devices, raw InfiniBand, added
capabilities, socket mounts, and unbounded resources.

## Observed platform baseline

| Surface | Spark 1 | Spark 2 | Assessment |
|---|---|---|---|
| Architecture / OS | AArch64, Ubuntu 24.04 | AArch64, Ubuntu 24.04 | Matches Spark software target. |
| DGX OS OTA | 7.5.0 | 7.5.0 | Same accepted generation. |
| Kernel | 6.17.0-1029-nvidia | 6.17.0-1029-nvidia | Same NVIDIA kernel. |
| Driver | 580.173.02 | 580.173.02 | Same driver; do not downgrade from the Dashboard-managed state. |
| Docker | 29.2.1, overlay2, systemd cgroups | same | Stock daemon configuration retained. |
| NVIDIA Container Toolkit | 1.19.1 | 1.19.1 | CDI available; both nodes passed the pinned, no-pull CUDA `--gpus all` container probe. |
| GPU | NVIDIA GB10 | NVIDIA GB10 | Unified memory is accounted from host `MemAvailable`; `nvidia-smi` memory `N/A` is expected. |
| Direct fabric | 192.168.100.10 and 192.168.101.10 | 192.168.100.11 and 192.168.101.11 | Both functions up at 200000 Mb/s, no fabric default route. |
| Agent services | active | active | No Spark 1 failed units; Spark 2 had only an unrelated stale graphical-session scope. |
| Storage | local NVMe ext4 | local NVMe ext4 | Suitable for image/model state; no SMB runtime storage. |
| Time / reboot | synchronized; no reboot required | synchronized; no reboot required | Meets certificate and signed-operation prerequisites. |
| Host security | Secure Boot and AppArmor active; cgroup v2/systemd | same | Retain the NVIDIA/Ubuntu security baseline. |
| NVMe health | 4 TB Samsung, 100% spare, 0% used, no media/error-log entries | same | Local ext4 runtime state is healthy; periodic trim is active. |
| Idle temperature | 40°C GPU / 40–41°C NVMe | 39°C GPU / 40–41°C NVMe | Cool and below any thermal warning; physical ambient remains an operator check. |

No `/etc/docker/daemon.json` customization was introduced. A named `nvidia`
runtime is not required when the installed NVIDIA Toolkit CDI path and
`docker run --gpus all` work. Vonk must not run `nvidia-ctk runtime configure`,
replace the daemon configuration, or install a second container engine for GPU
serving.

The broader repository review also found two explicitly archived surfaces.
The retired SSH-controller DeepSeek adapters use host networking, and the
two-node adapter also uses host IPC for its historical NCCL/RoCE path. Those
are legacy exceptions, not accepted recipe inputs; fresh installation never
deploys them, while source and runtime policy reject those settings. The old
node-policy helper can classify `earlyoom`, but no longer stops or disables a
platform service. The former manual fabric helper's `--apply` action likewise
refuses to write Netplan; only NVIDIA Sync owns fresh fabric configuration,
while the helper retains read-only evidence and narrow rollback behavior for
its own historical file. The archived SSH hardener remains an explicit
site-security operation guarded by a verified administrator key and recovery
channel; it is not NVIDIA Sync cluster setup.

Both NVMe devices reported historical unsafe-shutdown counters (5 and 9). They
also reported zero critical warnings, zero media errors, zero error-log
entries, no thermal warning time, and 0% endurance used. Preserve the counters
as a trend baseline and investigate only if they increase unexpectedly; they
do not currently indicate storage damage. Software could not authenticate the
physical power brick, so use of the supplied 240 W adapter and 5–30°C ambient
remain visual physical gates.

## Cross-surface corrections

- Runtime: Docker/NVIDIA is the accepted workload runtime; rootless Podman is
  build-only. Exact retries recognize an already-running container by a
  canonical policy digest rather than launching a duplicate.
- Container lifecycle: the helper uses Docker's init process, forbids registry
  pulls during launch, keeps restart policy at `no` so controller state remains
  authoritative, and selects the rotating `local` log driver with 10 MiB by
  three-file bounds. Images must already have passed digest-bound import and
  inspection before launch.
- GPU access: the trusted agent retains only the NVIDIA device visibility
  needed for inventory. Recipe build policy cannot request a device. The root
  Docker helper asks for `--gpus all` only for a GPU-bearing accepted recipe.
- Capability evidence: nodes advertise `build.rootless-podman.v1` and
  `runtime.spark-docker-nvidia.v1` separately. Inventory fails closed unless
  the Docker client works and `nvidia-ctk cdi list` contains
  `nvidia.com/gpu=all`.
- Fabric: `fabric_address` and `fabric_bandwidth_mbps` are mandatory for
  multi-node admission. Current Vonk containers use TCP over the declared
  direct address. Host RDMA benchmarks remain useful platform evidence, but
  raw RDMA and GPUDirect RDMA are not exposed or claimed.
- Endpoint isolation: only the endpoint owner receives a management-address
  publication. Non-entrypoint ranks bind their health endpoint to the accepted
  private fabric address, preventing an unrouted worker from exposing its
  unauthenticated model API on the management LAN.
- Memory: the DS4 recipe reserves its measured whole-host 120 GB peak and the
  controller retains a separate 4 GB floor. Qualification uses the same 124 GB
  threshold; it does not double-count another recipe-level host reserve.
- Installation: the Debian package depends on ordinary build/runtime support
  packages such as `acl`, Podman, and uidmap, but it does not own Docker,
  containerd, the NVIDIA driver/toolkit, firmware, or Netplan. Fresh installs
  validate those NVIDIA-owned prerequisites before package installation.
- Services: offline `systemd-analyze security` rates the packaged Rust agent,
  A/B supervisor, and privileged helper `4.8 OK`, `1.4 OK`, and `1.7 OK`.
  The agent's larger surface is the reviewed rootless-build namespace/device
  boundary; the Docker socket remains inaccessible to it and available only
  through the narrow root helper.
- Secrets: the host-runtime private signing key exists only in the NAS API
  projection and encrypted operator backup. GPU nodes receive only its public
  key at `/etc/vonk-forge-agent/host-helper-authority.pub`.
- Administration: no service account needs Docker-group membership. Human
  platform probes can use `sudo docker`; routine operations use the outbound
  agent and signed helper.
- Updates: NVIDIA Dashboard remains authoritative for DGX OS, firmware,
  kernel, driver, Docker, and NVIDIA Toolkit updates. Vonk package/image
  releases do not mutate those components.

The current lab has several operational follow-ups, none of which indicates a
damaged Spark. The two nodes run different accepted development agent package
versions after the earlier canary, so the next rollout must converge both to
one exact accepted version. This audit stopped two stale Spark 2 diagnostic
sessions, reset their obsolete failed GDM scope, and removed Spark 1's orphaned
DS4 BuildKit container plus its 11.6 GiB cache volume after verifying that no
container used it; the completed DS4 images were retained. The human `carst`
account still has root-equivalent Docker-group access from the archived SSH
controller; remove that membership only after confirming the legacy controller
is retired and use `sudo docker` for platform probes. The node firewall is
currently inactive; no Vonk workload is exposed now, but Docker-published
traffic bypasses ordinary UFW `INPUT` rules. Original-destination and
source-specific policy must be persisted in `DOCKER-USER` before a runtime port
is published. Both nodes also retain the working legacy
`99-dgx-spark-direct-fabric.yaml`; migrate that ownership to NVIDIA Sync only in
a separate maintenance window, and never add a second Netplan owner. Temporary
unattended sudo remains installed on the NAS and both
Sparks for acceptance automation and must be removed as the final physical
gate.

The first package rollout converged both nodes to the same signed Rust build
and exposed one control-plane configuration omission before any workload was
started: the development Compose publisher had no explicit direct-fabric CIDR
input, so the controller correctly rejected newly advertised fabric addresses.
The publisher now requires canonical, management-disjoint fabric CIDRs (or an
explicit single-node `none`), renders them into both API and worker policy, and
removes the template-only Compose project name so NAS UI and CLI redeploys
reuse the operator-selected project and its named volumes. The node config was
restored while that fail-closed path was corrected; Netplan and link state were
never changed.

## Physical and operational gates

Before declaring a fresh Spark production-ready, verify the supplied 240 W
power adapter, supported ambient temperature, Dashboard update state, no
pending reboot, synchronized time, local NVMe free space, CDI devices, and an
actual `docker run --rm --gpus all ... nvidia-smi` probe. Configure two-node
networking with the current NVIDIA Sync Cluster Assistant when its generated
SSH/Netplan changes meet site policy; retain this repository's reviewed manual
procedure only as a fallback and never mix both Netplan owners.

The physical canary order remains Spark 1 first, then Spark 2. A successful
unit suite is not evidence that GPU launch, TCP rendezvous, restart recovery,
route publication, or two-rank inference works on the real machines. Each
slice must pass and clean up before the next node is changed.

## NVIDIA references

- [NVIDIA Container Runtime for Docker](https://docs.nvidia.com/dgx/dgx-spark/nvidia-container-runtime-for-docker.html)
- [OS and component updates](https://docs.nvidia.com/dgx/dgx-spark/os-and-component-update.html)
- [DGX Spark release notes](https://docs.nvidia.com/dgx/dgx-spark/release-notes.html)
- [DGX Spark known issues](https://docs.nvidia.com/dgx/dgx-spark/known-issues.html)
- [DGX Spark clustering](https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html)
- [NVIDIA Sync Cluster Assistant](https://docs.nvidia.com/sync/latest/cluster-assistant.html)
- [NGC container guidance](https://docs.nvidia.com/dgx/dgx-spark/ngc.html)
- [CUDA porting notes, including GPUDirect RDMA limitation](https://docs.nvidia.com/dgx/dgx-spark-porting-guide/porting/cuda.html)
