# Development model smoke qualification

Date: 2026-08-12

Status: generic runtime publication and automated gates complete; physical
qualification is deliberately pending the accepted-main wrapper deployment.
No result in this document claims that the new agent path has run the model.

## Selected lane

The development smoke uses the existing audited DS4 Q2-imatrix lane rather
than creating another model definition. It is the smallest repository-backed
DeepSeek lane that fits one DGX Spark, and therefore can exercise both the
single-node and two-node control paths with identical runtime and artifact
identities. The two-node profile is a replicated two-rank gang with one routed
entrypoint. A versioned TCP HELLO/acknowledgement over the declared master
fabric endpoint gates both replicas before launch and is repeated when rank 1
recovers; DS4 inference itself remains independent on each node and is not
described as tensor- or pipeline-parallel inference.

Accepted workloads use Spark's Docker/NVIDIA runtime on an isolated bridge;
rootless Podman remains build-only. Rank 1 connects to the exact
controller-supplied master destination through host routing/SNAT and does not
attempt to bind the host fabric IP inside the container namespace. The
controller-supplied local fabric address remains in the bounded HELLO and
evidence. Address-specific rank-0 publication, host routing, and
direct-fabric-only firewall evidence prove the physical path.

The accepted Mia lane was not selected for this smoke because its 166 GB BF16
checkpoint cannot execute on one 128 GB unified-memory Spark. Mia remains the
accepted production-performance profile. DS4 remains unsuitable for production
performance advertising: its earlier performance, thermal, and lifecycle
deferrals remain recorded in
[`deepseek-ds4-operational.json`](../../inventory/reports/deepseek-ds4-operational.json).

## Immutable identities

| Boundary | Exact identity |
| --- | --- |
| DS4 source | `https://github.com/Entrpi/ds4` at `4ad370b4a338efe9723a386673c0e04f6e214108` |
| Source archive | SHA-256 `7db338d0a441fed36c5e4e7af44ff670e8bfe567e88d482f00ff6a3dc0e5dbe3` |
| Runtime image | `ghcr.io/carstvaartjes/vonk-forge-workloads@sha256:96993dcbb8f262c6fbcc41fd005498934b476b040486a6618898d4135b6d0817` |
| BusyBox transport | Included in the accepted generic runtime from the Public multi-architecture OCI index `docker.io/library/busybox:1.37.0-musl@sha256:fc6dddc4c44b1bfe37f41cae8e67d1693828e8f42a91862816d7953e2c9d3f23`; `linux/arm64/v8` child `sha256:97d3fa0415c6749d4b27849c2bf251ac11fe2ec7d3178a2dae4bbf3bd30056fc` |
| Base GGUF | 86,720,111,488 bytes; SHA-256 `ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0` |
| Drafter GGUF | 6,971,241,504 bytes; SHA-256 `8fa269560dc76fd73e4233ad9b1938b5f65dd363381fd9b1a5c6183f7d12d686` |
| Model bytes per node | 93,691,352,992 |
| Source bundle | 10,240 bytes; canonical manifest SHA-256 `629ed1747ce076754e8acfbca6fd279d263529d09055c517e17aebbcc5fd0e9e` |
| Recipe content | Canonical JSON SHA-256 `1e2ca9b77732d561cecafec6492bd57900dbf2f9d8c16a500b0ea47606c4f247` |

The checked source, artifact, topology, and recipe documents live under
[`config/recipes/development`](../../config/recipes/development). Both profiles
use the same two `http.file` identities. Each URL contains an immutable
Hugging Face commit, and the agent independently enforces the final file size
and `sha256:` revision before installation.

