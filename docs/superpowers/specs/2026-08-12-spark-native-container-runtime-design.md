# Spark-native container runtime design

## Outcome

Vonk Forge uses the container stack supplied and supported by NVIDIA DGX
Spark. Recipe builds remain daemonless and rootless because their Dockerfiles
are untrusted inputs. Accepted workload images are imported and run by the
preconfigured Docker Engine and NVIDIA Container Toolkit.

The `vonk-agent` account never joins `docker`, receives the Docker socket, or
runs Docker through unrestricted sudo. A root-owned, socket-activated helper
executes only controller-authorized, schema-validated runtime requests.

## Platform contract

A supported node is ARM64 Ubuntu 24.04 on DGX OS with:

- Docker Engine and the NVIDIA Container Toolkit already configured;
- a successful `docker run --rm --gpus all ... nvidia-smi` preflight;
- the NVIDIA GB10 unified-memory model;
- digest-pinned Linux/ARM64 images with a numeric non-root user;
- ConnectX-7 configuration created with NVIDIA Sync Cluster Assistant when
  available, followed by Vonk Forge's measured link, RoCE, and NCCL gates.

DGX Dashboard is the preferred platform-update authority. The Vonk Forge APT
repository updates only the agent package; it does not replace the DGX OS,
driver, firmware, Docker, or Container Toolkit lifecycle.

GB10 capacity uses host `MemAvailable` as the no-swap admission value and
counts unified memory once. It does not depend on the unsupported
`nvidia-smi` framebuffer-memory fields. Swap is not added to admission even
though NVIDIA documents it as potentially reclaimable: this is an intentional
availability margin, not a discrete-GPU assumption.

DGX Spark does not support GPUDirect RDMA. Vonk Forge may use the validated
ConnectX-7 RoCE/NCCL `NET/IB` transport, but must not require
`nvidia-peermem`, dma-buf, GDRCopy, or an undocumented GDR level.

## Authority and data flow

The existing host-helper Ed25519 authority gains one operation:
`execute-container-runtime-request`. Its grant binds:

- the certificate-bound node;
- the active job, operation attempt, and fence;
- one typed runtime action;
- the SHA-256 digest of one canonical request document;
- a maximum 300-second validity period that may never extend beyond the active
  operation-attempt lease.

The agent writes the canonical request beneath its managed runtime-request
directory with mode `0600`, sends only its digest and active claim identity to
the authenticated controller endpoint, receives a short-lived signed grant,
and submits that grant to the root helper. The helper consumes each grant once,
reopens the request without following links, verifies stable identity and the
signed digest, validates every field, and then invokes only `/usr/bin/docker`
with compiled arguments.

The controller issues a grant only while the exact certificate owns the
running operation attempt. The requested action must match the operation:

| Agent operation | Permitted helper action |
|---|---|
| `recipe.image.import.v1` | import and verify one Docker-loadable image archive |
| `recipe.install` | inspect one accepted image |
| `recipe.start` | run one managed workload or lifecycle hook; inspect that exact managed run during readiness; stop it on failed readiness |
| `recipe.stop` | remove one managed workload or lifecycle hook |

Uninstall removes only agent-owned installation metadata. Docker image garbage
collection remains an explicit, separately authorized maintenance concern so
one installation cannot remove an image still used by another.

## Runtime request policy

The helper accepts exact, deny-unknown-field documents. It derives Docker
arguments rather than accepting argv. In particular it enforces:

- names and labels under the `vonk-` managed namespace;
- digest-pinned images already imported by a signed image-import operation;
- `linux/arm64`, runtime-interface label `v1`, and the declared numeric
  non-root image user;
- `--read-only`, `--cap-drop=ALL`, `no-new-privileges`, no privileged or host
  namespaces, Docker init, and restart policy `no`;
- one bounded `/tmp` tmpfs with `rw,nosuid,nodev,mode=1777`; this preserves the
  read-only root while supporting ordinary runtime lock and temporary files;
- `--pull never` after digest-bound import and inspection, plus Docker's
  rotating `local` log driver with fixed size/file bounds;
