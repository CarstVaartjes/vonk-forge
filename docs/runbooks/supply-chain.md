# Verify the platform and workload supply chains

Standard service images are fixed by version and OCI index digest in
`deploy/compose/images.lock.json`; Compose uses those exact references as its
defaults. The custom control image is a release artifact and must be supplied
through `CONTROL_API_IMAGE`, `CONTROL_WORKER_IMAGE`, and `HERMES_AGENT_IMAGE`
with one complete set of registry digests. The three `vonk-forge` packages are
`ghcr.io/carstvaartjes/vonk-forge-api`,
`ghcr.io/carstvaartjes/vonk-forge-worker`, and
`ghcr.io/carstvaartjes/vonk-forge-hermes`. Build the `api` and `worker`
Dockerfile targets from the same release commit; the worker target deliberately
contains neither Git nor OpenSSH. The Node and Python build bases are separately
digest-pinned in the lock.

## Future image releases

No images are currently being published. The repository variable
`VONK_CONTAINER_RELEASES_ENABLED` remains unset/default-off until the whole
repository is release-ready. Setting it to `true` is a deliberate maintainer
enablement action. Once enabled, only an exact stable SemVer version-tag push
(`vX.Y.Z`) can publish the three packages; branches, pull requests, malformed
tags, and Dependabot cannot publish.

For each package's initial publication, a maintainer must open its GitHub
package page and choose **Package settings** → **Danger Zone** → **Change
visibility** → **Set package visibility to Public**. Public NAS pulls then need
no GitHub token. A successful three-image publication creates the public
release assets `vonk-forge-images.env` and
`vonk-forge-images.env.sha256`; NAS operators verify the checksum and use all
three version-and-digest assignments as one release set. See the authoritative
[NAS pull-only Compose deployment guide](../../deploy/compose/README.md).

The workflow may update each package's `latest` tag after a successful stable
version release, but `latest` is informational only and never a production
image input. Production selection remains authoritative only through the
trusted host-updater and its TUF-reviewed release asset digests. Docker does
not update running containers merely because a tag moves.

Dependabot checks Docker build inputs, Docker Compose files, and GitHub Actions
weekly and opens ordinary reviewed pull requests. It does not auto-merge, tag,
create a release, or publish an image; maintainers review an accepted update
before making a later deliberate version-tag release.

Run the offline gate before building or deploying:

```bash
scripts/verify-supply-chain --json
```

The verifier checks image defaults, both dependency lockfiles, deterministic
SPDX 2.3 documents, the rebuilt `vonk-agent-protocol` wheel hash,
Dockerfile/Compose inputs, the LiteLLM cosign public key, and the
content-addressed evidence manifest. Normal verification performs no network
access. Regeneration is an explicit reviewed operation:

```bash
scripts/verify-supply-chain --write-manifest --json
```

## Local diagnostic builds

The following are **local diagnostic only** builds. They are not an image
publication procedure: do not log in to a registry, tag a GHCR name, or push.
Exercise all three release targets together from the tagged source candidate:

```bash
docker buildx build --platform linux/amd64 --load \
  --file control/Dockerfile --target api --tag vonk-forge-api:release-dry-run .
docker buildx build --platform linux/amd64 --load \
  --file control/Dockerfile --target worker --tag vonk-forge-worker:release-dry-run .
docker buildx build --platform linux/amd64 --load \
  --file deploy/compose/hermes-agent/Dockerfile --target managed \
  --tag vonk-forge-hermes:release-dry-run deploy/compose/hermes-agent
```

After future deliberate enablement, the tag-triggered GitHub Actions workflow
is the only publication procedure. It builds all three targets, verifies their
inputs, emits SBOM and provenance, resolves all three digests, and creates the
checksum-protected three-reference release asset. It never treats one or two
images as a releasable publication. LiteLLM signatures use the checked-in key
copied from immutable upstream commit
`0112e53046018d726492c814b3644b7d376029d0`; verify the locked digest, never a
mutable tag. Store scan/signature attestations with the release evidence.

## Workload artifact build and promotion boundary

Generic package artifacts have an independent release cadence from
`vonk-forge`. A generic component that fits the installed node-package ABI does
not require a platform release. Model recipes instead use the exact
Library revision and execution-harness gates documented in the
[recipe operations runbook](model-switching.md). The authorities are
deliberately separate:

1. A reviewed Git change supplies a bounded generic-package build request. The request
   names an exact 40-character source commit, a content digest, reviewed source
   paths, a digest-pinned base image, target architecture, and output repository.
2. `.github/workflows/workload-artifacts.yml` is a build-only publisher. After
   the read-only CI gate, its job-scoped package token may push the resulting OCI
   artifact by digest. It selects and verifies the single executable manifest
   for the requested platform. It rejects ambiguous JSON and malformed index or
   descriptor metadata, and requires exactly one canonical BuildKit attestation
   manifest whose reference annotation binds it to that executable child. It
   then attaches signed SBOM and provenance evidence to the stable runtime
   digest. The run-specific outer BuildKit index remains evidence and is never
   the runtime identity. The token is not a BuildKit input. The job has no
   platform or workload TUF key and cannot change NAS desired state.
3. The NAS promotion service independently verifies the request digest, source
   identity, OCI manifest digest, SBOM, provenance, family policy, and validation
   evidence. A successful build is only a promotion candidate; it is not an
   authorized generic-package release.
4. Workload TUF authorizes the exact immutable generic-package release lock after
   promotion. Its roots, roles, target prefixes, and signing credentials are
   separate from platform TUF, so a workload key cannot update `vonk-forge`, its
   agents, supervisors, protocol, or node policy.
5. GPU nodes obtain authorized lock metadata from the NAS and fetch large
   content-addressed payloads from their declared upstream or approved mirror.
   SSH is not part of this standard path.

Build requests must not contain secrets, registry credentials, free-form build
arguments, shell commands, host paths, parent-directory traversal, mutable Git
references, or floating OCI tags. Publication from an ordinary branch or pull
request is forbidden. Promotion and rollback remain NAS-admin actions recorded
through the workload release plane; neither action mutates the platform release
manifest.

Store each reviewed request as `release/workloads/<request-id>.json`. Its
`context_digest` is the digest of the exact Git archive consumed by the builder:

```bash
workload_source_commit=$(git rev-parse HEAD)
workload_context=packages/<family>/<component>
git archive --format=tar "$workload_source_commit" -- "$workload_context" \
  | sha256sum
scripts/workload-artifact-metadata request \
  release/workloads/<request-id>.json
```

The validator prints the canonical request and its `build_request_digest`.
Submit that request through review before manually dispatching the workload
artifact workflow from the merged `main` revision. Tag-push publication is
intentionally disabled because a tag can otherwise select its own unreviewed
workflow definition. A workflow artifact result can be checked locally with:

```bash
scripts/workload-artifact-metadata result result.json \
  --request release/workloads/<request-id>.json
```

That validation proves metadata binding only. W13 promotion remains responsible
for verifying the registry subject, signed provenance, SBOM, family policy, and
qualification evidence before workload TUF authorization.

Before promotion, dispatch an unchanged reviewed request twice from accepted
`main`, validate both workflow artifacts, and require their executable
`oci_manifest_digest` values to match. BuildKit index digests, invocation
provenance, SBOM namespaces, and signed-bundle digests are run-specific and may
differ. An executable-manifest mismatch rejects both candidates.
