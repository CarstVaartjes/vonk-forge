# Dual Vonk Forge GPU node Model Platform Design

> **Historical two-GPU node baseline.** This document records the original
> physical lab inventory and early bring-up choices. It is superseded for
> architecture and operations by the [scalable GPU node platform and control
> plane design](2026-08-03-scalable-node-platform-control-plane-design.md)
> and the later clean-slate Fleet/Library control-plane design.
> Its addresses, names, and SSH/bootstrap procedures are not application
> defaults and must not be copied into a generalized fleet deployment.

Date: 2026-08-01

## Purpose

Configure two Vonk Forge GPU node systems as a reliable local model platform. The primary workload is `deepseek-ai/DeepSeek-V4-Flash-0731` running across both GPU nodes. The platform also runs the complete required model set defined in the [multi-runtime profile design](2026-08-02-multi-runtime-model-profiles-design.md), including Nemotron, Qwen image and vision models, Pixal3D, TRELLIS.2, rigging, and the requested alternative 3D generators. It exposes suitable model APIs, provides a browser UI later, and supports secure Tailscale access later.

This specification uses measurable defaults and acceptance gates. A measured value may replace a provisional threshold only when the command, result, date, and reason are committed to the private inventory or benchmark record.

## Current Environment

The completed host and fabric preparation is recorded chronologically in the
[installation record and lessons learned](../../installation-record.md). The
detailed runbooks and checked-in evidence remain the operational source of
truth.

- GPU node 1 LAN address: `192.168.1.211`
- GPU node 2 LAN address: `192.168.1.212`
- Linux user on both systems: `carst`
- Both LAN addresses are static.
- One 1 m Amphenol `NJAAKK-C106` passive copper cable directly connects the two systems. Its exact OEM identifier is not in the public NVIDIA compatibility list, but its two PCIe/RoCE functions report the same 200 Gb/s physical-link state. Simultaneous traffic across both functions reached 185.14 Gb/s in each direction and passed RDMA, latency, error-counter, and NCCL acceptance.
- The administration computer is a Mac using the 1Password SSH agent.
- The dedicated Ed25519 key named `Vonk Forge GPU node Admin` is installed on both GPU nodes. Fresh key-only access passes, password and keyboard-interactive SSH are disabled, and the private key remains in 1Password.
- A Synology DS218+ exists but is not part of the initial deployment. A new NAS or other external container host will be added later for Caddy, the controller, UI, LiteLLM, and Tailscale ingress.
- Both systems have a 4,031,871,553,536-byte root filesystem, more than 3.78 TB free at baseline, NVIDIA DGX OS OTA `7.5.0`, kernel `6.17.0-1029-nvidia`, driver `580.173.02`, CUDA Toolkit package `13.0.3-1`, Docker `29.2.1`, and Compose `5.0.2`.
- `earlyoom` is absent and inactive on both nodes.
- The direct one-link/two-function RoCEv2 fabric is configured with MTU 1500, GID index 3, no default route, a passing 185.14 Gb/s simultaneous aggregate in each direction, and a passing two-rank NCCL result of 19.308 GB/s average bus bandwidth.

## Goals

1. Establish secure, key-based administration from the Mac to both GPU nodes.
2. Update and inventory both systems before changing cluster networking.
3. Configure and validate the direct ConnectX-7 fabric with NVIDIA-supported tooling.
4. Validate NCCL/RoCE communication independently of any model runtime.
5. Serve `deepseek-ai/DeepSeek-V4-Flash-0731` across both nodes with vLLM tensor parallelism.
6. Support explicit Cluster Profile switching and measured co-residency for the exact Model Definition sets declared in the multi-runtime design, while keeping DeepSeek 0731 as the default agent.
7. Provide one stable authenticated API endpoint and a browser interface.
8. Add Tailscale access only after the LAN deployment is stable.

## Non-goals

- Kubernetes, Slurm, or Docker Swarm during the initial deployment.
- Assuming arbitrary Model Definition sets can run concurrently before the exact N-way set in a named Cluster Profile passes co-residency acceptance.
- Loading model weights from the NAS during inference.
- Public internet exposure or router port forwarding.
- Automatic operating-system, firmware, container, model, or distributed-profile updates.
- Running unreviewed remote installation scripts directly from a pipe to a shell.
- Hiding low-level Docker, SSH, NCCL, or vLLM behavior behind a custom orchestration daemon.
- Running Caddy, the profile controller, browser UI, LiteLLM, Tailscale ingress, or general-purpose monitoring containers on either GPU node.

## Prerequisites and Inventory

Before model installation, the repository records the following for each GPU node:

