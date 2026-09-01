# Node-bound agent repair capsules

Use a repair capsule only when an enrolled Spark cannot complete the ordinary
controller-managed agent upgrade and the exact failed state has been captured
in a reviewed repair authority. A capsule is a one-node recovery artifact, not
an agent release, APT package, or reusable upgrade channel.

The public `manifest.json` is an unsigned, package-signed descriptor. Its
`package_signature` authenticates the complete DEB SHA-256 with the Vonk host
release key. The signed DEB embeds the exact repair authority; its authority
digest and node ID determine the only valid public URL.

## Required gates

Before building or publishing, require all of the following:

- Live, read-only preflight matches the reviewed node ID, installed package,
  running agent/helper identities, and stale recovery state byte for byte.
- The repair version is a distinct canonical Debian version strictly newer
  than its ordinary target version.
- The signed DEB passes explicit repair verification for the expected node and
  authority. Default `verify-agent-deb` verification must reject it.
- Provenance identifies the target binary source and repair-packaging source as
  separate Git revisions. The binary source must match the ordinary target;
  the packaging source must be the reviewed repair implementation.
- `VONK_AGENT_RELEASE_KEY_FINGERPRINT` is the protected environment authority,
  computed as SHA-256 of the raw 32-byte Ed25519 public key. It must not be
  derived from the candidate DEB.
- The native repair state-machine suite, ordinary-package regression suite,
  and independent safety review pass.

Do not publish, dispatch, or retry an ordinary upgrade while any gate is open.

Before signing, bind the two source authorities independently. In a clean,
reviewed checkout, `REPAIR_PACKAGING_SOURCE_REVISION` must equal
`git rev-parse HEAD`, and `git status --porcelain` must be empty. Derive
`REPAIR_BINARY_SOURCE_REVISION` from the already-verified immutable ordinary
target release selected by the reviewed preflight, not from a repair build
environment variable. Verify that release's DEB, release signature, binary
digest, full source revision, and schema-2 capsule-capable recovery runner
before supplying its binaries to the repair builder. A full SHA that merely
shares a 12-character version prefix is not sufficient authority.

## Assemble the immutable publication bundle

The signed DEB must have its standard `.sha256` and `.host.sig` siblings. Use
the authority values produced by the reviewed preflight, never values observed
from an untrusted candidate package.

```sh
scripts/verify-agent-deb --json --repair \
  --expected-node-id "$REPAIR_NODE_ID" \
  --expected-repair-authority-sha256 "$REPAIR_AUTHORITY_SHA256" \
  --expected-release-key-sha256 "$VONK_AGENT_RELEASE_KEY_FINGERPRINT" \
  --expected-binary-source-revision "$REPAIR_BINARY_SOURCE_REVISION" \
  --expected-packaging-source-revision "$REPAIR_PACKAGING_SOURCE_REVISION" \
  "$REPAIR_DEB"

scripts/repair-capsule-publication assemble \
  --deb "$REPAIR_DEB" \
  --expected-node-id "$REPAIR_NODE_ID" \
  --expected-authority-sha256 "$REPAIR_AUTHORITY_SHA256" \
  --expected-release-key-sha256 "$VONK_AGENT_RELEASE_KEY_FINGERPRINT" \
  --expected-binary-source-revision "$REPAIR_BINARY_SOURCE_REVISION" \
  --expected-packaging-source-revision "$REPAIR_PACKAGING_SOURCE_REVISION" \
  --output "$REPAIR_BUNDLE"
```

The assembler derives the dispatch manifest only from the successful verifier
JSON. It records separate binary and packaging source revisions in the local
canonical publication plan. Review the plan, manifest, and exact object list:

```sh
jq . "$REPAIR_BUNDLE/publication-plan.json"
find "$REPAIR_BUNDLE/objects" -type f -print | LC_ALL=C sort
```

There must be exactly two publishable objects under this leaf:

```text
repair-capsules/{node_id}/{authority_sha256}/{package_sha256}/vonk-forge-agent.deb
repair-capsules/{node_id}/{authority_sha256}/{package_sha256}/manifest.json
```

Reject any `current`, `latest`, channel, APT, `artifacts/dev`, or
`artifacts/stable` object. Never pass a repair version to `agent-apt-state`, an
APT publication action, or installer promotion.

## Publish and verify

From the protected operator boundary configured for the existing installer R2
bucket:

```sh
scripts/repair-capsule-publication publish \
  --bundle "$REPAIR_BUNDLE" \
  --expected-release-key-sha256 "$VONK_AGENT_RELEASE_KEY_FINGERPRINT" \
  --expected-binary-source-revision "$REPAIR_BINARY_SOURCE_REVISION" \
  --expected-packaging-source-revision "$REPAIR_PACKAGING_SOURCE_REVISION" \
  --rclone-remote "$INSTALL_R2_REMOTE"
```

The publisher performs a full local repair verification again. It preflights
both immutable remote objects, permits only exact replay, uploads the DEB
first, verifies its remote bytes, uploads `manifest.json` last, and then
verifies both objects through `https://install.vonkforge.ai`. A differing or
unknown object fails closed without replacement.

Do not manually upload either object. A DEB-only leaf is safely incomplete and
may be completed by rerunning the same bundle. A manifest without its exact DEB
is invalid and must be investigated rather than repaired in place.

## Dispatch and acceptance

Import the published `manifest.json` through Fleet's single-node advanced
repair flow. Confirm the selected Spark matches `node_id`, review every package
digest, use `one-at-a-time`, and enter the required node-specific confirmation.
Do not advance another Spark until the repaired node reconnects with the exact
target agent and helper receipts.

Retain the authority, verifier JSON, canonical publication plan, signed DEB,
manifest, controller plan digest, operation log, and post-repair identity
evidence. Physical acceptance requires a successful ordinary controller-managed
upgrade after recovery; publication or reconnect alone is not acceptance.
