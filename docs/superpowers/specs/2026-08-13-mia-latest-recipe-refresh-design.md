# Latest Mia Two-Spark Recipe Design

**Date:** 2026-08-13

**Status:** Approved by the operator's instruction to use the latest official
Mia recipe from GitHub

## Outcome

Add a source-first Vonk recipe for the official text-only
`MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark` lane, currently reviewed at
commit `f752cd04ab30f2cf42077dd8811a5e1e682d63e7`. It runs one real tensor-parallel
vLLM service across exactly two DGX Sparks. It does not reuse the existing
two-replica DS4 smoke profile and does not rewrite the historically accepted
legacy Mia adapter.

“Latest” is resolved once during review. The recipe remains immutable after
admission: source commit, model revision, source bundle, base image, and every
hotfix is digest-bound.

## Exact upstream inputs

- Git source: `f752cd04ab30f2cf42077dd8811a5e1e682d63e7`.
- Model: `deepseek-ai/DeepSeek-V4-Flash-0731` revision
  `9e165c30e2704aec5d9d593cce3eebd58bbef1cb`.
- Model snapshot size: `166898660330` bytes from the immutable Hugging Face
  revision metadata.
- Runtime:
  `ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8`.
- Topology: two nodes, tensor parallel 2, pipeline 1, data 1, multiprocessing,
  with rank 1 using vLLM's headless worker mode.
- Serving defaults: NVFP4 DS-MLA KV cache, 1,048,576-token ceiling, six
  sequences, 8,192 batched tokens, 1,024 long-prefill threshold, MTP 5,
  utilization 0.835, and default reasoning effort `max`.

The optional abliterated model, vision sidecar, MCP installer, benchmarks, and
Stage-C plugin are separate workloads and are excluded.

## Runtime capability

The official distributed runtime requires host networking, host IPC, unlimited
memlock, the InfiniBand device tree, and the NVIDIA GPU. Vonk currently rejects
the first four. The recipe lane therefore gains one narrow direct-fabric mode,
represented by the existing `runtime.security.host_network=true` field.

The mode is accepted only when every profile is multi-node and declares
connected fabric. The Rust agent emits a fixed Docker shape:

- `--network host` and `--ipc host`;
- `--device /dev/infiniband:/dev/infiniband`;
- `--ulimit memlock=-1:-1` and `--ulimit stack=67108864:67108864`;
- no Docker port publication;
- the existing read-only root filesystem, dropped capabilities,
  no-new-privileges, numeric non-root user, memory/PID bounds, exact local
  image digest, model/state mounts, and controller-signed helper grant.

The privileged helper accepts host mode only when that complete fixed shape is
present and rejects partial or additional host capabilities. Rank placement
must include exact local/master fabric addresses. The runtime discovers the
interface, HCA, and RoCEv2 GID index matching its controller-selected local
address; no lab interface name is baked into the recipe.

Host mode makes the endpoint use its declared host port directly. The
persistent host firewall must permit TCP 8888 only from loopback and the NAS
management address on both nodes, and reject every other interface/source.
Rank 1 remains headless and therefore has no native API listener. Host-network
traffic traverses the host `INPUT` path, not Docker's `DOCKER-USER` chain, so
acceptance verifies both rule sets. Because the generic agent requires local
readiness from every rank, rank 1 pairs the native headless process with a
minimal readiness proxy that exposes only `/v1/models` and forwards that probe
to rank 0 over the selected fabric. The proxy exits with the headless process.
Rank 0's endpoint becomes ready only after the complete tensor-parallel world
has joined; external inference and the published route still target rank 0
only.

## Immutable runtime image

All upstream hotfixes and the exact model encoder are applied during the
networkless source build. Runtime mutation of Python packages would conflict
with Vonk's read-only root filesystem, so the container never patches itself on
startup. The source context contains the selected upstream files, Apache-2.0 license,
and provenance inventory. Its Dockerfile:

1. starts from the exact runtime digest;
2. copies only reviewed encoder/hotfix/launcher inputs;
3. applies the hotfixes in upstream order without package installation or
   network access;
4. removes no security controls and selects `10001:10001`;
5. labels the result `ai.vonkforge.runtime-interface=v1`.

The launcher finds exactly one immutable model snapshot beneath
`/models/sha256`, keeps provider access offline, derives RoCE identity from
`VONK_LOCAL_ADDR`, uses rank 1 headless mode, and binds rank 0 to the
controller-declared endpoint.

## Trust and secrets

No image or source bundle contains credentials, model-provider tokens, NAS
secrets, signing keys, or model weights. The public model requires no token.
Model files remain separate per-node content-addressed artifacts. Images are
built and distributed through the authenticated recipe operation path; release
publication remains GitHub-Actions-only.

## Acceptance

Automated acceptance covers schema semantics, exact Docker helper arguments,
rejection of partial host privilege, source provenance, model/image pins,
hotfix order, offline execution, role behavior, and source-bundle identity.

Physical acceptance occurs only after a merged agent package is installed on
both Sparks. It must verify exact model downloads, headless worker rendezvous,
`/v1/models`, chat inference, route publication, rank-failure withdrawal,
recovery, stop, and uninstall. Existing historical evidence is not reused.

## Provenance

- [Official repository](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark)
- [Selected commit](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark/tree/f752cd04ab30f2cf42077dd8811a5e1e682d63e7)
- [Selected Compose recipe](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark/blob/f752cd04ab30f2cf42077dd8811a5e1e682d63e7/docker-compose.dspark.yml)