- hostname, LAN address, NVIDIA DGX OS, kernel, firmware, NVIDIA driver, CUDA, Docker, and Compose versions;
- installed and free memory, swap configuration, SSD model, SSD capacity, filesystem, and free bytes;
- `earlyoom` package, enabled, and active state;
- LAN and fabric interface names, MTU, link mode and rate, HCA name, RoCE version, GID index, and fabric IP;
- the resolved values consumed by `NCCL_SOCKET_IFNAME`, `NCCL_IB_HCA`, `NCCL_IB_GID_INDEX`, `TP_SOCKET_IFNAME`, and `GLOO_SOCKET_IFNAME`;
- cable part number and supported link rate;
- current boot ID and thermal/throttling state.

The direct back-to-back link has no Ethernet switch, so switch-side PFC, ECN, or DSCP configuration is not required. Both fabric ends must use the same validated MTU. The fabric has no default route and permits traffic only between the two fabric addresses.

The repository is private. Fabric topology and LAN addresses remain in the checked-in inventory only while that is true; any later public release must exclude or sanitize the inventory.

### Quantitative host gates

The following initial gates apply before a heavyweight profile starts:

| Gate | Required value |
| --- | ---: |
| Available memory before DeepSeek start | at least 100 GiB per node |
| Swap in use before DeepSeek start | at most 1 GiB per node |
| Free disk before the first 0731 snapshot download | at least 350 GiB per node |
| Free disk after model, encoder, image, and JIT caches are complete | at least 150 GiB per node |
| Memory recovery after a profile stop | within 5 GiB of the recorded clean baseline within 120 seconds |
| Fabric MTU | identical on both ends; exact value taken from the NVIDIA-validated configuration |

The pinned 0731 revision contains 166,898,660,330 bytes of repository files, including 166,886,535,336 bytes of SafeTensors. Each node stores the complete snapshot even though TP=2 partitions runtime weight allocations. The installed SSD variant is therefore a hard inventory item, and unused Hugging Face revisions and build caches are never allowed to accumulate without a size report.

Upstream recommends disabling `earlyoom` because it can kill a vLLM head or worker during transient unified-memory pressure. The initial host setup records its prior state, then runs `sudo systemctl stop earlyoom` and `sudo systemctl disable earlyoom` on both nodes before any DeepSeek profile. `systemctl is-active earlyoom` and `systemctl is-enabled earlyoom` must both report a non-running/non-enabled state for DeepSeek acceptance. Memory and swap remain monitored; disabling `earlyoom` is not treated as permission to overcommit the hosts.

## Architecture

### Administration plane

The Mac is the trusted administration workstation. The `Vonk Forge GPU node Admin` private key is held by the 1Password SSH agent. The security property is: no unencrypted private-key material exists in `~/.ssh`, and no private-key file is usable without unlocking the 1Password vault. Only the public key is installed in `carst`'s `authorized_keys` on each GPU node.

SSH host aliases provide stable names for the two LAN addresses, select the 1Password agent explicitly, set `IdentitiesOnly yes`, and select only the dedicated Vonk Forge key. The first implementation step installs that public key on both GPU nodes using the existing Linux password and verifies fresh key-authenticated sessions before password authentication is disabled.

Cluster jobs require separate node-to-node SSH credentials. These credentials are generated on the GPU nodes, are not reused for Mac administration, and are restricted to the private cluster fabric where the supported tooling permits. SSH agent forwarding is not used.

### Compute plane

GPU node 1 is the head node and GPU node 2 is the worker. DeepSeek runs as one logical vLLM service with tensor parallel size two, pipeline parallel size one, and the `mp` distributed executor. TP=2 is mandatory for this checkpoint: the snapshot is about 155.44 GiB, or about 77.72 GiB of weight payload per rank before runtime workspaces and metadata.

Inter-node model traffic uses the direct ConnectX-7 fabric. During initial AI bring-up, vLLM and TRELLIS.2 bind to loopback and the Mac reaches them through SSH tunnels. After the new external host arrives, Caddy, the profile controller, browser UI, optional LiteLLM, Tailscale ingress, and any later general-purpose monitoring services run there.

The installed fabric uses the official manual two-DGX-Spark procedure from pinned
NVIDIA `dgx-spark-playbooks` commit
`1fb66f059ee427c5a3678b3117ef73aab042b458`. NVIDIA Sync Cluster Assistant was
not used because its setup flow expected password-based SSH bootstrap and did
not import the existing hardened 1Password SSH configuration. The manual path
retained strict host-key checking, worker-first rollout, `netplan try`, and the
same topology, addressing, RDMA, and NCCL acceptance gates.

### Storage plane

Each GPU node keeps its own complete, verified local model cache. In this document, **verified model cache** means:

