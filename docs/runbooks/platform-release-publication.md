# Publish a platform release

Platform publication is a protected CI operation. It publishes `vonk-forge`
control services, host deployment assets, GPU node agents, and their signed
platform metadata. Workload packages have an independent publication system;
adding or updating a model stack must not require this workflow.

One immutable `vX.Y.Z` tag is the release unit. The same tagged commit builds
the ARM64 `vonk-forge-agent` Debian package and the API, worker, and Hermes
control-plane images. CI binds their digests, SBOM/provenance hashes, the
package signature hash, and deployment bundle into one platform manifest and
attaches the exact package and image evidence to the same GitHub Release.
There is no second tag or independently published agent release.

## Release input

A stable `vX.Y.Z` tag must point at a reviewed commit containing canonical JSON
at `release/platform/X.Y.Z.input.json`. The document contains every v2 platform
release field except `deployment_bundle`; it also contains a version-matching
placeholder in `agent_packages` for each published Debian architecture. The
workflow derives that descriptor from the canonical bundle bytes. A release PR must update exact predecessor,
host-updater ABI, database, protocol, image, agent, SBOM, and provenance
bindings. Do not hand-write an OCI digest or reuse a manifest from another
checkout.

For the first real release only, `rollback.predecessors` is an empty array and
installation is valid only on a host with no active generation. Every later
release lists the complete exact predecessor descriptor retained for recovery.
An empty list on an already-installed host cannot bypass this check: planning
rejects a target that does not authorize the active generation.

Both repository variables are default-off and must be `true`:

- `VONK_CONTAINER_RELEASES_ENABLED`
- `VONK_PLATFORM_RELEASES_ENABLED`

## Agent package channel boundary

The reusable native ARM64 package build consumes only canonical metadata from
the trusted tag and the protected `agent-release` environment. It returns the
exact package filename and version to this workflow. The GitHub Release job
rejects an unexpected downloaded-artifact member and attaches the accepted
`.deb`, checksum, CycloneDX/SPDX SBOMs, SLSA provenance, Sigstore bundle, and
systemd exposure report. GitHub-hosted package attestations are verified
separately with `gh attestation verify`.

Only after `gh release create` succeeds may the protected `apt-release`
environment publish that same accepted artifact to apt `stable`. The reusable
apt publisher has `contents: read`, receives no GitHub Release write authority,
and owns only the production apt key and stable private-state bucket. The
development path uses `agent-development` and `apt-development` from exact
accepted `main`; it cannot create Releases or publish stable state.

Configure and operate those four environments using the
[agent package channel guide](../operations/agent-package-release.md). In
particular, do not place apt keys, R2 credentials, agent keys, or NAS runtime
secret files in the `platform-release` environment or release artifacts.

The image/bundle build job has `contents: read` and `packages: write`, but no
OIDC permission. A separate `publish-platform-target` job uses the protected
GitHub environment `platform-release`, downloads the immutable build evidence,
and receives only `contents: read` plus `id-token: write`. Configure only these
environment variables for that delegated-authority job:

- `VONK_PLATFORM_AUTHORITY_URL`: HTTPS base URL of the online delegated
  publication service;
- `VONK_PLATFORM_AUTHORITY_AUDIENCE`: exact OIDC audience accepted by that
  service.

GitHub supplies `ACTIONS_ID_TOKEN_REQUEST_URL` and
`ACTIONS_ID_TOKEN_REQUEST_TOKEN` to the job. Do not create a long-lived GitHub
token, TUF root-key secret, or private-key environment variable. The TUF root
private key remains offline. The online service holds only the narrowly
delegated targets/channel authority permitted by repository, workflow,
environment, versioned target prefix, retained-predecessor policy, and channel.

## Deterministic clean-checkout build

CI runs these repo-owned interfaces from the tagged checkout:

```bash
scripts/build-control-deployment-bundle \
  --source-root deploy/compose \
  --output control-deployment.tar
scripts/publish-platform-target describe-bundle \
  --bundle control-deployment.tar \
  --repository ghcr.io/carstvaartjes/vonk-forge-control-deployment \
  > control-deployment-descriptor.json
scripts/build-platform-manifest \
  --input "release/platform/${RELEASE_VERSION}.input.json" \
  --bundle-descriptor control-deployment-descriptor.json \
  --artifact-evidence api-evidence.json \
  --artifact-evidence worker-evidence.json \
  --artifact-evidence hermes-evidence.json \
  --agent-package-evidence agent-package-evidence.json \
  --version "$RELEASE_VERSION" \
  --output platform-release.json
```

The workflow derives each published image reference from the digest emitted by
its own Buildx step, fetches that digest's raw manifest, SBOM and provenance,
and replaces the corresponding release-input locator with canonical evidence.
Thus a reviewed input cannot silently redirect the API, worker, or Hermes
artifact. The builder also rejects noncanonical input, an input-supplied bundle
descriptor, duplicate evidence locators, version mismatch, an existing output,
or a release that fails the shipped v2 parser and schema.

