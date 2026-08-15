# Remote development project publication design

Date: 2026-08-15

Status: approved by the operator's standing instruction to make the physical
installation reproducible and use best judgement for implementation details.

## Problem

The development project publisher deliberately requires POSIX ownership,
`fchmod`, local staging, and an exclusive `flock`. A Linux mount backed by an
ordinary local filesystem can provide those guarantees. The Windows/WSL view
of an SMB drive can instead appear as `9p`, DrvFs, CIFS, or another filesystem
that cannot preserve them. Publishing directly through that view therefore
fails closed, even though the same project is writable in Windows Explorer.

The NAS Docker UI still needs the simple operator contract:

```text
vonk-forge/
├── docker-compose.yml
└── secrets/
```

Cloning the repository onto the NAS, generating secrets on Windows/SMB, or
weakening the publisher's mode and locking checks are not acceptable fixes.

## Decision

Add a generic `scripts/dev-runtime-project-remote` entrypoint. It accepts the
same Compose, secret, hostname, address, and CIDR inputs as
`scripts/dev-runtime-project`, plus an SSH target, optional identity file,
absolute remote project path, and a choice between direct Docker access and
non-interactive sudo.

The workstation snapshots and validates the complete local source generation
under its shared generation lock. It creates one bounded in-memory tar stream
containing only:

- the accepted development Compose input;
- the complete validated local secret generation;
- `dev-runtime-project`; and
- `dev-runtime-secrets.py`.

OpenSSH carries that stream to a fresh mode-`0700` directory under NAS tmpfs.
The remote command extracts without retaining archive ownership or
permissions, fixes every staged input to mode `0600`, and removes the exact
tmpfs directory from a shell trap on success, failure, interruption, or SSH
disconnect. A NAS reboot also clears that tmpfs.

The remote command derives the publisher runtime image from the accepted
Compose anchor and permits only the public Vonk Forge development API image
forms. It pulls that image anonymously, then runs the mounted publisher with:

- no network;
- a read-only root filesystem;
- every capability dropped;
- `no-new-privileges`;
- the remote operator's numeric UID and GID;
- the tmpfs stage mounted read-only;
- only the destination parent mounted writable; and
- a bounded, non-executable `/tmp` tmpfs.

The existing publisher remains the sole renderer, source validator,
destination validator, transaction journal owner, rollback mechanism, and
byte-for-byte verifier. It operates on the NAS's real Linux filesystem, not
the SMB client view. The remote wrapper never interprets or prints secret
values.

## Trust and privilege boundary

Host-key checking remains OpenSSH's normal strict behavior. Batch mode is
mandatory; passwords and host-key prompts never share the secret transport.
The remote operator must already be able to run Docker directly or through
`sudo -n`. The guide must not recommend `NOPASSWD: ALL`; Docker authority is
root-equivalent and belongs to a dedicated NAS operator or an existing
administrative SSH identity.

The source Compose may contain either the accepted mutable `:dev` API alias or
an accepted pinned development API image. Production `:latest`, arbitrary
registries, and caller-selected publisher images are rejected. Production
deployment remains behind the trusted host updater.

The remote destination must be an absolute normalized POSIX path beneath a
non-root parent. Commas, control characters, traversal components, root, and a
project directly under `/` are rejected before SSH. These constraints keep the
writable bind mount narrow and unambiguous.

## Operator paths

The remote path is the recommended fresh-install and WSL/Windows path. Windows
Explorer or the NAS file manager is used only to confirm that the shared folder
shows `docker-compose.yml` and `secrets/`, then the Docker UI imports that
directory.

The existing mounted-path command remains supported only when the mounted
destination itself passes the publisher's POSIX filesystem and lock checks.
Failure on 9p, DrvFs, CIFS, or SMB is a security result, not a request to loosen
permissions.

## Verification

Tests must prove:

- exact bounded archive membership and private modes;
- local generation validation occurs before SSH;
- remote paths, SSH targets, identity files, Docker mode, and image forms fail
  closed;
- SSH uses batch mode and no pseudo-terminal;
- the remote script contains all container restrictions and exact cleanup;
- secret bytes never appear in argv, stdout, stderr, or generated commands;
- failures propagate without reporting publication success; and
- the fresh-install and full NAS runbooks recommend the remote path while
  retaining the two-item visible project contract.
