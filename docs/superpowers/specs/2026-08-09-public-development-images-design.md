# Public Development Images and Compose-Only NAS Design

## Goal

Run the Vonk Forge development control stack on the NAS from one generated
`docker-compose.yml` plus operator-owned runtime secret files. The SMB project
never receives source code, Dockerfiles, build contexts, Git metadata, helper
scripts, or image archives, and the NAS never builds an application image. A
committed checkout exists only inside the NAS-local repository volume required
by the running control plane.

The project directory is:

```text
vonk-forge/
├── docker-compose.yml
└── secrets/
    ├── postgres-password
    ├── database-url
    └── git-signing-key
```

The Compose file is replaceable. The secret directory and Docker named volumes
survive image updates.

## Build once and promote by digest

GitHub Actions builds the public API and worker images once for each accepted
`main` commit and publishes them to the existing packages:

```text
ghcr.io/carstvaartjes/vonk-forge-api
ghcr.io/carstvaartjes/vonk-forge-worker
```

Development tags use `dev-sha-<full-40-character-commit>`. The generated
development Compose file pins both the readable development tag and immutable
manifest digest. The workflow never publishes `latest`.

A production release does not rebuild these images. After all production gates
pass, it adds the stable `vX.Y.Z` tag to the already-tested digest for the tagged
commit and records that digest in signed release metadata and the platform
target. Tags identify channels; digest identity, attestations, signed release
evidence, and production configuration authorize production use.

## Development workflow

A dedicated `.github/workflows/dev-images.yml` runs on pushes to `main` and
manual dispatch. It has `contents: read` and `packages: write`, but uses no
GitHub environment and receives no repository or development secrets. Both
triggers publish only the current `origin/main` tip. A manual run must select
`refs/heads/main`; the workflow checks that its checked-out commit equals the
freshly fetched `origin/main` tip before any image build or package-write step.
Runs selected from another branch or tag fail closed.

The workflow:

1. Checks out full committed history, fetches `origin/main`, and proves the
   selected commit is the current `origin/main` tip.
2. Runs focused source, Compose, migration, and image-input tests.
3. Builds linux/amd64 API and worker images locally with commit development tags.
4. Scans image filesystems and metadata for forbidden credential paths, secret
   filenames, `.dev`, `.env`, private-key markers, and unexpected build args.
5. Generates synthetic, job-local runtime test credentials after image build.
6. Runs the complete image-only Compose stack, including repository
   initialization, PostgreSQL migration, worker/API health, readiness request,
   restart, and clean teardown.
7. Logs in to GHCR only after those checks pass, pushes the exact tested images,
   and records their digests and GitHub provenance attestations.
8. Renders and uploads one `docker-compose.yml` artifact with exact image
   digests and source commit. The artifact contains no secret values.

The workflow fails before registry login or publication if any build, scan, or
runtime acceptance check fails.

## Image inputs and secret exclusion

The root `.dockerignore` remains an explicit allowlist. It excludes `.dev`,
`.env` files, private-key and certificate formats, credential files, shell
credentials, cloud configuration, Git credentials, and source `.git` metadata.
Neither image build receives build arguments, BuildKit secret mounts, GitHub
tokens, registry credentials, or runtime credentials.

The workflow's `GITHUB_TOKEN` is used only by the GHCR login and publication
steps after the tested images already exist. Tests verify the final image
filesystem rather than relying only on source-pattern assertions.

## Compose runtime

The generated Compose project contains these services:

- `postgres`: the existing digest-pinned upstream PostgreSQL image.
- `dev-init`: a one-shot service using the exact API image.
- `migrate`: a one-shot service using the exact API image.
- `control-worker`: the exact worker image.
- `control-api`: the exact API image.

There is no dedicated init image. The API package exposes
`python -m vonk_control.dev_init`; production Compose never invokes it.

`dev-init` runs as root only for named-volume ownership setup. It clones the
public GitHub repository at the exact 40-character commit embedded in the
generated Compose file into a NAS-local `dev-repository` named volume, checks
out local `main`, verifies the commit, and assigns the checkout to UID/GID
10001. The generated Compose file sets `VONK_DEPLOYMENT_BRANCH=main` for API and
worker, and passes `VONK_DEV_EXPECTED_COMMIT=<40-character-main-commit>` only to
`dev-init`. Initial deployment therefore requires outbound HTTPS access to
public GitHub as well as GHCR.

