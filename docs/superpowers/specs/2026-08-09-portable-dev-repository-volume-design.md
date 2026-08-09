# Portable Development Repository Volume Design

## Goal

Make the portable development Compose project run completely from an SMB
transport directory without exposing the source checkout's `.git` directory or
depending on SMB ownership and mode behavior at container runtime.

Success means a freshly published project can build on a Docker host, initialize
PostgreSQL, apply all migrations, start the worker and API, pass the API readiness
probe, survive a Compose restart, and shut down cleanly. The same acceptance path
must pass locally before the project is republished to the NAS share.

## Repository artifact

Add `scripts/dev-compose-repository`, an idempotent Ubuntu-side helper that
atomically creates `.dev/vonk-forge-repository.bundle` from
`refs/heads/main`. The Git bundle contains committed objects and the `main` ref,
but none of the source checkout's Git configuration, reflogs, hooks, credentials,
worktrees, or uncommitted files. The existing `.dev/` ignore rule excludes it
from Git.

Both publishers require this artifact and copy it to the portable project's
`.dev/` directory. The PowerShell publisher only copies the existing artifact;
it does not run Git or generate repository state on Windows. A missing artifact
fails publication before the existing destination is replaced.

## Runtime repository

Add a `dev-repository` Docker named volume. `dev-init` mounts the published Git
bundle read-only and the named volume at `/repository`. It runs from the API
image target, which contains Git, and performs the following initialization as
UID/GID 10001:

1. Create an empty Git repository in the named volume when `.git` is absent.
2. Fetch `refs/heads/main` from the bundle into local `main`.
3. Check out `main` and verify that `HEAD` is a commit.
4. Refuse a non-empty, non-repository volume or malformed bundle.
5. Leave an existing valid repository untouched on ordinary restarts.

The API mounts `dev-repository` read-write because its development code-host
adapter creates signed local changes. The worker mounts it read-only. Both use
`VONK_REPOSITORY_PATH=/repository`, and the development deployment branch is
explicitly `main`.

The SMB project directory remains a build/import transport only. It is not bind
mounted into API or worker containers. This removes NAS ACLs and SMB mode bits
from the live repository boundary.

## Existing development state

PostgreSQL, identity, state, route, supervisor, runtime-secret, and repository
data remain in Docker named volumes. Republishing files does not delete them.
To intentionally reseed the repository from a newer bundle, the operator removes
only the development repository volume and redeploys; ordinary project rebuilds
preserve it.

## Failure behavior

- Secret and repository artifacts are validated before publication staging.
- `dev-init` exits nonzero if the repository bundle is invalid or the named
  volume has unsafe unexpected content.
- API and worker do not start unless `dev-init` succeeds.
- The source `.git` directory is never synchronized to SMB.
- No production credentials or production repository provider behavior are
  introduced; this remains a development-only stack.

## Verification

Focused tests cover atomic Git-bundle generation, byte-for-byte publication,
missing-artifact failure, repository initialization, restart preservation, and
the rendered Compose mounts and dependencies.

The final local acceptance test publishes a fresh portable project into a
temporary directory, makes that host project inaccessible to UID 10001 to model
SMB/NAS permissions, then runs the actual Compose project with fresh volumes. It
must verify:

- all images build from the published context;
- `dev-init` and `migrate` exit zero;
- PostgreSQL and worker remain running;
- the API becomes healthy and `/api/v1/readyz` returns success;
- `/repository/.git` exists in the named volume and resolves `main`;
- a Compose restart returns the project to healthy state without reseeding or
  deleting data;
- teardown removes only the temporary acceptance project and its volumes.

Only after this full acceptance path passes is the project republished to
`Z:\vonk-forge`.
