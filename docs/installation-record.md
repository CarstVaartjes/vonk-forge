# Vonk Forge GPU node Installation Record and Lessons Learned

This is the chronological as-built record for the two-node Vonk Forge GPU node cluster
through secure host preparation and validated RDMA/NCCL fabric. It is the
starting point for a rebuild or a third-party review. The linked runbooks remain
the source for exact commands, safety checks, rollback procedures, and expected
output.

The v1 recipe catalog, execution-harness path, and live node-health path are implemented.
The opening sections record the pre-runtime baseline; later sections record the
installed Mia and DS4 runtimes. Recipe acceptance remains revision- and
mapping-specific.

## Final baseline

| Item | GPU node 1 / head | GPU node 2 / worker |
| --- | --- | --- |
| Hostname | `node-3542` | `node-2297` |
| Mac SSH alias | `vonk-node-1` | `vonk-node-2` |
| Management address | `192.168.1.211` | `192.168.1.212` |
| Role | head | worker |
| Memory | 130,663,231,488 B | 130,663,231,488 B |
| Root filesystem | 4,031,871,553,536 B | 4,031,871,553,536 B |
| NVIDIA DGX OS OTA | `7.5.0` | `7.5.0` |
| Kernel | `6.17.0-1029-nvidia` | `6.17.0-1029-nvidia` |
| NVIDIA driver | `580.173.02` | `580.173.02` |
| CUDA Toolkit package | `13.0.3-1` | `13.0.3-1` |
| Docker / Compose | `29.2.1` / `5.0.2` | `29.2.1` / `5.0.2` |
| `earlyoom` | absent and inactive | absent and inactive |

Both hosts use the same Linux account, `carst`. The management addresses are
stable on the LAN, while the direct fabric has its own static addresses and no
default route. The machine-readable final topology is in
[`inventory/cluster.toml`](../inventory/cluster.toml).

## Installation sequence

### 1. Detect and repair cloned host identities

The two factory installations unexpectedly had the same `/etc/machine-id` and
the same RSA, ECDSA, and Ed25519 SSH host keys. We treated the original
identities as compromised, identified each physical machine using its chassis
and DMI serial rather than its cloned keys, then regenerated the machine ID and
all host keys one physical GPU node at a time from a local console.

This problem was discovered after the first password-authenticated SSH
inspection. That was sufficient to expose the duplicate but was not a trusted
identity ceremony. The corrected rebuild procedure now checks the NVIDIA
security update and performs identity inspection from a local keyboard/display
before entering a password over SSH or accepting either factory host key.

Each new host fingerprint was compared between the trusted console and a
filtered network scan before accepting it. Each GPU node was rebooted once after
machine-ID rotation because the already-running journal service continued to
use the old machine-ID directory until reboot.

The complete identity gate, guarded backup, rollback, known-hosts replacement,
and cleanup procedure is in [SSH bootstrap](runbooks/ssh-bootstrap.md).

### 2. Install the 1Password-managed administration key

We created a dedicated Ed25519 key named `Vonk Forge GPU node Admin` in 1Password and
enabled the 1Password SSH agent. Only its public key was exported to
`~/.ssh/vonk_node_admin.pub`; no unencrypted private key was written to
`~/.ssh`, and SSH agent forwarding was not enabled.

The public key was installed on both nodes with:

```bash
ssh-copy-id -f -i "$HOME/.ssh/vonk_node_admin.pub" vonk-node-1
ssh-copy-id -f -i "$HOME/.ssh/vonk_node_admin.pub" vonk-node-2
```

The `-f` flag was necessary because the local file contains only the public
half and the private half remains in 1Password. Fresh `BatchMode=yes` sessions
proved key-only access before password authentication was changed.

### 3. Harden SSH without locking out recovery

We kept one authenticated session and a local-console route open on each node,
hardened GPU node 2 first, verified a fresh key-only connection and a negative
password-only test, and then repeated the process on GPU node 1. The effective
server policy is:

```text
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin prohibit-password
```

The checksum gate, installer, verification, and rollback are in
[SSH hardening and recovery](runbooks/ssh-recovery.md).

### 4. Confirm the platform version and reboot sequentially