1. a pinned Hugging Face snapshot revision;
2. a checked-in manifest containing every required relative filename, byte count, and SHA-256 value from repository/LFS metadata;
3. a local verification run that hashes every file and reports no missing, extra-required, size-mismatched, or hash-mismatched file; and
4. the required `encoding/encoding_dsv4.py` file is present and hashes correctly.

The same verified model revision, encoder, container digest, and runtime configuration are present on both nodes before a distributed service starts. `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and `HF_HUB_DISABLE_XET=1` are hard serving requirements after cache preparation; they prevent an incomplete worker cache from silently re-downloading data and saturating its disk.

The NAS may store configuration backups, benchmark results, and optional download archives. It is not mounted into the model, KV-cache, JIT, or generated-artifact hot path. Caddy does make the NAS an explicit availability dependency for client API access, but not for model execution or CX-7 traffic. Text inference traffic is far below the NAS's 1 GbE capacity; large TRELLIS.2 artifacts are measured separately and may use a separately authenticated direct download route if the gateway becomes a demonstrated bottleneck.

### Access plane

The initial phase has no gateway: vLLM binds to `127.0.0.1:8888` on GPU node 1 and TRELLIS.2 binds to loopback on GPU node 2; the Mac uses SSH local forwarding for direct validation. No AI service binds to a LAN-facing address during this phase.

When the new NAS or external container host is available, Caddy becomes the stable client endpoint at `https://node-gateway.home.arpa:8443`. The host receives a static DHCP reservation or static LAN address and the name is installed in local DNS. Caddy uses an internal/private CA certificate whose root is installed only on approved clients. It enforces bearer API keys, serves controller status, actively health-checks the advertised upstream, and has two atomic route states:

- **active:** proxy the advertised profile to its firewall-restricted LAN upstream and return HTTP 503 if that upstream becomes unhealthy;
- **draining/maintenance:** reject new inference with HTTP 503 and `Retry-After: 30` while existing proxied requests receive their configured grace period.

After gateway deployment, clients and the browser UI always use Caddy. The vLLM upstream then binds on GPU node 1's LAN address, but its host firewall permits port 8888 only from the external gateway and local host. Caddy is limited to 0.5 CPU and 256 MiB of memory and may start when the control host boots, always using the fail-closed maintenance configuration until the controller advertises a healthy profile. The external host is a prerequisite for this phase; neither GPU node is a gateway fallback.

The profile controller is a one-shot container on the same external control host as Caddy. It changes Caddy state through a private container network and uses dedicated restricted SSH keys to invoke a root-owned `node-nodectl` forced command on each GPU node. That command accepts only the explicit runtime operations required by the controller; it does not provide a general shell. Caddy's admin API is reachable only on the private container network and is never exposed on the LAN.

The browser UI is added only after the new external host exists and the direct API passes correctness and load gates. That host must have a supported container runtime, at least 4 GiB installed memory, at least 2 GiB available memory before start, and at least 20 GiB free disk. Its UI container is limited to 1 CPU and 2 GiB.

LiteLLM is optional and deferred until routing multiple simultaneously active endpoints provides value. If deployed, the DS218+ must have at least 6 GiB installed memory and 3 GiB available before LiteLLM plus the UI start. LiteLLM is limited to 1 CPU and 1 GiB in addition to the UI allocation.

Tailscale is added after LAN acceptance as a container or signed-package installation on an external gateway host rather than through its convenience `curl | sh` installer. Remote clients use a named Tailscale Service and, where needed, a restricted subnet route protected by grants or ACLs. No Tailscale daemon is required on the GPU nodes initially, and no GPU node API port is exposed directly to the public internet.

### Port and bind map

| Service | Node | Port | Bind/source scope | Authentication |
| --- | --- | ---: | --- | --- |
| Administrative SSH | both | 22/TCP | LAN; approved admin clients | Ed25519 public key |
| Cluster SSH | both | 22/TCP | fabric peer only | separate cluster key |
| Controller SSH | both | 22/TCP | external control-host source only | forced-command controller key |
| Caddy API/status, future | external host | 8443/TCP | LAN, later Tailscale | private CA TLS plus bearer key |
| Caddy admin API, future | external host | 2019/TCP | private container network only | controller-network isolation |
| vLLM API, initial | GPU node 1 | 8888/TCP | loopback only through SSH tunnel | vLLM API key plus SSH |
| vLLM API upstream, future | GPU node 1 | 8888/TCP | GPU node 1 LAN; firewall source gateway and local host only | vLLM API key plus proxy isolation |
| vLLM `mp` rendezvous | both | 25000/TCP | fabric peer only | network isolation |
| NCCL/Gloo/TP runtime traffic | both | runtime-selected | fabric peer only | direct-link firewall isolation |
| TRELLIS.2, initial | GPU node 2 | 7860/TCP | loopback only through SSH tunnel | SSH plus application token where supported |
| TRELLIS.2 upstream, future | GPU node 2 | 7860/TCP | GPU node 2 LAN; firewall source gateway only | upstream token plus proxy isolation |
| Browser UI | external host | 3000/TCP | LAN; exact host firewall source list | UI login plus Caddy API key |

