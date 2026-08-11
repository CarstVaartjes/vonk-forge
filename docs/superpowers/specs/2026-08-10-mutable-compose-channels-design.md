# Mutable Compose channel design

## Goal

Make development follow its mutable image channel and make production follow
its signed release channel without requiring an operator to replace image
digests:

- `docker-compose.dev.yml` always pulls the newest accepted development images
  through `:dev`;
- the production host updater resolves the signed `stable` channel to its
  newest accepted immutable Compose bundle and image digests;
- public `:latest` aliases identify the images from that newest accepted
  production release but are not production deployment authority; and
- `docker-compose.pinned.yml` retains immutable image tags and digests for an
  explicit development reproduction or state-aware recovery.

The mutable development file is intentionally operator-controlled. Publishing
a new `:dev` alias does not restart a NAS project. The operator chooses when to
pull and redeploy through the Docker UI. Production remains operator-controlled
through an explicit host-updater `stable` apply; it never deploys by pulling
`:latest` directly.

## Operator contract

The official development NAS project contains exactly one replaceable Compose
file and three operator-owned runtime secret files:

```text
vonk-forge/
├── docker-compose.yml
└── secrets/
    ├── postgres-password
    ├── database-url
    └── git-signing-key
```

For normal development, `docker-compose.yml` is a copy of
`docker-compose.dev.yml`. It contains no repository checkout, Dockerfile,
build context, image archive, GitHub credential, or image digest. Updating is
an explicit **pull/redeploy** of the unchanged project in the NAS Docker UI.
The three secret files and all named volumes survive the redeploy.

Production keeps its larger existing runtime-secret and deployment-asset
contract. Selecting production `stable` means asking the trusted host updater
to resolve that signed channel to its newest exact TUF target. The updater
retains its root-owned active projection, backup, migration, preselection,
journaling, and rollback boundaries. This design does not change the production
authority split or secret inventory.

Docker does not continuously update a running container when a tag moves.
Documentation and UI wording must say `pull/redeploy`, not merely `restart`.

## Compose artifacts

### Development

`docker-compose.dev.yml` uses exactly these first-party references:

```yaml
image: ghcr.io/carstvaartjes/vonk-forge-api:dev
pull_policy: always
```

```yaml
image: ghcr.io/carstvaartjes/vonk-forge-worker:dev
pull_policy: always
```

The file contains no `@sha256:` suffix and no accepted source commit frozen at
render time. The verified runtime cohort supplies that commit to `dev-init`.

### Production

`docker-compose.production.yml` remains an immutable release artifact. The
trusted host updater obtains it from the exact TUF-authorized deployment bundle
and supplies the version-and-digest references selected by that release. An
operator selects the signed `stable` channel rather than editing
those digests, but Docker Compose never interprets mutable `:latest` as the
authority for a production migration or service start.

The release workflow still advances the public first-party `:latest` aliases
after the complete release succeeds. Those aliases are useful for discovery,
inspection, and disposable evaluation. They are not substituted into the
production Compose graph. Third-party images also remain pinned according to
the existing dependency policy.

### Pinned

`docker-compose.pinned.yml` continues to use immutable
`dev-sha-<40-character-commit>@sha256:<digest>` development references. Signed
production releases continue to publish immutable version-and-digest evidence
and deployment bundles. Production rollback uses the host updater's recorded
and still-authorized predecessor generation, not this development artifact.

The pinned development artifact is not updated in place on a NAS automatically.
An operator selects it deliberately when reproducing an accepted cohort or as
one input to the state-aware recovery procedure below. Replacing only the
Compose file is not a supported rollback after persistent state has advanced.

## Channel publication

The development workflow builds immutable `dev-sha-*` images first. It runs
the complete acceptance, secret scan, SBOM, provenance, attestation, and
manifest checks against those immutable images. Only after every image and the
Compose artifacts pass does it advance each public `:dev` alias.

The production workflow follows the same build-first shape for a trusted
annotated, SSH-signed release tag reachable from `main`. It publishes the
immutable TUF target, then completes immutable versioned images, the agent
package, platform manifest, deployment bundle, and public release evidence
before advancing the signed `stable` channel. Only after `stable` succeeds does
it reconcile the informational `:latest` aliases. A failure before complete
release evidence advances neither channel; a later `:latest` failure can leave
only the non-authoritative evaluation aliases stale and is repaired by the
serialized reconciliation job.

Development aliases in separate GHCR repositories cannot move in one registry
transaction. Publication ordering reduces the window but does not pretend it
is atomic. The development runtime cohort gate closes that boundary. Production
does not rely on cross-repository aliases: the host updater selects one
immutable platform target that already binds the complete cohort.

## Embedded release identity

Every first-party development image embeds one canonical, non-secret release
identity file at a fixed read-only path. At minimum it contains:

- schema version;
- source repository and exact 40-character source commit;
- development channel kind;
- platform semantic version;
- platform build identity;
- database revision;
- agent/control protocol compatibility range; and
- image role.

The identity is generated from accepted workflow metadata and copied into the
image during its immutable build. It contains no credential, runtime secret,
NAS path, or mutable registry lookup result. API and worker identities for one
cohort differ only in their declared image role.

Immutable image acceptance verifies the embedded identity against OCI labels,
workflow source authority, SBOM/provenance, and the expected development
metadata before any mutable alias advances. Production keeps its existing
stronger release-manifest, deployment-bundle, active-projection, and built-image
identity checks.

## Fail-closed runtime cohort gate

The mutable development Compose graph includes one short-lived reporter for
each first-party image role and one verifier. Each reporter runs from the same
image reference as its long-running service and copies only its embedded public
release identity into a dedicated file in a temporary named volume. Reporters
receive no runtime secrets and no Docker socket.