After identity rotation and SSH hardening, NVIDIA DGX Dashboard confirmed both
Founders Edition systems were on NVIDIA DGX OS OTA `7.5.0` and reported
`No Available Updates`. The worker was validated and rebooted before the head.
This reboot also moved journald onto the newly generated machine-ID directory.
Later host changes continued to use worker-first ordering wherever possible so
GPU node 1 remained the recovery point while GPU node 2 was validated.

The detailed maintenance, recovery-media, reboot, and validation procedure is
in [Platform operations](runbooks/platform-operations.md).

### 5. Capture the pre-change platform inventory

We recorded host, storage, memory, NVIDIA, Docker, networking, RDMA, and thermal
state before fabric changes. Both nodes passed the memory, swap, and disk gates.
The initial collector could not represent Vonk Forge GPU node unified GPU memory because
`nvidia-smi` reports that field as `N/A`; driver and temperature were therefore
verified separately. Initial RDMA fields were intentionally incomplete because
the fabric was not configured yet.

The commands and observed baseline are in
[Pre-change inventory](runbooks/inventory.md).

### 6. Validate CUDA through the NVIDIA container path

CUDA support was already present through the Vonk Forge driver and NVIDIA Container
Toolkit even though a host `nvcc` command was not the correct first readiness
test. We pulled and ran the versioned ARM64 CUDA development image
`nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04`; it detected the GB10 GPU and
reported driver `580.173.02` and CUDA `13.0` on both nodes. The first pull was
large; subsequent validation reused the local image.

This distinguishes three separate components that are easy to conflate: the
host driver, the optional host CUDA Toolkit, and the container's CUDA user-space
runtime. The exact validation gate is in
[Platform operations](runbooks/platform-operations.md).

### 7. Confirm the memory-killer prerequisite

The upstream DeepSeek guidance warns that `earlyoom` may kill a distributed
worker during transient unified-memory pressure. The package and service were
already absent on both nodes, so the guarded disable script made no change and
accepted `absent` as a safe final state. The recorded evidence is
[`inventory/reports/earlyoom.json`](../inventory/reports/earlyoom.json).

### 8. Configure the direct ConnectX-7 fabric

Cable EEPROM inspection identified the same 1 m Amphenol `NJAAKK-C106` passive
copper PAM4 DAC from both ends. Its exact OEM identifier was not found in the
public NVIDIA compatibility list, so acceptance was based on observed behavior:
both functions reported the same negotiated 200 Gb/s physical-link state, and
both passed non-fragmenting traffic in both directions. These are two PCIe/RoCE
functions of one QSFP link, not two independent 200 Gb/s links.

The NVIDIA Sync UI was not used for the final configuration. It expected to
bootstrap SSH using a password and did not import the already-hardened
1Password SSH configuration. We instead used the audited manual procedure based
on NVIDIA `dgx-spark-playbooks` commit
`1fb66f059ee427c5a3678b3117ef73aab042b458`, applying GPU node 2 first with
`netplan try`, validating management access, and only then applying GPU node 1.

The final physical link is exposed through these two addressable functions:

| Function label | GPU node 1 | GPU node 2 | Interfaces / HCA | RoCE GID | MTU |
| --- | --- | --- | --- | ---: | ---: |
| 100 | `192.168.100.10` | `192.168.100.11` | `enp1s0f1np1` / `rocep1s0f1` | 3 | 1500 |
| 101 | `192.168.101.10` | `192.168.101.11` | `enP2p1s0f1np1` / `roceP2p1s0f1` | 3 | 1500 |

Neither function-addressed subnet has a default route. Because the cable is a direct back-to-back
link, there is no Ethernet switch requiring PFC, ECN, DSCP, or switch-side MTU
configuration. The exact Netplan procedure, cluster-only SSH key, rollback,
and postchecks are in [Direct ConnectX-7 fabric](runbooks/fabric.md).

### 9. Install and validate RDMA and NCCL tooling

Both nodes received the pinned native ARM64/CUDA toolchain required for a real
two-rank test: OpenMPI `4.1.6-7ubuntu2`, CUDA 13 `nvcc`, NCCL `2.30.7-1`, and
MPI-enabled `nccl-tests` from pinned source commits. The cluster uses a separate
head-to-worker SSH key restricted to the two fabric source addresses; it does
not reuse or forward the 1Password administration key.