No client route exists on the fabric. Because the fabric is a dedicated point-to-point network, its peer-to-peer runtime port range is allowed only between the two recorded fabric IPs rather than exposed on the LAN.

## DeepSeek Model Definition qualification

### Historical, superseded staged-lane design

This staged-lane ladder is retained as historical qualification rationale. It
does not define the active `deepseek-agent-dual` configuration: the approved
Mia-first implementation uses commit
`b131b2a22164675890dd1465fd8862b5cfb6ff13` for the planned, 1M-capable
dual-GPU node candidate. It remains unaccepted until its exact runtime and
acceptance evidence are recorded.

The three experimental features—speculative decoding, padded NVFP4 KV, and million-token context—are not enabled simultaneously on first boot. They are introduced one at a time:

| Model Definition candidate | Context ceiling | `max_num_seqs` | KV cache | draft-model | Purpose |
| --- | ---: | ---: | --- | --- | --- |
| `deepseek-baseline` | 16,384 | 1 | FP8 | off | prove TP=2 weight load, encoding, API, and deterministic output |
| `deepseek-draft` | 16,384 | 1 | FP8 | MTP=5 | isolate speculative decoding and record acceptance |
| `deepseek-nvfp4` | 16,384 | 1 | `nvfp4_ds_mla` | MTP=5 | validate the padded Stage-C NVFP4 workaround, including an 8K prompt |
| `deepseek-agent-dual` | 200,000 | 6 | `nvfp4_ds_mla` | MTP=5 | normal short/mid-context concurrent agent traffic backing `agent-full-dual` |
| `deepseek-long-dual` | 1,048,576 | derived, maximum 2 | `nvfp4_ds_mla` | MTP=5 | controlled deep-context work backing a separately accepted `agent-long-dual` Cluster Profile |

Only accepted serving definitions are referenced by activatable Cluster
Profiles. Each dual definition reserves both GPU nodes, starts the worker first,
starts the head second, stops the head first, and exposes only the head. Both
serving definitions advertise the stable OpenAI model name `deepseek`; clients
select a Cluster Profile, never an internal qualification definition.

The `nvfp4_ds_mla` path is the upstream **padded Stage-C workaround** using the known-good 584-byte sparse-MLA envelope. It is not described as a true-layout NVFP4 kernel. The discarded true-layout experiment failed beyond roughly 411 real prompt tokens; the NVFP4 gate therefore uses at least an 8,192-token prompt and asserts correct sentinel output.

### Capacity and admission parameters

The 128 GB per-node memory figure is marketed unified memory, not wholly
available runtime memory. Inventory exposes 121.69 GiB per node, and the
initial admission budgets after an 8 GiB OS reserve are 110.27 GiB on GPU node 1
and 110.23 GiB on GPU node 2. TP=2 partitions roughly 155.44 GiB of SafeTensor
payload to about 77.72 GiB per rank, while the OS, CUDA graphs, JIT artifacts,
model metadata, and runtime workspaces consume additional memory. Raw
subtraction is not used to declare KV capacity.

The adopted upstream 0731 recipe defaults to `gpu_memory_utilization=0.80`, `max_num_batched_tokens=8192`, and `MTP_NUM_TOKENS=5`. One upstream run at utilization 0.835 reported a 2,493,464-token shared KV pool and 2.38 maximum full-context concurrency. The live boot log on this cluster is authoritative.

Let `P` be the `GPU KV cache size` reported by the pinned runtime at boot:

```text
live-token invariant: sum(active prompt and generated tokens) <= P
full-context slots:   Cfull = min(2, floor(P / 1,048,576))
agent worst case:     6 * 200,000 = 1,200,000 tokens
```

The `deepseek-long-dual` definition renders `max_num_seqs=Cfull`; startup fails
if `Cfull < 1`. It never advertises more than two full-context slots. The
`deepseek-agent-dual` definition fixes `max_num_seqs=6` and requires
`P >= 1,200,000`. Six simultaneous 1M requests are neither admitted nor
claimed. If two full-context slots do not fit according to the live pool, the
long definition runs with one slot rather than relying on preemption.

