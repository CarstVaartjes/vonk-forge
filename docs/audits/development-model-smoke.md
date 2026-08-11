# Development model smoke qualification

Date: 2026-08-11

Status: configuration and automated gates complete; physical qualification is
deliberately pending the accepted-main deployment and an anonymous GHCR pull.
No result in this document claims that the new agent path has run the model.

## Selected lane

The development smoke uses the existing audited DS4 Q2-imatrix lane rather
than creating another model definition. It is the smallest repository-backed
DeepSeek lane that fits one DGX Spark, and therefore can exercise both the
single-node and two-node control paths with identical runtime and artifact
identities. The two-node profile is a replicated two-rank gang with one routed
entrypoint; it is not described as tensor-parallel inference.

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
| Runtime image | `ghcr.io/carstvaartjes/spark-ds4@sha256:084d9a9ffa47431842c5dec84de97b058034dec0535b2a563bc5db78c9e14615` |
| Base GGUF | 86,720,111,488 bytes; SHA-256 `ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0` |
| Drafter GGUF | 6,971,241,504 bytes; SHA-256 `8fa269560dc76fd73e4233ad9b1938b5f65dd363381fd9b1a5c6183f7d12d686` |
| Model bytes per node | 93,691,352,992 |
| Source bundle | 10,240 bytes; canonical manifest SHA-256 `adda3c053bdfa47e8068ae776908e5feae3563554ce1e3e626e61bfeb20ff030` |

The checked source, artifact, topology, and recipe documents live under
[`config/recipes/development`](../../config/recipes/development). Both profiles
use the same two `http.file` identities. Each URL contains an immutable
Hugging Face commit, and the agent independently enforces the final file size
and `sha256:` revision before installation.

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
| Rootless runtime | Podman 4.9.3 and Buildah 1.33.7 | Podman 4.9.3 and Buildah 1.33.7 |
| Available memory | approximately 117 GiB | approximately 117–118 GiB |
| Available disk | approximately 3.2 TiB | approximately 3.3 TiB |
| Management | `192.168.1.211/24` | `192.168.1.212/24` |
| Direct fabric | `192.168.100.10/24` and `192.168.101.10/24` | `192.168.100.11/24` and `192.168.101.11/24` |
| Fabric link | two active 200 Gb/s ConnectX-7 RoCE links | two active 200 Gb/s ConnectX-7 RoCE links |
| Rootless model cache | empty | empty |

The canonical read-only probe printed hostname, architecture, OS, GPU identity,
rootless runtime, byte-valued memory/disk, IPv4 interfaces, CUDA code list when
available, and RDMA links. SHA-256 of that bounded output was:

- `dgx-spark-1`: `b32d5fd214caf402122fc11ec614ef479b85f391cd80d39ddc9040a1e5e2db6f`
- `dgx-spark-2`: `5a794608c8da9bbde78d9282abe0b64f5bf3164abd96bcc83c307961c2629038`

Spark 2 still has the previous Mia snapshot in a Docker-managed host location;
it is not treated as evidence that the new rootless agent cache is populated.
Spark 1 is clean. This distinction prevents qualification from silently relying
on manually installed legacy state.

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
- missing rootless Podman, license acknowledgement, active 200 Gb/s fabric, or
  distinct management/fabric subnets;
- duplicate node identities, inconsistent image identities, or inconsistent
  artifact sets.

Success writes one mode-0600 canonical JSON document bound to the source,
artifact set, topology, and evidence SHA-256 values. The runner then requires
the control plane's fenced start receipts to report every exact mapped node;
the control service already validates each receipt's image digest, artifact-set
digest, model identity, rank, world size, endpoint, and memory reservation.

## Current publication gate

An anonymous manifest request for the pinned DS4 image returned HTTP 401 on
2026-08-11. GitHub reports the personal `spark-ds4` container package as
private. The attempted API visibility update returned HTTP 404 and made no
change. Consequently a truthful physical evidence document must currently set
`public_pull` to false, and the qualifier correctly refuses it as
`image.public_pull`.

Before the physical slice, an owner must change that package's visibility to
public in GitHub Packages. The runbook requires repeating the anonymous raw
manifest inspection, recording its digest/platform, and only then setting
`public_pull` to true. This is a deployment prerequisite, not a reason to put a
GHCR token in a container or on a Spark.

## Physical acceptance still required

After accepted-main images and packages are published, the physical run must:

1. qualify a fresh two-node evidence document;
2. execute the one-node profile through inference, restart persistence, stop,
   route withdrawal, and uninstall;
3. execute the replicated two-rank profile and prove both exact start receipts
   before publication;
4. stop the designated worker agent, observe automatic route withdrawal after
   the 30-second rank-presence window, restore it, and observe republication;
5. restart both agents and the NAS stack, prove fresh identities and inference
   without rebuilding, then stop and uninstall normally.

The acceptance evidence remains local and redacted. It is not committed merely
because configuration/unit tests pass.
