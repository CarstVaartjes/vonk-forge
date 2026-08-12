# Rootless Build Storage Accounting Design

## Problem

The Rust agent runs each recipe build in an operation-private Podman graph root
and scans that directory while the subprocess runs to enforce the recipe's
temporary-storage limit. On Ubuntu 24.04 with rootless Podman 4.9,
`fuse-overlayfs` materializes some image directories as subordinate UIDs with
mode `0700`. The unprivileged parent agent cannot traverse those directories,
so complete accounting fails with `EACCES` and the agent terminates an otherwise
valid build as `rootless image build failed`.

Skipping unreadable directories is not acceptable because it would create an
unaccounted path inside attacker-controlled build storage.

## Selected approach

Every Podman command that opens the operation-private graph root will use the
same storage options:

- `overlay.ignore_chown_errors=true`;
- `overlay.mount_program=/usr/bin/fuse-overlayfs`;
- `overlay.force_mask=shared`.

The containers/storage `shared` force mask makes graph-root files and
directories host-traversable while `fuse-overlayfs` presents their original
permission metadata inside the container. The graph root remains below
`/var/lib/vonk-forge-agent`, whose mode-`0700` ownership boundary prevents any
other host user from reaching it. The parent agent can therefore account for
every file without broadening the host trust boundary.

The common arguments will be produced by one helper and reused for build,
inspect, Docker-archive export, and image cleanup. A regression test will fail
if any command omits or changes the storage mask.

## Rejected approaches

- Treat `PermissionDenied` as absent during directory scans. This weakens the
  declared storage limit and is fail-open.
- Delegate accounting or builds to the privileged Docker helper. That expands
  the root boundary and contradicts source-build isolation.
- Require filesystem project quotas. They are not portable across supported
  Spark/Ubuntu installations and do not replace accounting of the complete
  temporary graph root.

## Verification

Verification must include:

1. a red/green Rust regression test covering all four Podman command paths;
2. the complete `vonk-agent` crate test suite plus formatting and Clippy;
3. signed ARM64 package lifecycle CI;
4. activation through the documented A/B canary path on Spark 1;
5. a fresh physical synthetic slice proving build, Docker import, runtime,
   route publication, inference, stop, withdrawal, and uninstall;
6. targeted cleanup of diagnostic graph roots after acceptance.

## Documentation

The development workload runbook will state that the private graph-root
ancestor and `force_mask=shared` are a paired boundary: the mask enables exact
accounting, while the ancestor prevents host disclosure. It will also document
the bounded Caddy 502 window during a single-replica development API redeploy
and the post-redeploy checks that distinguish it from a persistent fault.