Context and concurrency acceptance tests are coupled to these lanes: six requests are tested only at or below 200,000 live tokens each, while 900K acceptance is tested at the derived `Cfull` limit. One request beyond the configured scheduler limit must queue or receive the documented overload response without killing either rank.

### Runtime pins and sampling

The approved planned candidate is MiaAI-Lab commit
`b131b2a22164675890dd1465fd8862b5cfb6ff13`, model revision
`9e165c30e2704aec5d9d593cce3eebd58bbef1cb`, and image
`ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8`.
Deployment does not use mutable branches, tags, or an unverified image.

The local configuration pins:

- source repository commit;
- model snapshot revision and per-file manifest;
- encoder checksum and installation path;
- container image digest;
- every locally applied patch checksum;
- distributed executor, context, batching, KV, speculative-decoding, CUDA-graph, and NCCL parameters;
- named client sampling presets including temperature, top-p, maximum output tokens, reasoning mode, and seed where supported.

The server uses `--generation-config vllm` and no inherited model-repository generation override. Validation requests carry explicit sampling parameters. The deterministic quality preset uses `temperature=0`, `top_p=1`, a fixed seed where supported, and a fixed output ceiling. The production default follows the model card with `temperature=1.0` and `top_p=1.0`; benchmark presets reproduce the pinned benchmark script exactly. UI and API clients must select a checked-in preset rather than silently invent defaults.

The prebuilt Anemll image is acceptable only after provenance and contents are inspected. If that review is unsatisfactory, the same pinned runtime is built locally from reviewed sources.

### `trellis2`

- Requires any conflicting dual-GPU node DeepSeek definition to be stopped first.
  Co-residency with the accepted single-GPU node DeepSeek definition on GPU node 1 is
  permitted only through an exact accepted Cluster Profile such as
  `creative-3d`.
- Runs in its own pinned container/environment on GPU node 2.
- Uses local checkpoints and output storage.
- Starts with 512-cubed generation for acceptance testing before higher resolutions.
- Initially binds to GPU node 2 loopback and is reached through an SSH tunnel. After gateway deployment it binds to GPU node 2's LAN address with a firewall rule allowing only the gateway and is advertised through Caddy.

### `maintenance`

- Stops all GPU model containers on both nodes.
- Leaves GPU node SSH and DGX Dashboard available; external Caddy continues returning its maintenance response.
- Is the required state before OS, firmware, driver, or fabric maintenance.

### Multi-runtime Model Definitions and Cluster Profiles

The required model catalog, runtime adapters, optimized-artifact policy,
placement classes, and model-specific acceptance gates are defined in the
[multi-runtime profile design](2026-08-02-multi-runtime-model-profiles-design.md).
A Model Definition owns runtime details; a Cluster Profile assigns the complete
accepted definition set to both nodes and exposes stable aliases. A profile is
not advertised until every definition and the exact combined placement pass
health, output-quality, capacity, and lifecycle gates.

## Cluster Profile controller

There is no NVIDIA-standard Cluster Profile switcher for DGX Spark. The
platform therefore uses thin, project-local `vonkctl` logic over ordinary
runtime commands and SSH. It is not a daemon and does not hide the underlying
commands.

Before the external control host arrives, the controller executes on the
developer machine and stores state under `.state/vonkctl`. After the external
host arrives, the same contract moves to a one-shot container with
`/var/lib/vonk-node-platform` as a persistent bind mount. State contains the
prior canonical profile, target canonical profile, phase, controller PID, host
identity, start timestamp, last error, and both GPU node boot IDs.

The developer-machine lock records PID, host identity, and timestamp; breaking
it is an explicit operation that refuses a live PID or a lock younger than the
configured threshold. The future container uses the control host kernel's
`flock` on the bind-mounted lock for mutual exclusion. If state shows an
interrupted transition, status reports `recovery-required`; recovery is
permitted only after it proves no controller process and no Model Definition
process is still active on either GPU node.

The controller provides:

- `profile list`
- `profile activate <profile> [--restore <profile>]`
- `profile status`
- `profile validate <profile>`
- `logs <model-definition>`
- `doctor`
- `break-stale-lock`

A profile switch performs this sequence:

1. Acquire `flock` and write transition metadata.
2. When Caddy exists, load its draining route over the private container network and confirm new inference receives HTTP 503. Initial SSH-tunnel operation has no shared gateway and therefore performs a documented hard cutover after the operator starts the transition.
3. When a gateway exposes an active-request metric, poll it for up to 300 seconds by default. The configured grace may be 30–1,800 seconds; expiry is logged.
4. Invoke restricted node commands to stop the head before the worker with a 120-second Compose stop grace.
5. Confirm all changed prior-definition processes exited on both nodes within 60 seconds.
6. Confirm the quantitative memory, swap, and disk gates for the target profile.
7. Validate image digests, model manifests, encoder checksum, offline mode, fabric connectivity, and rendered configuration.
8. Invoke restricted node commands to start target workers before the target head.
9. Wait up to 900 seconds for container and application health checks.
10. Run structural, deterministic output-quality, and profile-specific capacity smoke tests.
11. Load Caddy's active route over the private container network, verify upstream health, and publish the target in controller state.
12. Mark the transition successful and release the lock.

