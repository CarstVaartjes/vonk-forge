# Development and Production Agent Package Release Design

## Goal

Publish the ARM64 Vonk Forge agent as a reproducible, authenticated Debian
package in two explicit channels that mirror the public container channels:

- accepted `main` commits publish immutable development package artifacts and
  advance a signed apt `dev` suite;
- annotated, trusted `vX.Y.Z` tags publish immutable production package assets
  and advance the signed apt `stable` suite.

Container publication remains as designed in
`2026-08-09-public-development-images-design.md`: `dev` follows accepted
`main`, `latest` follows the highest completed stable release, and every
Compose artifact remains digest locked.

## Channel contract

| Source | Package version | Immutable evidence | Moving discovery channel |
| --- | --- | --- | --- |
| Exact accepted `main` tip | `X.Y.Z~dev.<commit-epoch>+g<sha12>` | GitHub Actions artifact, checksum, SBOM, provenance, Sigstore bundle, GitHub attestations | apt distribution `dev` |
| Trusted annotated tag `vX.Y.Z` reachable from `main` | `X.Y.Z` | GitHub Release assets, checksum, SBOM, provenance, Sigstore bundle, GitHub attestations | apt distribution `stable` |

`X.Y.Z` is the canonical workspace version in `Cargo.toml`. The root, control,
and Python-agent project versions must match it. A production tag must equal
that version. The Debian `~dev` suffix sorts before the corresponding final
version, so a node may move from a development candidate to its final release
without a downgrade override. Main must advance the workspace version before
publishing development builds for the next release line.

Package filenames and apt metadata are the only channel discovery mechanism.
There is no mutable `.deb` filename and no package called `latest` or `dev`.

## Development workflow

`.github/workflows/agent-release.yml` runs on pushes to `main` and may be run
manually only against `refs/heads/main`. Both triggers fetch `origin/main` and
fail unless `GITHUB_SHA` is its exact current tip. The workflow derives the
canonical development version from committed source metadata; it does not
accept an operator-supplied version.

The native Ubuntu 24.04 ARM64 job:

1. builds the package twice from the exact commit and requires byte equality;
2. runs the Rust, package-verifier, systemd, and full Debian lifecycle gates;
3. verifies development-to-newer-development and development-to-final Debian
   version ordering;
4. creates the checksum, SPDX/CycloneDX SBOMs, SLSA provenance, keyless
   Sigstore bundle, and GitHub artifact attestations;
5. uploads an immutable artifact named with the full commit SHA; and
6. exposes no registry, apt, R2, controller, NAS, or runtime secret.

The build uses the existing persistent agent release key so development and
production packages share the embedded slot-verification trust root. The key
is supplied only by a branch-restricted `agent-development` GitHub environment.
That environment stores the same `VONK_AGENT_RELEASE_PRIVATE_KEY` value as the
production `agent-release` environment but has no apt, R2, container, or
runtime credentials. The workflow verifies its derived public-key fingerprint
against the environment variable `VONK_AGENT_RELEASE_KEY_FINGERPRINT`, preventing
an accidental split trust root.

After build acceptance, a separate `apt-development` job downloads only that
workflow artifact, verifies it again, rechecks that the commit is still the
exact `origin/main` tip, and publishes distribution `dev`. Its concurrency group
is development-only: replacing an older pending run is safe because every
surviving run publishes the newest accepted main package. Production apt state
cannot be canceled, replaced, or overwritten by this group.

## Production workflow

The unified tag workflow remains the sole production package release path. It
continues to require an annotated SSH-signed `vX.Y.Z` tag whose commit is
reachable from `origin/main`, and now additionally requires the tag version to
equal the canonical workspace version. It builds and accepts the clean `X.Y.Z`
package, attaches the exact package/evidence set to the same GitHub Release as
the container and platform assets, and only then publishes apt `stable`.

The production package is rebuilt because its canonical Debian version differs
from the development package version. Reproducibility, exact source revision,
source labels, attestations, and lifecycle gates bind both builds to the same
reviewed source; production never relabels development bytes with different
metadata.

## Apt isolation and credentials

Both apt suites are public at `https://packages.vonkforge.ai`, but publication
state and signing authority are isolated:

- `apt-development` uses its own GPG signing key, keyring filename, private R2
  aptly-state bucket, and serialized publication group;
- `apt-release` retains the production GPG key, production private state bucket,
  and production serialized publication group;
- both use least-privilege R2 credentials for the shared public bucket, where
  `dists/dev` and `dists/stable` are disjoint and package pool filenames include
  immutable versions;
- neither environment receives the agent release private key; and
- no private key, passphrase, R2 credential, or runtime secret is written to a
  package, artifact, attestation, repository file, image, or Compose output.

Development clients install `vonk-forge-dev-archive-keyring.gpg` and select
distribution `dev`. Production clients install
`vonk-forge-archive-keyring.gpg` and select distribution `stable`. A channel
switch requires installing the destination keyring first and then changing the
apt source explicitly.

Private aptly state is checksum verified and extracted only after rejecting
absolute paths, traversal, and links. Development state is retained under
immutable commit/version paths plus `latest`; production state remains under
immutable release-version paths plus `latest`. Exact publication retries reuse
an existing trusted snapshot; a different package under an existing immutable
snapshot/version is rejected.

## Failure and ordering behavior

All publication is fail closed:

- a stale manual ref or superseded main commit cannot publish development;
- a development build failure leaves the previous apt `dev` suite unchanged;
- a tag/version mismatch cannot create production package assets;
- package bytes are verified before any apt signing key or R2 credential is
  materialized;
- public indexes are signed and locally verified before upload;
- the public apt tree is copied before the new private `latest` state pointer;
- immutable private state is written before `latest`;
- development and production failures cannot mutate the other channel's state;
  and
- reruns either reproduce the exact snapshot or fail rather than overwrite an
  immutable package version.

## Verification

Repository tests parse both workflows and execute package metadata/version
helpers against valid and adversarial inputs. Package tests cover canonical
development versions, malformed versions, tag/workspace mismatch, Debian
ordering, exact-main enforcement, secret and permission boundaries, apt-state
isolation, immutable artifact naming, and channel-specific keyrings.

The decisive CI acceptance remains a native ARM64 package build followed by
fresh install, offline reinstall, upgrade, downgrade rejection, configuration
preservation, remove, and reinstall. Local verification runs all architecture-
independent Python and workflow tests, Rust formatting/lint/tests, package
metadata rendering, supply-chain verification, YAML/shell syntax checks, and a
real deterministic package build when an AArch64 host and signing key fixture
are available.

## Operator documentation

`docs/operations/agent-package-release.md` documents:

- all four protected environments and their exact secrets/variables;
- creation, fingerprinting, backup, rotation, and separation of agent,
  development apt, and production apt signing keys;
- apt source installation commands for `dev` and `stable`;
- explicit channel switching and Debian version ordering;
- verification of GitHub artifact/release attestations and Sigstore bundles;
  and
- recovery from failed publication without reconstructing trusted state from
  the public bucket.
