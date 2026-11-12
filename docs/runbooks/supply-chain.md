# Verify the platform supply chain

Standard service images are fixed by version and OCI index digest in
`deploy/compose/images.lock.json`; Compose uses those exact references as its
defaults. The custom control image is a release artifact and must be supplied
through `CONTROL_API_IMAGE`, `CONTROL_WORKER_IMAGE`, and `LITELLM_IMAGE`
with one complete set of registry digests. The three `vonk-forge` packages are
`ghcr.io/carstvaartjes/vonk-forge-api`,
`ghcr.io/carstvaartjes/vonk-forge-worker`, and
`ghcr.io/carstvaartjes/vonk-forge-litellm`. Build the `api` and `worker`
Dockerfile targets from the same release commit; the worker target deliberately
contains neither Git nor OpenSSH. The Node and Python build bases are separately
digest-pinned in the lock. Refreshing a pin is a manual, reviewed edit; follow
[Refresh the pinned container images](../image-pin-refresh.md).

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
image input. Production consumes the digest-pinned Compose and package assets
from one immutable GitHub Release. Docker does not update running containers
merely because a tag moves.

Dependabot checks Docker build inputs, Docker Compose files, and GitHub Actions
weekly and opens ordinary reviewed pull requests. It does not auto-merge, tag,
create a release, or publish an image; maintainers review an accepted update
before making a later deliberate version-tag release.

Run the offline gate before building or deploying:

```bash
scripts/verify-supply-chain --json
```

The verifier checks the Python, Rust, and web dependency lockfiles, the reviewed
third-party contracts wheel digest, runtime and build image pins,
Dockerfile/Compose relationships, and the digest of the LiteLLM cosign public
key. Both LiteLLM `FROM` lines must carry exactly the locked
`build_bases.litellm` digest. The verifier has no network access, so it cannot
check signatures; every CI job that builds the LiteLLM image
(`ci.yml`, `dev-images.yml` and the `litellm` leg of `release-acceptance.yml`)
first runs `scripts/verify-litellm-base-signature`. It rechecks that both
Dockerfile `FROM` lines equal the lock, then runs
`cosign verify --key deploy/compose/trust/litellm-cosign.pub` on the locked
base through `scripts/retry-dependency-fetch`, failing closed. A workflow test
requires that step, installed cosign and ordering before every LiteLLM build.
To move the public-contracts pin, run
`scripts/refresh-public-contracts <recipes-commit>`. It builds the wheel from that
commit, rewrites `control/pyproject.toml`, `control/uv.lock` (through `uv lock`),
`control/packaging/public-contracts.lock`, the wheel in `inventory/wheels` and
the wheel name in `control/Dockerfile` and `scripts/resolve-publication-producer`.
The wheels directory is git-ignored: stage the new wheel with `git add -f`.
The lock owns the version; the verifier derives the wheel path and checks the
Dockerfile names it, so no script or test carries a version literal. A recipes
contract major is deployed consumer-first: pin the recipes commit here, deploy,
then move the recipes `platform_ref`.
The verifier does not require
generated inventory in the checkout. The
protocol wheel and SPDX 2.3 files are build outputs. Generate release evidence
into a separate directory:

```bash
scripts/build-control-wheel
scripts/verify-supply-chain --output-dir /tmp/vonk-supply-chain --json
```

The helper builds the protocol wheel from source and verifies its SHA-256
against the Controller lock before local syncs, runs, or Docker image builds.

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
  --file deploy/compose/litellm/Dockerfile --target runtime \
  --tag vonk-forge-litellm:release-dry-run .
```

After future deliberate enablement, the tag-triggered GitHub Actions workflow
is the only publication procedure. It builds all three targets, verifies their
inputs, emits SBOM and provenance, resolves all three digests, and creates the
checksum-protected three-reference release asset. It never treats one or two
images as a releasable publication. LiteLLM base signatures are checked in CI (not at
publication) with the checked-in key copied from immutable upstream commit
`e82a3c646908ba0a6ee0beddc4310d7a6cac4065` (the vendor's 2026-10-23 key rotation;
SHA-256 of the PEM `e3fe3e08c44b16407fe0b37c43b4f8b603b3a0f4ee74c29e647bc022d762c2c0`,
as published in the vendor's October 21 incident post); the verified reference is the
locked digest, never a mutable tag. Store scan/signature attestations with the release evidence.

## Recipe workloads

Recipe images and model artifacts are not published by this repository. The
Controller builds or imports them from the exact recipe library revision and
verifies them before any Spark uses them; see the
[recipe operations runbook](model-switching.md). There is no separate workload
artifact publisher or workload signing root.