If startup or validation fails, the controller restores Caddy's maintenance route, stops the partial target deployment, preserves logs, and leaves the system in a known stopped state. It does not automatically restart the previous heavyweight workload.

Distributed and GPU-heavy profiles use `restart: "no"`. They never auto-start after a GPU node reboot because Compose cannot enforce cross-host worker-before-head order. Caddy may auto-start on the NAS, but it starts fail-closed and returns maintenance or upstream-unhealthy HTTP 503. A GPU node boot-ID change causes controller status to report `stopped-after-reboot`; an operator must run `doctor` and explicitly start a profile.

Compose uses explicit project names, health checks, `stop_grace_period: 120s`, and Docker JSON log rotation of `max-size: 50m` and `max-file: 5` per container. The external Caddy container alone may use `restart: unless-stopped`; controller runs use `restart: "no"`. Production overrides contain runtime settings without duplicating base definitions.

## Output-Quality Gate

Health and HTTP success are insufficient. Every DeepSeek lane runs fixed, versioned prompts locally against the restricted vLLM upstream and then through NAS-hosted Caddy. The gate includes:

- an exact deterministic sentinel response;
- an English prose response checked for an expected fact or phrase;
- a small code task executed or compared with its expected result;
- reasoning-content separation for `low`, `high`, and `max` modes;
- a tool-call fixture with asserted function name and JSON arguments;
- an 8K-or-longer prompt with a sentinel beyond token 411 for the padded NVFP4 lane;
- Unicode script ratios that fail unexpected CJK drift in an English-only fixture;
- a repetition detector that fails repeated character runs, repeated n-grams, or low-entropy loops;
- checks that assistant-visible output contains no leaked prompt, schema, or tool XML.

The validation fixture pins sampling parameters and records the runtime digest, source commit, model revision, encoder checksum, and output hash. Agent-harness validation runs separately with fallback models disabled so stale session replay or fallback behavior cannot be mistaken for a model-runtime defect.

## Security

- Install the `Vonk Forge GPU node Admin` public key on both GPU nodes, then verify fresh agent-backed sessions before changing SSH authentication.
- Run `sshd -t` before every SSH configuration reload.
- After key verification on both nodes, set `PasswordAuthentication no` and `KbdInteractiveAuthentication no` in a managed drop-in and reload SSH.
- Verify key login again, then verify a connection with public-key authentication disabled is rejected. Retain local console/DGX Dashboard recovery access.
- Do not copy the Mac's private key to either GPU node or use SSH agent forwarding.
- Keep the controller's dedicated private key only on the external control host; each GPU node restricts its public key with a forced `node-nodectl` command and source-address rule.
- Keep secrets out of Git, Compose files, logs, process arguments where avoidable, and command histories.
- Store API keys and future Tailscale or LiteLLM credentials in 1Password and render runtime-only secret files under `/run` with mode `0600`.
- Require a bearer API key at Caddy and a separate upstream key at vLLM.
- Rotate API keys under maintenance by temporarily accepting old and new proxy keys, updating and testing clients, then removing the old key and reloading Caddy. Record only key IDs and rotation dates.
- Bind application ports only to the addresses in the port map and enforce the corresponding host firewall rules.
- Run containers without root and with read-only mounts where the GPU/RDMA runtime permits; grant only required devices and capabilities.
- Verify signed images when publishers provide signatures and pin every image by digest.
- If LiteLLM is deployed, use a signed release at version `1.83.7` or later because GHSA-r75f-5x8p-qvmc affects earlier versions; configure a master key and keep its management UI off the public internet.

## Updates and Change Control

Both GPU nodes must end maintenance on matching supported DGX OS, driver, CUDA, firmware, and container-runtime versions. Initial updates use DGX Dashboard, which NVIDIA recommends over ad-hoc package upgrades.

Updates occur only in `maintenance`:

1. Back up configuration, export the inventory, and record current versions.
2. Review release notes, known issues, and firmware reversibility.
3. Update GPU node 2 first.
4. Reboot GPU node 2 and validate SSH, DGX Dashboard, GPU visibility, Docker GPU access, storage, and fabric-interface state without starting a distributed profile.
5. Stop if GPU node 2 fails; do not update GPU node 1.
6. Update and reboot GPU node 1.
7. Compare both nodes, then rerun fabric, RDMA, NCCL, container, profile-ladder, quality, and performance gates.