On every subsequent start, `dev-init` validates that the existing checkout is a
non-symlink Git repository with a clean worktree, fetches public `origin/main`
without credentials, and verifies that the expected commit is reachable from
that ref. If local `main` already equals the expected commit, initialization is
idempotent. If local `main` is an ancestor of the expected commit, `dev-init`
updates only `refs/heads/main` with a compare-and-swap and resets the clean
checked-out `main` worktree to that commit. Other local branches and refs are
preserved. A dirty worktree, non-fast-forward transition, missing expected
commit, changed origin URL, or attempted silent rollback fails initialization;
an intentional rollback requires explicitly recreating only the development
repository volume.

It also initializes synthetic development identity/state and stages runtime
credentials before exiting successfully. API and worker cannot start until it
completes.

The API mounts `dev-repository` read-write because the development code-host
adapter creates signed local changes. The worker mounts it read-only. No service
bind-mounts the Compose project directory as `/repository`, so SMB/NAS ACLs are
outside the live application boundary.

## Runtime secrets

All durable secret inputs are NAS files under `./secrets/` and are absent from
Git, GitHub Actions artifacts, Compose environment values, images, and image
layers. Compose declares each file separately and mounts it read-only only into
the services that need it:

- `postgres-password` is consumed only by PostgreSQL.
- `database-url` is consumed by `dev-init` and projected read-only for migration.
- `git-signing-key` is consumed by `dev-init` for the API runtime projection.

`dev-init` writes two disjoint named-volume projections because API and worker
currently share UID 10001 and filesystem modes alone cannot isolate them:

- `dev-api-secrets` contains the database URL, Git signing key, and generated
  admin private key. Only `control-api` mounts it, read-only.
- `dev-worker-secrets` contains a separate database URL copy and generated
  worker API token. Only `control-worker` mounts it, read-only.

Migration receives only the database URL through its own read-only Compose
secret mount. Neither API nor worker mounts the other service's projection, and
the shared `dev-runtime-secrets` volume is removed. Secret contents are never
written to logs, Compose output, manifests, workflow summaries, or image labels.

## Development and production separation

Development and production share tested API/worker image digests, not Compose
configuration or runtime authority. Separation is explicit through:

- `dev-sha-*` versus stable `vX.Y.Z` tags;
- development versus signed production deployment artifacts;
- `VONK_DEPLOYMENT_MODE=development` and synthetic generation identity;
- a development-only `dev-init` command and named volumes;
- production-only release gates, attestations, signed platform targets,
  infrastructure, and secret providers.

The development Compose file cannot serve as a production deployment artifact.
The production release workflow cannot select a development tag without
matching and promoting its immutable digest through the release gates.
Production is initiated only by a signed `vX.Y.Z` tag whose commit already has
the accepted `main` development images; it never treats a branch, manual
workflow ref, or untested rebuild as production input.

## Operator flow

For a new accepted commit, the operator downloads the workflow's generated
Compose artifact and replaces only `vonk-forge/docker-compose.yml` on the SMB
share. UGREEN Docker rebuild is unnecessary; project redeployment pulls the
public digest-pinned images. The existing `secrets/` directory and named volumes
remain untouched.

No registry login is required while the GHCR packages are public.

## Failure and verification behavior

- Missing or unreadable NAS secret files prevent Compose deployment before API
  startup.
- A repository clone that does not resolve to the exact expected commit makes
  `dev-init` fail and blocks migration, API, and worker.
- Migration failure blocks API and worker.
- Image publication is impossible until filesystem secret scanning and full
  image-only acceptance pass.
- Local verification can build the same targets and run the same generated
  Compose graph before workflow publication changes are pushed.
- The final handoff verifies the actual `Z:\vonk-forge\docker-compose.yml`
  contains only image-based services, digest pins, relative secret paths, and
  named volumes; it does not inspect or print secret contents.