The final acceptance run on 2026-08-02 proved the one physical link rather than
adding two sequential function results. Simultaneous `ib_write_bw` traffic on
both functions reached 185.14 Gb/s in each direction, above NVIDIA Sync's
184 Gb/s lower bound for a 200 Gb/s GPU node link. Sequential function diagnostics
were 108.88–109.02 Gb/s write and 80.42–80.43 Gb/s read. Fixed 8-byte,
10,000-iteration write-latency runs averaged 1.80–1.98 us with p99 of
2.03–2.22 us, and every monitored RDMA error counter remained zero. The
accepted NCCL `all_reduce_perf` run selected both RoCE HCAs using `NET/IB`,
completed with zero out-of-bounds values, and reported 19.308 GB/s average bus
bandwidth. Full command evidence and distributions are in
[`inventory/reports/rdma-nccl.json`](../inventory/reports/rdma-nccl.json).

### 10. Enable controller container access and validate live health

On 2026-08-02, `carst` was added to the `docker` group on both dedicated
GPU nodes so `vonkctl` can start and stop profile containers noninteractively.
Docker-group membership is root-equivalent access and is intentionally limited
to this trusted administration account. Both sessions were closed and reopened
after the group change before validation.

Both nodes then reported `docker` in `carst`'s groups and Docker Server
`29.2.1`. The pinned
`nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04` container saw the NVIDIA GB10
with driver `580.173.02` on each node. A fresh
`vonkctl nodes status --json` exited `0`: both nodes were `healthy`, with no
warnings or errors, Docker available, GPU temperature 39 C, and both fabric
functions reporting speed `200000`, MTU 1500, and RDMA state `ACTIVE`.

### 11. Install and verify the Mia dual-GPU node DeepSeek runtime

On 2026-08-02, both nodes received immutable runtime release
`92f5ae51cc5410cae7b19541e433acba4b38a11ec89bca45c91b4da2a9b0575e`.
It pins MiaAI-Lab commit `b131b2a22164675890dd1465fd8862b5cfb6ff13`,
the DeepSeek checkpoint revision
`9e165c30e2704aec5d9d593cce3eebd58bbef1cb`, and the Anemll image by
digest. Preparation and verification ran on both local model caches in
parallel. The worker started first, followed by the head; the API binds only to
GPU node 1 loopback and serves the stable model ID `deepseek`.

The live startup record proves TP=2, PP=1, the `mp` executor, MTP-5, padded
`nvfp4_ds_mla`, and a 1,048,576-token limit. Each rank loaded 79.17 GiB. The
shared KV pool measured 1,787,827 tokens, deriving one admitted simultaneous
full-context request. All 11 versioned output-quality gates passed, including
reasoning modes, streaming, tool use and the historical >411-token regression.

The pinned Mia quick benchmark recorded 1,997 input tok/s and 67.0 output
tok/s at 2K/C1. Three cache-distinct C3 observations were 85.8, 88.8 and 100.0
aggregate tok/s. These are retained as an operational baseline, not final
performance acceptance: natural completion lengths varied substantially, and
the user chose to reserve performance fine-tuning for the final cross-model
optimization phase. Exact evidence and deferred gates are in
[`inventory/reports/deepseek-mia-operational.json`](../inventory/reports/deepseek-mia-operational.json).

### 12. Verify the DS4 single-GPU node DeepSeek runtime

On 2026-08-03, GPU node 1 received immutable DS4 release
`ca69bf50d544856357716d4f326dfd88a6c2d1f40f8fb9cfce426f60858482b2`.
It pins Entrpi DS4 commit `4ad370b4a338efe9723a386673c0e04f6e214108`,
the Q2-imatrix base from `antirez/deepseek-v4-gguf` revision
`1cd7b564460821938add0475a60b942c409295e0`, the DSpark drafter from
`bleysg/DeepSeek-V4-Flash-DSpark-drafter-GGUF` revision
`81c6fdd38f9582da45ba27f0ed7b63bcd3ea3b62`, and the ARM64 image
`ghcr.io/carstvaartjes/spark-ds4@sha256:084d9a9ffa47431842c5dec84de97b058034dec0535b2a563bc5db78c9e14615`.
Both GGUFs were rehashed inside a read-only, network-disabled container before
the live runtime was started.