This sequencing provides a detection point before both nodes change; it is not a promise of rollback. Firmware is commonly non-reversible, so firmware changes require a documented vendor recovery path or explicit acceptance that recovery may be roll-forward only.

Model, runtime, image, encoder, and sampling changes use new pins and repeat the same acceptance tests. Floating automatic updates are disabled.

## Observability and Operations

`status` and `doctor` report:

- persisted profile, live profile, transition phase, boot ID, and recovery state;
- NAS reachability, Caddy route/upstream-health state, and last successful advertisement;
- container state and health on both nodes;
- LAN and fabric connectivity, MTU, link rate, and resolved NCCL variables;
- disk bytes free and model-manifest verification time/result;
- available memory, swap use, and `earlyoom` enabled/active state;
- GPU/SoC temperature, clocks, power, and thermal-throttling indicators exposed by Vonk Forge-supported tools;
- vLLM model identity, context ceiling, `max_num_seqs`, live KV-pool tokens, and active requests;
- last 100 log lines plus current bounded log sizes;
- last successful structural, output-quality, capacity, and performance test with pin set.

GPU node runtime metrics and logs remain on the GPU nodes initially and are queried by the external controller. Any later Prometheus or centralized logging containers run only on external hosts.

## Failure Handling

- A failed switch ends with Caddy in maintenance and both heavyweight profiles stopped.
- Loss of the worker makes DeepSeek unhealthy; Caddy must stop advertising it.
- Model or encoder verification failure prevents startup; download repair occurs only in an explicit online maintenance operation.
- Fabric or NCCL failure prevents DeepSeek startup but does not block maintenance or single-node TRELLIS.2 diagnosis.
- `earlyoom` is disabled before DeepSeek; vLLM scheduler limits enforce the configured lanes. The platform does not promise automatic OS-pressure draining because the controller is not a daemon.
- If memory or thermal thresholds regress during operation, the operator drains through Caddy, stops the profile, and retains logs; no automatic cache deletion occurs.
- Recovery removes profile containers and ephemeral configuration without deleting verified model caches, manifests, benchmark records, or user outputs.

## Performance Gates

Performance is tested after warm-up with the exact pinned upstream benchmark method. Initial minimums are 70% of the adopted upstream 0731 results:

| Test | Upstream reference | Initial pass floor |
| --- | ---: | ---: |
| 2K prompt, concurrency 1, prefill | 2,563 tok/s | 1,794 tok/s |
| 2K prompt, concurrency 1, decode | 68.8 tok/s | 48.1 tok/s |
| 2,048-token decode, concurrency 1 | 82.4 tok/s | 57.6 tok/s |
| 2,048-token decode, concurrency 3 aggregate | 134.6 tok/s | 94.2 tok/s |
| 2K prompt, concurrency 6 aggregate | 143.7 tok/s | 100.5 tok/s |

The source results came from specific upstream hardware state and profile settings, so they are comparison baselines rather than vendor guarantees. Falling below a floor fails acceptance until the variance is explained and recorded. A 15-minute sustained decode run must show no thermal-throttling flag and no more than 15% reduction between the first and final five-minute median throughput windows.

## Validation Sequence

1. Install the 1Password-managed public key on both nodes and verify fresh key-backed sessions.
2. Validate SSH configuration, disable password and keyboard-interactive login, then verify key login and password rejection.
3. Inventory hardware, storage, software, thermals, interfaces, NCCL variables, cable, and `earlyoom` on both nodes.
4. Enter maintenance; update GPU node 2, validate it, then update GPU node 1 and compare versions.
5. Disable `earlyoom` on both nodes and verify its state.
6. Configure the ConnectX-7 fabric with NVIDIA Sync Cluster Assistant.
7. Verify bidirectional fabric IP connectivity, matching MTU, link rate, raw RDMA, and NCCL bandwidth.
8. Validate Docker GPU access and image architecture on both nodes.
9. Audit and pin the MiaAI-Lab source, Anemll image digest, patches, encoder, configuration, and sampling presets.
10. Check disk gates; download the pinned snapshot online during maintenance, generate/verify manifests on both nodes, then enforce offline mode.
11. Run `deepseek-baseline` through a GPU node 1 SSH tunnel and pass structural plus deterministic quality gates.
12. Add draft-model, pass quality gates, and record speculative acceptance.
13. Add padded NVFP4, pass the greater-than-411-token regression and 8K quality gates.
14. Run `deepseek-agent`, verify `P >= 1,200,000`, six concurrent requests with at most 200,000 live tokens each, and overload behavior.
15. Run `deepseek-long`, derive `Cfull` from the boot log, complete a 900K sentinel request at the admitted limit, and verify one excess request queues or rejects safely.
16. Run reasoning, tool-call, streaming, restart, output-quality, and performance gates through the SSH tunnel.
17. Stop DeepSeek; verify memory recovery and the clean stopped state.
18. Install and validate TRELLIS.2 at 512-cubed resolution through a GPU node 2 SSH tunnel.
19. Switch repeatedly between DeepSeek and TRELLIS.2 with direct scripts and confirm deterministic recovery.
20. After the new external host arrives, install Caddy and the one-shot controller, install forced `node-nodectl` access, and validate TLS, bearer rejection, upstream failure, private admin networking, restricted node control, route switching, state locking, and log limits.
21. Reboot both GPU nodes while the external host remains available; confirm Caddy returns HTTP 503 and status says `stopped-after-reboot` until an explicit start.
22. Add the browser UI, then apply the LiteLLM gate and add Tailscale in separate acceptance steps.

