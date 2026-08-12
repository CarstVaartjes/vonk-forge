# Generic Runtime and Spark Wrapper Build Design

**Date:** 2026-08-12
**Status:** Accepted under the operator's delegated best-judgement approval

## Problem

The development model acceptance uses a networkless source recipe that copies a
static BusyBox binary from a second OCI stage into the pinned DS4 runtime. On
Ubuntu 24.04 ARM64, the native Podman 4.9/Buildah stack fails that stage copy
when the operation-private graphroot uses `overlay.force_mask=shared`. The
storage driver records original modes in `user.containers.override_stat`, then
the multi-stage copier attempts to reproduce that internal xattr through
FUSE-overlayfs and receives `EPERM`.

The force mask is not optional in the current builder. It lets the unprivileged
agent account for every byte below the private graphroot while FUSE presents
the image's original modes inside build containers. Removing it would weaken a
tested storage boundary. Replacing native Podman with a separately managed
BuildKit service would be a platform change disproportionate to a small runtime
wrapper.

## Decision

Reusable runtime composition belongs in the existing GitHub workload-artifact
publisher. Spark-side source builds remain small, single-stage, digest-pinned,
and networkless.

The accepted DS4 binary image remains byte-identical. A separate reviewed
Dockerfile composes that exact digest with the exact public BusyBox digest and
copies only BusyBox to `/opt/vonk/busybox` in the final stage, matching the
existing rendezvous wrapper contract. It performs no `RUN`, package
installation, source download, or compilation. A reviewed
`release/workloads/*.json` request will publish that derived image to the generic
`ghcr.io/carstvaartjes/vonk-forge-workloads` repository using the existing
workflow. The workflow already requires an accepted `main` source commit,
digest-pinned base images, exact source-context identity, a read-only CI gate,
SBOM and provenance attestations, and digest-only publication.

After publication, the development model recipe will use one `FROM` instruction
that pins the resulting `ghcr.io/carstvaartjes/vonk-forge-workloads` OCI digest,
followed only by the local
`COPY --chmod=0755 model-smoke fabric-rendezvous /opt/vonk/` instruction.

The model weights remain separate content-addressed artifacts on node-local
NVMe. No image contains NAS credentials, model-provider credentials, runtime
tokens, private signing material, or model weights.

## Extensibility Contract

A new workload type does not require one image per model and does not require a
Vonk release when it fits existing capabilities. Its author may:

1. pin a suitable public upstream runtime image; or
2. submit a reviewed runtime build request through the generic artifact
   publisher when Spark-specific compilation, patches, or utilities are needed;
3. declare model and adapter artifacts, checksums, resources, topology, command,
   health endpoint, and routing in workload-package data; and
4. add a small networkless wrapper source bundle when required.

One qualified vLLM, DS4, TensorRT-LLM, or other runtime release may serve many
compatible models and deployment profiles. Only a genuinely new privileged
device, mount, network, or host-helper capability requires a platform/agent
release.

## Publication Sequence

The legacy DS4 Dockerfile and runtime manifest remain byte-for-byte unchanged
until the replacement is qualified. The request binds an earlier accepted
source commit, so publication is intentionally two-stage:

1. merge and pass CI for the runtime Dockerfile and tests;
2. calculate that commit's Git-archive context digest;
3. review and merge a request that binds the exact source commit and context;
4. dispatch `workload-artifacts.yml` from current `main`;
5. verify the OCI digest, GitHub attestations, SBOM, provenance, architecture,
   numeric non-root user, required label, and anonymous pull;
6. update the recipe, qualification locks, audit records, and documentation to
   the accepted digest; and
7. rerun single- and two-Spark physical acceptance.

The new GHCR package must be public before a GPU node may pull it. A registry
credential on the NAS, a Spark, in Compose, or in a workload image is forbidden.

## Failure Handling

- A source/context mismatch, stale source commit, mutable base, failed CI gate,
  missing attestation, or unexpected image metadata stops publication.
- The current accepted DS4 digest remains unchanged until the replacement is
  published and qualified.
- A failed Spark wrapper build retains structured operation evidence but does
  not start a workload or download model weights.
- Rollback pins the previous accepted runtime digest and resets the affected
  development acceptance record; it never moves an immutable registry digest.

## Verification

Automated tests must prove the derived DS4 stage copies the static transport
utility from the pinned BusyBox stage without networked build steps, the legacy
release remains immutable, the development wrapper is single-stage, all runtime references are
digest-pinned and internally consistent, the generic build request validates,
and the workflow retains its source/CI/attestation boundaries.

Physical acceptance must prove anonymous ARM64 image retrieval, rootless
single-stage build on native Podman 4.9, Docker/NVIDIA runtime import, exact
model downloads, single-node inference and restart persistence, two-node
fabric rendezvous, rank failure/route withdrawal/recovery, and complete stop,
route withdrawal, and uninstall cleanup.