The 32,768-token loopback API served the stable model ID `deepseek`. All 12
single-node quality gates passed: English, script, repetition, XML, streaming,
reasoning off/low/high/max, structured tool calling, an 8,204-token prompt, and
a second tool-result turn that reused 2,441 cached prefix tokens while computing
only 15. Startup used 474 in-process derived artifacts without a full host copy;
the measured cold start was 67 seconds, peak observed temperature was 50 C, and
the live DSpark counters accepted 193 of 246 drafts (`0.7846`).

The definition is `verified`, not `accepted`. Stable performance thresholds,
the sustained thermal run, three-cycle lifecycle acceptance, reboot/no-autostart
acceptance, and exact co-resident profile tests remain deferred. MXFP4 is also
deferred; this evidence covers only the Q2-imatrix lane. The operational record
is [`inventory/reports/deepseek-ds4-operational.json`](../inventory/reports/deepseek-ds4-operational.json).
After recording it, DS4 was stopped with memory recovery and the default Mia
worker and head were explicitly restored worker-first; both hardened health
checks passed and `/v1/models` again advertised only `deepseek`.

## Lessons learned

| Lesson | Operational rule retained in the repository |
| --- | --- |
| Factory-installed systems may not have unique identities. | Compare machine IDs and every configured SSH host key before first remote trust; repair one physical unit at a time using chassis/DMI serials. |
| Regenerating `/etc/machine-id` while booted does not move the current journal immediately. | Reboot and revalidate the worker first, then the head, before trusting current-boot journal checks. |
| A 1Password SSH key does not require a private key file in `~/.ssh`. | Export only the public half, use the 1Password agent explicitly, and use `ssh-copy-id -f` for installation. |
| A failed password attempt is not proof that password authentication is disabled. | Inspect the server's advertised authentication methods as well as proving a fresh public-key login. |
| `nvidia-smi` showing CUDA support and `nvcc` being absent are not contradictory. | Validate the Vonk Forge container path first; distinguish the host driver, host Toolkit, and container CUDA runtime. |
| A GUI helper is not mandatory when it conflicts with an already-hardened SSH design. | Use the pinned NVIDIA manual playbook, preserve strict host-key checking, and document the deviation. |
| A physical QSFP connection exposes two usable network/HCA functions here. | Inventory and pin both interface/HCA/GID consumers, but never add their duplicated 200 Gb/s link-state reports as if they were independent physical links. |
| A cable part number alone did not establish support. | Record the EEPROM identity, negotiated rate, MTU behavior, bidirectional RDMA, and NCCL transport evidence. |
| `earlyoom` absence is already a valid safe state. | Treat `absent`, `disabled and inactive`, or `masked and inactive` as explicit accepted states; do not install it merely to disable it. |
| Worker-first ordering materially limits blast radius. | Apply host, update, SSH, fabric, and runtime changes to GPU node 2 first and stop before GPU node 1 if validation fails. |
| Noninteractive container lifecycle requires Docker daemon access. | Grant the trusted `carst` administrator Docker-group membership on these dedicated hosts, reconnect before testing, and treat that membership as root-equivalent. |

## Evidence and remaining scope

The installation baseline is complete when all of the following remain true:

- key-only Mac administration succeeds for both aliases and password-based SSH
  remains disabled;
- both hosts remain on matched supported platform revisions;
- `earlyoom` remains absent or otherwise disabled and inactive;
- both fabric functions retain their static addresses, GID index 3, MTU 1500, and
  no default route;
- [`scripts/validate-fabric`](../scripts/validate-fabric) reproduces the RDMA
  aggregate-bandwidth, latency, error-counter, and NCCL acceptance result;
- `vonkctl nodes status --json` reports both nodes healthy with Docker
  available and no warnings or errors.

The common controller/profile framework and live-health integration are now
implemented. The Mia dual-GPU node and DS4 single-GPU node DeepSeek runtimes are
installed and quality-verified, but deliberately remain `verified` rather than
`accepted` until final performance, thermal, lifecycle and reboot gates run.
Mia is the restored home runtime; no accepted profile may select DS4 yet. The
next model-runtime phase is the remaining model catalog. Caddy, LiteLLM, the
browser UI, and optional Tailscale ingress are implemented as separate,
tested Compose services; their physical deployment remains deferred until the
new external container host is available. None is required for initial local
model testing.