The verifier requires all expected role files and rejects:

- a missing or duplicate role;
- malformed, oversized, symlinked, or non-canonical metadata;
- different source commits, platform versions, build identities, database
  revisions, protocol ranges, or channel kinds;
- an identity that is not an accepted development image under `:dev`; and
- an unexpected repository or image role.

On success, it writes one canonical selected-cohort document to a separate
read-only runtime volume. Development `dev-init` obtains its exact accepted
repository commit from that document instead of from a rendered Compose
literal.

The pinned development artifact runs this same gate and derives every runtime
identity from the selected-cohort document. Its reporter images are immutable
`dev-sha-<commit>@sha256:<digest>` references, so pinning is enforced by image
selection rather than by a second, potentially divergent initializer input.

Migration, initializer, signer, API, worker, and other stateful services depend
on successful cohort verification. A mismatch therefore fails before database
migration or service mutation. Existing running containers remain available
until the operator initiates redeployment; if the candidate pull is mixed, the
new graph stops visibly and the operator retries after publication completes
or selects a pinned artifact.

The gate proves that the pulled development images belong to one accepted
cohort. It does not replace immutable build verification, GitHub environment
protection, attestations, or pinned recovery evidence. Production continues to
use its trusted host updater and does not use this gate as a substitute for TUF
selection or the root-owned active projection.

## Updates and rollback

The normal development update sequence is:

1. Wait for the development publication workflow to finish successfully.
2. In the NAS Docker UI, choose pull/redeploy for the existing project.
3. Confirm every cohort reporter and verifier exits successfully.
4. Confirm initializer and migration jobs exit successfully.
5. Confirm PostgreSQL and all long-running first-party services are healthy.

If cohort verification fails, do not delete secrets or volumes. Retry the pull
after publication is complete.

A pinned development file is not a one-file rollback. After cohort B advances
the persisted repository baseline beyond cohort A, `dev-repository-init` deliberately
rejects pinned A as a non-fast-forward accepted baseline. Recovery to A is
allowed only when A is explicitly documented as database-backward-compatible
and the operator performs the guarded repository-volume reset in the
[development NAS installation runbook](../../runbooks/development-nas-installation.md#advanced-guarded-recovery).
The operator must discover and verify the actual Compose repository volume,
confirm its labels and exact expected commit, stop the project, delete only
that repository volume, and restart with pinned A. The reset discards local
development branches and must never be represented as preserving all volumes.

If cohort B ran a migration that is not backward-compatible with A, the
operator must restore PostgreSQL, repository, generated-secret projections,
identity, route, supervisor, and other stateful volumes plus the three secret
files from one matching A recovery point. An operator with UI-and-SMB access
only may instead perform an explicitly destructive clean development install
after deciding that all development state can be discarded. There is no safe
UI-only in-place downgrade.

The normal production update sequence remains the host updater: resolve the
signed `stable` channel to its newest target, preview the exact target, apply
that same target, and let the updater perform backup, migration, preselection,
selection, readiness, and journaling. Production rollback selects the recorded
predecessor generation through the updater and requires that exact target to
remain authorized. The operator never edits or copies a production image
digest.

## Documentation policy

The development NAS runbook and Compose README lead with the two-item project
layout and mutable `:dev` pull/redeploy path. They describe the pinned file as
the explicit recovery/reproduction path, not the recommended normal install.

Release documentation describes `:latest` as images from the newest fully
accepted production release and states that it changes only after the complete
tagged release succeeds. It also states that production deployment selects the
signed `stable` channel through the host updater; `:latest` remains
discovery/evaluation metadata rather than production authority. Immutable
release manifests, versioned tags, digests, and deployment bundles remain the
deployment evidence and rollback records.

## Testing and acceptance

Automated contracts must prove:

- development rendering emits exact unpinned `:dev` references and
  `pull_policy: always`;
- the mutable development Compose file contains no first-party `@sha256:`
  reference or frozen expected commit;
- production rendering continues to require the immutable references selected
  by the signed release and rejects bare `:latest`;
- pinned rendering still requires matching immutable tags, commits, and
  digests;
- every development image embeds canonical release identity matching its
  immutable build evidence;
- missing, malformed, stale, cross-channel, and mixed-role cohorts fail before
  initialization and migration;
- a matching cohort supplies the exact development repository commit;
- alias promotion remains after all build, scan, attestation, artifact, and
  authority checks;
- selecting production `stable` resolves that signed TUF channel to its newest
  immutable target and exercises the existing host-updater lifecycle;
- the UGREEN-compatible project validates with only `docker-compose.yml` and
  the three development secret files; and
- updating the aliases and redeploying the unchanged Compose file starts the
  newer cohort while preserving secrets and named-volume state.

Acceptance includes a local lifecycle test that deploys cohort A, advances
synthetic mutable aliases to cohort B, pulls/redeploys the unchanged Compose
file, and proves B is active. A second test deliberately mixes A and B and
proves migration never runs. A rollback contract test proves that pinned A is
rejected over B's preserved repository volume, succeeds only after the guarded
repository reset when schema-compatible, and otherwise requires a matching
full-state restore. Production acceptance continues through the host updater,
never a direct `docker compose pull` of `:latest`.

## Scope boundaries

This change does not automatically redeploy a NAS, watch GHCR, alter runtime
secret values, relax signed release authority, replace the production host
updater, publish a local release, or change the Rust agent package channel. The
guarded development recovery path may delete one verified repository volume or
restore a complete matching recovery point only through an explicit operator
decision. Agent packages continue to use their independent `dev` and `stable`
apt distributions. Installing and enrolling those packages follows only after
the NAS control plane is healthy.