Independent accepted-main publisher runs
[`31610534569`](https://github.com/CarstVaartjes/vonk-forge/actions/runs/31610534569)
and
[`31610540920`](https://github.com/CarstVaartjes/vonk-forge/actions/runs/31610540920)
produced the same executable ARM64 runtime manifest above. Their outer indexes
and signed bundles remained run-specific. Independent verification required
the numeric `10001:10001` user, runtime-interface label, reviewed DS4 source,
no model weights or secret build inputs, valid signed provenance and SBOM, and
an anonymous pull. The Spark-side wrapper now has one digest-pinned `FROM` and
copies only the local model and rendezvous wrappers; BusyBox is already present
at `/opt/vonk/busybox` in the accepted runtime.

DS4 source is MIT licensed. The qualifier also requires explicit acknowledgement
of the upstream DeepSeek/model terms. A credential is not embedded in the
recipe, image, qualification output, Compose file, or repository; if a provider
credential becomes necessary, it is supplied only through the agent's
root-owned local curl configuration described by the runbook.

## Read-only node evidence

The inventory used no `sudo`, installed nothing, and did not start or stop a
service or container.

| Gate | `spark-3542` | `spark-2297` |
| --- | --- | --- |
| Platform | aarch64, Ubuntu 24.04.4 | aarch64, Ubuntu 24.04.4 |
| GPU | NVIDIA GB10, driver 580.173.02, compute 12.1 | NVIDIA GB10, driver 580.173.02, compute 12.1 |
| `sm_121` | Host CUDA 13.0 `nvcc` lists `sm_121` | Hardware compute 12.1 and pinned DS4 configuration; final image inspection still required |
| Build isolation | Rootless Podman 4.9.3 and Buildah 1.33.7 | Rootless Podman 4.9.3 and Buildah 1.33.7 |
| Accepted runtime | Docker 29.2.1 with NVIDIA Toolkit 1.19.1 and CDI | Docker 29.2.1 with NVIDIA Toolkit 1.19.1 and CDI |
| Available memory | approximately 117 GiB | approximately 117–118 GiB |
| Available disk | approximately 3.2 TiB | approximately 3.3 TiB |
| Management | `192.168.1.211/24` | `192.168.1.212/24` |
| Direct fabric | `192.168.100.10/24` and `192.168.101.10/24` | `192.168.100.11/24` and `192.168.101.11/24` |
| Fabric link | two active 200 Gb/s ConnectX-7 RoCE links | two active 200 Gb/s ConnectX-7 RoCE links |
| Rootless model cache | empty | empty |

The canonical read-only probe printed hostname, architecture, OS, GPU identity,
build/runtime separation, byte-valued memory/disk, IPv4 interfaces, CUDA code list when
available, and RDMA links. SHA-256 of that bounded output was:

- `dgx-spark-1`: `b32d5fd214caf402122fc11ec614ef479b85f391cd80d39ddc9040a1e5e2db6f`
- `dgx-spark-2`: `5a794608c8da9bbde78d9282abe0b64f5bf3164abd96bcc83c307961c2629038`

Spark 2 still has the previous Mia snapshot in a Docker-managed host location;
it is not treated as evidence that the new agent cache is populated.
Spark 1 is clean. This distinction prevents qualification from silently relying
on manually installed legacy state.

The measured DS4 start reduced host `MemAvailable` by 119,038,607,360 bytes and
left 7,968,821,248 bytes available. The checked recipe therefore rounds the
whole-host measured peak up to 120,000,000,000 bytes and sets its additional
`system_reserve_bytes` to zero: the measurement already includes host impact.
The controller independently retains its 4,000,000,000-byte global floor, so
qualification and admission both require exactly 124,000,000,000 bytes before
start. Adding another recipe reserve would double-count the host allowance and
reject both healthy 128 GB unified-memory Sparks.

## Qualification behavior

Run `scripts/qualify-development-model` with a fresh JSON evidence document.
The evidence contains identities and capacity facts only, never credentials or
model contents. The qualifier refuses:

- a non-aarch64 node, wrong OS/GPU/compute capability, or missing `sm_121`;
- a mutable source, image, or model revision;
- an image without an arm64 manifest, the runtime-interface label, numeric
  non-root user, or anonymous public pull;
- a model size or SHA mismatch on either node;
- insufficient memory or disk;
- missing rootless Podman build isolation, Spark Docker/NVIDIA CDI runtime,
  license acknowledgement, active 200 Gb/s fabric, or
  distinct management/fabric subnets;
- duplicate node identities, inconsistent image identities, or inconsistent
  artifact sets.

Success writes one mode-0600 canonical JSON document bound to the source,
artifact set, topology, and evidence SHA-256 values. The runner then requires
the control plane's fenced start receipts to report every exact mapped node;
the control service already validates each receipt's image digest, artifact-set
digest, model identity, rank, world size, endpoint, and memory reservation.

## Current publication gate

The generic `vonk-forge-workloads` package is anonymously readable. Its
accepted executable manifest is the matching child identity from both
independent publications, not either run-specific outer BuildKit index. The
checked source document, sole wrapper `FROM`, and private qualification output
must all retain that exact reference. Any anonymous-pull regression, manifest
drift, mutable reference, or identity mismatch blocks the physical slice; it
is never worked around by placing a GHCR token on the NAS, a Spark, or in an
image.

## Physical acceptance still required

After accepted-main images and packages are published, the physical run must:

1. qualify a fresh two-node evidence document;
2. install and retain Docker-aware `DOCKER-USER` policy that allows the NAS to
   the routed management endpoint, confines rendezvous to the direct peer, and
   rejects the worker endpoint on its management address;
3. execute the one-node profile through inference, restart persistence, stop,
   route withdrawal, and uninstall;
4. execute the replicated two-rank profile, prove the pre-launch TCP fabric
   exchange and both exact start receipts before publication;
5. verify the management labels and stop the designated worker rank's exact
   Docker-managed container while
   its Rust agent remains healthy, observe the next authenticated run snapshot
   mark only that rank failed and withdraw the route, start the same container,
   then observe fresh health and automatic republication;
6. restart both agents and the NAS stack, prove fresh identities and inference
   without rebuilding, then stop and uninstall normally.

The acceptance evidence remains local and redacted. It is not committed merely
because configuration/unit tests pass.

## Development project clean-room audit

On 2026-08-11 a disposable private local directory was used to render the
mutable `:dev` Compose graph, generate a fresh 17-file local source generation,
publish the exact two-item project with its 14-file runtime-secret projection,
run `docker compose config --quiet`, and run the publication recovery fixture
suite. The project contained one mode `0600` `docker-compose.yml`, one mode
`0700` `secrets/` directory, and exactly 14 mode `0600` regular secret files.
The publication fixture must pass in the accepted workflow.

Only public audit values were retained:

- rendered Compose SHA-256:
  `b5974d97b529bb246fad3b87316c5384c0342d991243c24ffd63274bf07eeeba`;
- disposable agent CA SHA-256:
  `c4c77231d1694b8064147a03d09d0f5d95a5d803ce4866b02ad83dff4f15f2da`;
- disposable controller CA SHA-256:
  `4a483e2dd1aba9ac874e22d9930a24cad191d4fa9b09fb0d2ff4ebdd5aeaaf3b`.

The disposable directory, including all private keys and tokens, was moved to
the workstation trash after validation. These generated fingerprints are
clean-room evidence only and are not deployment trust values.