## Acceptance Criteria

- The Mac reaches both GPU nodes using the 1Password agent with no Linux password, no unencrypted private-key material in `~/.ssh`, and no private-key file usable while the vault is locked.
- Password and keyboard-interactive SSH are disabled only after fresh key sessions pass on both nodes; negative password-auth tests then fail as expected.
- Both nodes have matching supported platform software and pass the numeric memory and disk gates.
- `earlyoom` is stopped and disabled on both nodes before DeepSeek starts.
- The direct fabric passes NVIDIA connectivity, RDMA, NCCL, MTU, and link-rate validation with recorded interface/HCA/GID consumers.
- The verified 0731 model and encoder manifests pass offline on both nodes.
- Each DeepSeek ladder stage passes before the next feature is enabled.
- `deepseek-agent` serves six requests of at most 200K live tokens each with `P >= 1,200,000`.
- `deepseek-long` reports the 1,048,576-token ceiling, derives one or two admitted slots from the live KV pool, and completes the 900K sentinel test without rank failure.
- Deterministic, script/language, repetition, XML-leakage, reasoning, streaming, and tool-call quality gates pass both directly and through Caddy.
- All performance floors pass, and the 15-minute run has no thermal throttling or greater than 15% sustained regression.
- NAS-hosted Caddy is the only client endpoint, enforces TLS and bearer keys, exposes no LAN admin API, and returns HTTP 503 during drains, upstream failures, and post-reboot state.
- The GPU nodes run only AI/model containers; gateway, controller, UI, LiteLLM, Tailscale ingress, and general monitoring containers run on non-GPU node hosts.
- Profile switches are serialized by kernel `flock`, use the numeric timeouts, and fail to a known stopped state.
- After reboot, no distributed/GPU-heavy profile starts automatically.
- TRELLIS.2 produces a valid GLB from a sample image at 512-cubed resolution.
- The browser UI displays only the profile advertised as healthy by the controller.
- No model service is publicly exposed; later remote access is restricted by Tailscale policy.
- Runtime configuration is reproducible from this private repository without committed secrets.

## References

- [NVIDIA DGX Spark user guide](https://docs.nvidia.com/dgx/dgx-spark/)
- [NVIDIA ConnectX-7 Cluster Assistant](https://docs.nvidia.com/sync/latest/cluster-assistant.html)
- [NVIDIA two-Spark networking guide](https://build.nvidia.com/spark/connect-two-sparks/stacked-sparks)
- [NVIDIA DGX Spark update guide](https://docs.nvidia.com/dgx/dgx-spark/os-and-component-update.html)
- [DeepSeek-V4-Flash-0731 model card](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731)
- [MiaAI-Lab dual-GPU node 0731 recipe](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark/tree/b131b2a22164675890dd1465fd8862b5cfb6ff13)
- [MiaAI-Lab 0731 measurements](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark/blob/b131b2a22164675890dd1465fd8862b5cfb6ff13/docs/DEEPSEEK_V4_FLASH_0731.md)
- [Microsoft TRELLIS.2](https://github.com/microsoft/TRELLIS.2)
- [Docker Compose production guidance](https://docs.docker.com/compose/how-tos/production/)
- [Caddy configuration API](https://caddyserver.com/docs/api)
- [Tailscale Services](https://tailscale.com/kb/1552/tailscale-services)
- [LiteLLM repository and signed releases](https://github.com/BerriAI/litellm)
- [LiteLLM GHSA-r75f-5x8p-qvmc](https://github.com/BerriAI/litellm/security/advisories/GHSA-r75f-5x8p-qvmc)