The unprivileged build job pins ORAS setup, resolves its absolute executable,
and publishes only the bundle:

```bash
export VONK_PLATFORM_ORAS_BIN=/absolute/path/to/oras
scripts/publish-platform-target publish-bundle \
  --manifest platform-release.json \
  --bundle control-deployment.tar > bundle-publication.json
```

Only after that artifact is uploaded does the OIDC job invoke the authority:

```bash
export VONK_PLATFORM_TUF_PUBLISHER_BIN="$PWD/scripts/platform-release-authority"
export VONK_PLATFORM_CHANNEL_PUBLISHER_BIN="$PWD/scripts/platform-release-authority"
scripts/publish-platform-target publish-authority \
  --manifest platform-release.json \
  --bundle control-deployment.tar \
  --bundle-publication bundle-publication.json \
  --channel stable > authority-publication.json
```

Each executable is opened without following links, must be a single-link
regular executable that is not group/world-writable, and is run through its
validated descriptor. Subprocesses receive only their exact proxy/TLS,
registry, or OIDC inputs plus a fixed path/locale; output, time, and process
groups are bounded.

## Publication order and retry

The only valid order is:

1. upload the empty OCI config blob, canonical bundle layer, and OCI manifest
   by digest;
2. publish canonical release bytes as
   `platform/releases/X.Y.Z/<manifest-sha256>.json` and retain every exact
   supported predecessor target;
3. receive and validate the new positive TUF targets version; and
4. publish the canonical `stable` discovery document with an ETag compare-and-
   swap.

The immutable target is append-only. An exact target replay returns the same
receipt; a different document under the same name is rejected. A channel retry
with byte-identical content is accepted. Any different update must advance
`tuf_targets_version` strictly; equal or lower versions are rejected. OCI and
target steps are safe to retry after interruption. The channel is updated last,
is discovery-only, and is never an installation or rollback authority.

The workflow uploads and attaches the signed ARM64 `.deb`, its checksum,
embedded SBOM/provenance and keyless Sigstore bundle, plus the canonical image manifests, SBOMs,
provenance documents and artifact-evidence records, the deployment descriptor,
platform manifest, bundle/authority receipts, and the deterministic installable
host-updater archive plus its checksum. Keep them together. The receipts bind
target name/SHA-256, TUF targets version, bundle descriptor, channel, and
channel-document SHA-256.

A third minimal protected job downloads the already-built host-updater archive
and signs GitHub/Sigstore build provenance with `actions/attest`. It has
`contents: read`, `id-token: write`, and `attestations: write`, but no package,
release, registry, or TUF publication permission. Operators verify this
attestation with `gh attestation verify` before the first root installation.

## Delegated authority HTTP contract

`scripts/platform-release-authority` obtains a short-lived GitHub Actions OIDC
token for the configured audience and calls only the HTTPS endpoints below.
Responses are canonical JSON and bounded to 64 KiB.

Immutable target publication:

```text
PUT /v1/platform/targets/<percent-encoded-target-name>
Authorization: Bearer <OIDC>
Idempotency-Key: <target-sha256>
If-None-Match: *
Content-Type: application/json
```

The canonical request contains `schema_version`, `target_name`,
`target_sha256`, base64 canonical manifest bytes, and the ordered
`retained_targets`. Success is `200` or `201` with the exact values and a
positive `targets_version`. On `409` or `412`, the client performs `GET` on the
same URL and accepts only an exact receipt; no overwrite is attempted.

Discovery-channel publication first reads
`GET /v1/platform/channels/stable`. Creation uses `If-None-Match: *`; replacement
uses `If-Match: <current-etag>`. The body is the canonical discovery document.
Only a byte-identical replay or a strictly greater TUF targets version is
accepted. A missing ETag, stale compare-and-swap, alias named `latest`, or
mismatched receipt fails the workflow.

## Authority implementation and incident response

The authority implementation must validate the GitHub issuer, audience,
repository, workflow ref/SHA, protected environment, tag, target prefix,
manifest canonicality, retained-target existence, and monotonic channel rule
before signing. It must publish consistent-snapshot target metadata atomically
and keep supported predecessors fetchable. This repository owns the client
contract; the authority may be hosted on any hardened Docker-capable machine.

If publication stops before the channel step, correct the infrastructure issue
and rerun the same tag: content-addressed OCI objects and the exact target replay
are idempotent. If the target receipt differs, the channel advanced elsewhere,
or signing policy is uncertain, stop. Preserve the receipts and audit log; do
not delete metadata, repoint the channel manually, rotate root trust online, or
publish a replacement under the same immutable name.
