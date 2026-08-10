# Development NAS installation and runtime secrets

This runbook installs the development control stack on a generic
`linux/amd64` NAS with Docker Engine and the Compose plugin, including the
UGREEN Docker Project UI. The normal development deployment is an
operator-chosen pull/redeploy of one unchanged Compose file; it does not clone
this repository into the project directory, build an image, or receive a
Dockerfile, build context, image archive, GitHub token, or production secret.

## Start with this two-item NAS project

Create one NAS-local project directory in the file manager, then select that
directory when creating a Docker/Compose project. Its contents must be exactly:

```text
vonk-forge/
├── docker-compose.yml
└── secrets/
    ├── postgres-password
    ├── database-url
    └── git-signing-key
```

Copy only those three development secret files into `secrets/`; do not put a
checkout, an image archive, or a production secret beside them. The Compose
file is replaceable, while `secrets/` and Docker named volumes survive normal
redeploys. The file contains secret *paths*, never secret values.

The publication workflows expose three clearly named files:

| File | Published by | Graph and image reference |
|---|---|---|
| `docker-compose.dev.yml` | Accepted `main` workflow | Development graph with bare mutable `:dev` references. This is the normal NAS artifact. |
| `docker-compose.production.yml` | Signed release workflow | Full production graph selected only by the trusted host updater; use its production deployment bundle and [production secret guide](../../deploy/compose/README.md#required-secret-file-paths). |
| `docker-compose.pinned.yml` | Accepted `main` workflow | Immutable development references for explicit reproduction or state-aware recovery. |

For normal development, copy `docker-compose.dev.yml` as the bare mutable `:dev`
artifact and rename it to `docker-compose.yml`. A moved tag does not change a
running project: after a successful publication, pull/redeploy the unchanged
`docker-compose.yml`, not restart containers and not replace the file. The
pinned file is deliberately an exception for
reproduction or recovery; this development guide never installs the production
graph or its much larger production credential set.

The three operator-owned inputs have narrow purposes:

| File | Consumer | Purpose |
|---|---|---|
| `postgres-password` | PostgreSQL only | Password for the development `control` database role. |
| `database-url` | initializer, migration, then separate API/worker projections | Matching SQLAlchemy URL for that role. |
| `git-signing-key` | initializer, then API projection only | Unencrypted Ed25519 SSH private key for development Git signing. |

On first start, `dev-init` generates an admin-grant private key and a separate
worker API token. It projects secrets into three distinct named volumes: API
gets the database URL, signing key, and admin-grant key; migration gets only
the database URL; worker gets the database URL and worker token. Those values
are not host files, image contents, CI inputs, Compose environment values, or
worker/API-shared authority.

## Obtain the normal development artifact

Open the successful `Development images` workflow run for the accepted `main`
commit. Download the artifact named
`vonk-forge-dev-compose-<40-character-commit>`. It contains
`docker-compose.dev.yml` and `docker-compose.pinned.yml`. Select
`docker-compose.dev.yml` and rename it to `docker-compose.yml` in the NAS
project directory. Do not edit the first-party image references or add digests:
the mutable `:dev` channel is intentionally selected when the Docker UI pulls.

In the NAS file manager, confirm that the artifact is named
`docker-compose.yml` and that the project UI identifies it as a Compose file;
do not edit image references, add a digest, or add a build section. Both GHCR
packages must be public. A pull-only NAS needs no registry login. If the Docker
UI requests credentials, stop and correct package visibility instead of
installing a GitHub token on the NAS.

## Create and copy the three NAS secret files

Use the NAS SMB share or NAS file manager to create `secrets/` in the same
project directory as `docker-compose.yml`, then copy the following exact files
into it. Create regular files, not folders or shortcuts; use UTF-8 without a
BOM and end each text value with one newline. Do not open values in the Docker
UI or put them in the Compose file.

| File | Exact content rule |
|---|---|
| `postgres-password` | 64 lowercase hexadecimal characters followed by one newline. |
| `database-url` | `postgresql+psycopg://control:<postgres-password>@postgres:5432/control` followed by one newline, where `<postgres-password>` is the exact value in `postgres-password`. |
| `git-signing-key` | One unencrypted Ed25519 OpenSSH private key followed by one newline; the initializer has no interactive passphrase input. |

Do not overwrite existing secret files during a normal redeploy. If an SMB
client created the files, safely eject/disconnect the share after copying and
use the NAS file manager to confirm only the three expected names appear.

The numeric owners match the pinned images: PostgreSQL is UID/GID `999`, while
the API and migration run as `10001:10001`. Docker implementations differ in
how bind-backed Compose secrets expose ownership; these owners and mode `0400`
are the restrictive compatible host settings. The Docker daemon must be able
to traverse the root-owned `0700` secret directory.

Expected owners are `999:999`, `10001:10001`, and `0:0`, respectively; every
mode is `400`. File sizes are safe to display. Never use `cat`, `Get-Content`,
or a screenshot that reveals values. Confirm the names, presence, and file
sizes through the NAS file manager; do not copy configuration output into
diagnostics.

## SMB/file-manager preparation

Use an SMB client only to copy the three already-prepared files into
`secrets/`. Generate password and private-key material through the
organization's approved secret-management process rather than by pasting a
command into a client terminal. The SMB client must create regular files with
the exact names and content rules above; it must not create a public key,
temporary copy, or duplicate filename in the project directory.

Obtain the unencrypted private key through the approved secret-management
process, then copy it as a regular file from protected local storage. SMB is a
copy path, not a secret-generation environment; its ordinary cleanup does not
guarantee secure erasure from snapshots or managed Windows storage.

Windows ACLs on an SMB drive do not establish the Linux numeric ownership used
inside containers. Use the NAS administration interface's file-permission
controls to apply the ownership and modes above before deploying; do not use a
Docker-project action to change secret-file permissions.

## Create and redeploy the Compose project

In a generic NAS Docker UI (UGREEN calls this a Docker Project):

1. Create or import a project from the NAS-local `vonk-forge/` directory.
2. Select `docker-compose.yml`; retain its relative `./secrets/...` paths.
3. Verify that `secrets/` contains `postgres-password`, `database-url`, and
   `git-signing-key`, without opening their contents in the UI.
4. Choose **Pull** then **Redeploy** for the project. Do not choose build or
   restart; there is no build context and restart cannot fetch a moved `:dev`
   image.
5. Keep every named volume. Do not choose a remove-volumes or clean-project
   option during normal development.

After the UI reports the deployment, follow the two prerequisite lanes in the
job and container status: the cohort reset, API and worker cohort reporters,
and cohort verifier complete in one lane while PostgreSQL becomes healthy in
the other. Only then may `dev-init` and `migrate` complete, followed by the
long-running API and worker becoming healthy. A one-shot service that exits
successfully is complete, not failed. The API binds only to NAS loopback; use
your organization's approved trusted access path rather than widening the
Compose listener.

## Update after an accepted development publication

Keep `docker-compose.yml` unchanged. In the NAS Docker UI, open the existing
project and pull/redeploy it. Do not replace the Compose file, restart existing
containers, delete `secrets/`, or delete named volumes. The repository
volume has two deliberately separate branches: `main` is the accepted
origin-tracking baseline from public `origin/main`, while `deploy` is the
mutable runtime branch used for locally signed development changes.
`refs/vonk/deploy-base` is the exact merge-base between those branches. On each
start, `dev-init` fetches public `origin/main`, verifies the artifact's exact
accepted commit, and updates `main`; it advances `deploy` and
`refs/vonk/deploy-base` together only when `deploy` still equals that base.
Other local refs are preserved. A dirty checkout, rollback, changed origin,
missing commit, non-fast-forward accepted baseline, or merge-base not exactly
equal to `refs/vonk/deploy-base` fails closed.

Moving aliases can be temporarily inconsistent because API and worker images
are published from separate repositories. On a mixed pull, the cohort gate
exits before `migrate`, so it prevents database migration and other stateful
startup. Do not delete `secrets/` or named volumes. Wait for publication to
finish, then pull/redeploy the same unchanged project again; if the published
cohort is still mixed, use the pinned artifact only through the recovery path
below.

An older pinned image may be incompatible with schema or data already written
by a newer migration. Use the guarded repository-volume reset only when the
target schema is compatible. For an incompatible migration, use a matching
full-state restore (or perform a clean development reinstall) with every
stateful volume and all secret files from one matching recovery point. That
includes PostgreSQL, identity, control state, route publications, supervisor
state, repository, and the generated API, migration, and worker secret
projections.
Never treat a repository-volume
reset as a database or runtime-state rollback.

## Advanced guarded recovery

The following shell procedure is for the explicitly documented,
schema-compatible repository-only recovery case. Do not use it for normal
installation, updates, or an incompatible migration.

For a documented repository-only rollback, discover the actual volume name
from the running API before stopping the project. This remains correct if a NAS
UI changes the Compose project name:

```bash
set -eu
cd /volume1/docker/vonk-forge
api_container=$(sudo docker compose -f docker-compose.yml ps -q control-api)
test -n "$api_container"
repository_volume=$(sudo docker inspect "$api_container" --format \
  '{{range .Mounts}}{{if eq .Destination "/repository"}}{{.Name}}{{end}}{{end}}')
test -n "$repository_volume"
volume_identity=$(sudo docker volume inspect "$repository_volume" --format \
  '{{index .Labels "com.docker.compose.volume"}} {{index .Labels "com.docker.compose.project"}}')
case "$volume_identity" in
  'dev-repository '*) ;;
  *) echo 'refusing an unrecognized repository volume' >&2; exit 1 ;;
esac
printf 'repository volume selected for deletion: %s (%s)\n' \
  "$repository_volume" "$volume_identity"
printf 'Type the exact volume name to confirm: '
read -r confirmation
test "$confirmation" = "$repository_volume"
sudo docker compose -f docker-compose.yml down
sudo docker volume rm -- "$repository_volume"
sudo docker compose -f docker-compose.yml up -d --wait
```

This is destructive to local branches and unpushed changes in that one volume.
Do not use `down --volumes`. A rollback that requires database state must use a
tested, matching backup rather than ad hoc volume deletion.

### Recovery after an interrupted repository reset

`dev-init` atomically advances `main`, `deploy`, and `refs/vonk/deploy-base`
before resetting the worktree. A host or container interruption in that narrow
interval can leave those refs at the accepted commit while checked-out files
remain old. The next start intentionally fails because the worktree is not
clean; it will not guess which files are safe to discard.

Recover only when the expected commit is known from the pinned Compose file or
its workflow artifact. Stop the project and enter the repository volume with
an operator shell. Before changing anything, verify that `HEAD` is `deploy`,
that `main`, `deploy`, and `refs/vonk/deploy-base` all resolve to that exact
40-character expected commit, that the worktree contains no operator edits to
preserve, and that the remote is the public repository. If any result is
ambiguous, restore the repository volume from its backup instead.

After recording those checks in the incident notes, explicitly run
`git reset --hard <expected-commit>` on `deploy`, then verify
`git status --porcelain=v1 --untracked-files=all` is empty. Restart the stack;
`dev-init` will recheck origin, refs, merge-base, and the exact artifact
commit. Do not move one ref alone, delete `.git`, or use this repository reset
as a substitute for restoring PostgreSQL or generated-secret state.

## Rotation and recovery

- To rotate the Git signing key, generate a replacement as a temporary file in
  `secrets/`, set `root:root 0400`, atomically rename it to `git-signing-key`,
  and redeploy. `dev-init` refreshes the API-only projection. Preserve the old
  public key wherever historical development signatures are verified.
- The PostgreSQL password and `database-url` are one credential pair. Never
  replace only one file. Back up the database, stage a new hexadecimal password
  and matching URL, change the existing PostgreSQL `control` role through a
  protected stdin-fed `psql` session, atomically replace both files, and
  immediately recreate the affected containers. If this coordinated procedure
  is unfamiliar, stop and restore rather than deleting the database volume.
- The generated admin private key and worker token are intentionally not
  operator files. A disposable full development reset can rotate them by
  removing their named volumes, but that invalidates related development state.
  Do not delete individual secret-projection volumes in a stateful installation
  without a tested recovery plan.

Back up the three host secret files and every named volume needed for continuity
to encrypted, access-controlled storage. The repository volume can be cloned
again from public GitHub, but local `deploy` history, `main`,
`refs/vonk/deploy-base`, other local refs, and signed changes exist only in its
backup until pushed. PostgreSQL is authoritative for development catalog data.
The API, migration, and worker secret projections contain generated private
authority and must be protected like the host secret files.

Never commit a backup, place it in the Compose directory, upload it as a GitHub
Actions artifact, or copy it into an image. Restores must keep the PostgreSQL
volume and matching password/URL pair from the same recovery point.

## Safe troubleshooting

- `pull access denied`: make both GHCR packages public; do not add a long-lived
  GitHub token to the NAS.
- `permission denied` under `/run/secrets`: repeat the NAS ownership/mode step,
  check for CRLF/BOM corruption, and redeploy so bind-backed secrets are
  recreated.
- PostgreSQL authentication failure after editing secrets: restore the matching
  password/URL pair and database state. Recreating containers does not change
  the password stored in an existing database volume.
- `dev-init` repository failure: inspect its status and the public GitHub
  connectivity. Do not bind-mount the SMB project as `/repository`; the live
  checkout belongs in the Compose-managed `dev-repository` volume discovered
  from the running container.
- API unreachable from another computer: the listener is intentionally
  loopback-only; use the SSH forwarding boundary above.
- Migration failure: preserve the PostgreSQL volume and diagnose the migration.
  Deleting data is not a migration repair.

When sharing diagnostics, include service state, exit codes, image digests, and
the accepted commit. Do not paste secret files, unredacted environment output,
or unrestricted service logs.