- bounded memory and PIDs;
- only explicitly address-bound, non-loopback TCP publications and managed
  model, state, and runtime-contract mounts beneath
  `/var/lib/vonk-forge-agent`;
- no Docker/container socket, arbitrary host path, secret directory, or
  controller credential mount;
- logical `nvidia.com/gpu=all` mapped to Docker CDI `--device nvidia.com/gpu=all`; no raw NVIDIA
  device list is accepted from a recipe;
- no raw RDMA device exposure in the first runtime version; distributed
  profiles use the validated TCP fabric path until a separately qualified
  Spark-native NCCL transport policy exists.

The helper captures bounded diagnostics internally but returns only typed
status/evidence to the agent. User-provided strings never become shell input.

During readiness, the agent sends the exact hardened run request every ten
seconds under a distinct controller-authorized `run-inspect` action. The helper
validates the complete request and accepts it only while the same labeled
managed container is still running; an absent, exited, or substituted container
fails readiness immediately. `run-inspect` has no create/start/stop path, so the
guard cannot restart or replace a workload. It remains separate from the
renewable controller lease needed for long artifact verification and model
startup.

## Build boundary

Recipe builds continue to use operation-private rootless Podman storage with
no Docker socket, GPU, host mount, privileged mode, or private-network access.
Podman uses the dedicated account's lingering user systemd manager for cgroup
v2 delegation. The agent service retains `ProtectControlGroups=yes`; persistent
homes remain inaccessible, while its own mode-`0700` `/run/user/<uid>` runtime
stays writable because Podman maintains `libpod` state there. Unix ownership
keeps every other user's runtime private. Podman alone receives the effective
UID's runtime path and user D-Bus address. The build command explicitly selects
the systemd cgroup manager.
`AF_NETLINK` is available only because `runc` requires it to create the
isolated network namespace; the source build still runs with `--network=none`.
The resulting OCI image is exported through Podman's `docker-archive`
transport and uploaded by archive digest. The target node downloads the exact
Docker-loadable archive, verifies size and SHA-256 before requesting authority,
and the helper verifies the Docker-loaded image identity before reporting
success. The v1 wire/database field `oci_layout_sha256` is retained for
compatibility; for this Spark Docker backend it binds the complete immutable
Docker archive, not an OCI-layout tar.

This split follows Spark's supported runtime without granting an untrusted
Dockerfile access to the rootful host daemon.

## Service hardening

The unprivileged agent no longer needs workload GPU devices or Docker access.
It retains only the minimum host visibility needed for inventory and the
rootless build sandbox. The root helper receives the Docker CLI/socket paths
and network/device visibility required by its compiled runtime operations, but
keeps a closed executable allowlist, no shell, bounded execution, signed
one-shot grants, and managed read/write paths.

The Debian maintainer lifecycle enables lingering for `vonk-agent` without
starting the network agent and disables lingering on package removal. This
makes the rootless cgroup authority available after boot and before any source
build without adding the account to sudo or Docker.

The documented operator preflight and runtime capability advertisement fail
closed if Docker, the NVIDIA Container Toolkit, or the configured GPU runtime
is absent. The Debian package deliberately does not depend on or reconfigure
the Spark-owned Docker daemon, NVIDIA Toolkit, driver, kernel, or firmware.

## Acceptance

CI must prove protocol parity between Python and Rust, grant binding and
replay rejection, request digest/ownership checks, Docker argv compilation,
GPU mapping, mount/path rejection, image verification, and no Docker access in
the agent unit.

Physical acceptance is canary-first on Spark 1:

1. validate NVIDIA's Docker GPU command;
2. publish and activate the signed package;
3. complete synthetic build, image import, install, start, readiness, stop,
   and uninstall;
4. verify limits, non-root identity, no Docker socket in the workload, and
   restart persistence;
5. repeat on Spark 2 and then run the two-node TCP-over-direct-fabric slice.

No temporary sudoers entry or diagnostic artifact is removed until all
acceptance and recovery checks are complete.
