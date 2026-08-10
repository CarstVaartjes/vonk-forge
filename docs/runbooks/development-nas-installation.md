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

Before copying it to the NAS, check only non-secret properties:

```bash
grep -E 'ghcr.io/carstvaartjes/vonk-forge-(api|worker):dev$' docker-compose.yml
test "$(grep -c '^[[:space:]]*build:' docker-compose.yml)" -eq 0
```

Both GHCR packages must be public. A pull-only NAS needs no registry login. If
a pull requests credentials, stop and correct the package visibility instead
of installing a GitHub token on the NAS.

## Preferred: generate secrets directly on the NAS

SSH to the NAS and set `project_dir` to its local filesystem path, not an SMB
UNC path. The example path is intentionally generic; obtain the real project
path from the NAS storage UI.

```bash
project_dir=/volume1/docker/vonk-forge
sudo install -d -m 0700 -o root -g root "$project_dir/secrets"
sudo sh -s -- "$project_dir" <<'SH'
set -eu
umask 077
secret_dir=$1/secrets
for name in postgres-password database-url git-signing-key git-signing-key.pub; do
  if [ -e "$secret_dir/$name" ] || [ -L "$secret_dir/$name" ]; then
    printf 'refusing to overwrite %s\n' "$secret_dir/$name" >&2
    exit 1
  fi
done
postgres_password=$(openssl rand -hex 32)
printf '%s\n' "$postgres_password" > "$secret_dir/postgres-password"
printf 'postgresql+psycopg://control:%s@postgres:5432/control\n' \
  "$postgres_password" > "$secret_dir/database-url"
ssh-keygen -q -t ed25519 -N '' \
  -C vonk-forge-development-git-signing \
  -f "$secret_dir/git-signing-key"
rm -f -- "$secret_dir/git-signing-key.pub"
chown 999:999 "$secret_dir/postgres-password"
chown 10001:10001 "$secret_dir/database-url"
chown root:root "$secret_dir/git-signing-key"
chmod 0400 "$secret_dir/postgres-password" \
  "$secret_dir/database-url" "$secret_dir/git-signing-key"
unset postgres_password
SH
```

The generated password is hexadecimal, so it can be placed in the URL without
percent encoding. Do not substitute a password containing URL punctuation
unless it is percent-encoded correctly. The Git key must have no passphrase:
the noninteractive initializer has no agent or passphrase input.

The numeric owners match the pinned images: PostgreSQL is UID/GID `999`, while
the API and migration run as `10001:10001`. Docker implementations differ in
how bind-backed Compose secrets expose ownership; these owners and mode `0400`
are the restrictive compatible host settings. The Docker daemon must be able
to traverse the root-owned `0700` secret directory.

Validate existence, size, ownership, and mode without printing content:

```bash
project_dir=/volume1/docker/vonk-forge
for name in postgres-password database-url git-signing-key; do
  sudo test -f "$project_dir/secrets/$name"
  sudo test -s "$project_dir/secrets/$name"
  sudo stat -c '%n uid=%u gid=%g mode=%a bytes=%s' \
    "$project_dir/secrets/$name"
done
```

Expected owners are `999:999`, `10001:10001`, and `0:0`, respectively; every
mode is `400`. File sizes are safe to display. Never use `cat`, `Get-Content`,
or a screenshot that reveals values. `docker compose config` prints file paths
and operational configuration, not these files' contents; use `config -q` when
you only need validation so that metadata is not copied into diagnostics.

## Alternative: create files from Windows and copy over SMB

Direct NAS generation is preferable because the private key never crosses the
network. If only the SMB share is initially available, this PowerShell flow
creates UTF-8 files without a BOM and does not put the generated password in
command history. Replace `Z:\vonk-forge` if the share uses another drive.

```powershell
$ErrorActionPreference = 'Stop'
$project = 'Z:\vonk-forge'
$secretDir = Join-Path $project 'secrets'
New-Item -ItemType Directory -Force -Path $secretDir | Out-Null
$secretNames = 'postgres-password', 'database-url', 'git-signing-key'
foreach ($name in $secretNames) {
  $destination = Join-Path $secretDir $name
  if (Test-Path -LiteralPath $destination) {
    throw "Refusing to overwrite $destination"
  }
}

$bytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$rng.Dispose()
$postgresPassword = -join ($bytes | ForEach-Object { $_.ToString('x2') })
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText(
  (Join-Path $secretDir 'postgres-password'),
  "$postgresPassword`n",
  $utf8NoBom
)
[IO.File]::WriteAllText(
  (Join-Path $secretDir 'database-url'),
  "postgresql+psycopg://control:${postgresPassword}@postgres:5432/control`n",
  $utf8NoBom
)
$postgresPassword = $null
[Array]::Clear($bytes, 0, $bytes.Length)

$key = Join-Path $secretDir 'git-signing-key'
$keyTempDir = Join-Path ([IO.Path]::GetTempPath()) `
  ("vonk-forge-key-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $keyTempDir | Out-Null
$tempKey = Join-Path $keyTempDir 'git-signing-key'
$stagedKey = Join-Path $secretDir `
  ('.git-signing-key.' + [Guid]::NewGuid().ToString('N') + '.tmp')
try {
  & "$env:WINDIR\System32\OpenSSH\ssh-keygen.exe" `
    -q -t ed25519 -C vonk-forge-development-git-signing -f $tempKey
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $tempKey)) {
    throw "ssh-keygen failed with exit code $LASTEXITCODE"
  }
  Copy-Item -LiteralPath $tempKey -Destination $stagedKey -ErrorAction Stop
  if (-not (Test-Path -LiteralPath $stagedKey) -or
      (Get-Item -LiteralPath $stagedKey).Length -ne
      (Get-Item -LiteralPath $tempKey).Length) {
    throw 'SMB private-key copy did not verify'
  }
  [IO.File]::Move($stagedKey, $key)
} finally {
  if (Test-Path -LiteralPath $stagedKey) {
    Remove-Item -LiteralPath $stagedKey -Force -ErrorAction Stop
  }
  Remove-Item -LiteralPath $keyTempDir -Recurse -Force -ErrorAction Stop
}
```

`ssh-keygen` prompts twice for a passphrase; press Enter at both prompts so the
runtime key is unencrypted. Do not pass an empty `-N` through Windows
PowerShell 5: its native argument handling is inconsistent and caused the
earlier key-generation failures. The key is generated on the protected local
temporary filesystem and only then copied as a regular file; `ssh-keygen` must
not write directly to SMB. Prompt deletion of the temporary directory reduces
exposure, but it is not guaranteed secure erasure on SSDs, snapshots, or
managed Windows storage.

Windows ACLs on an SMB drive do not establish the Linux numeric ownership used
inside containers. After copying, run this mandatory NAS-shell step:

```bash
project_dir=/volume1/docker/vonk-forge
sudo chown root:root "$project_dir/secrets"
sudo chmod 0700 "$project_dir/secrets"
sudo chown 999:999 "$project_dir/secrets/postgres-password"
sudo chown 10001:10001 "$project_dir/secrets/database-url"
sudo chown root:root "$project_dir/secrets/git-signing-key"
sudo chmod 0400 "$project_dir/secrets/postgres-password" \
  "$project_dir/secrets/database-url" "$project_dir/secrets/git-signing-key"
```

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

After the UI reports the deployment, inspect the job and container status in
this order: the API and worker cohort reporters, the cohort gate, `dev-init`,
`migrate`, then PostgreSQL and the long-running API and worker. The reporters,
gate, `dev-init`, and `migrate` must complete successfully before the
long-running services become healthy. The API binds only to NAS loopback. From
Windows, use a trusted SSH forward rather than widening the Compose listener:

```powershell
ssh.exe -L 8080:127.0.0.1:8080 your-nas-account@your-nas-host
```

Then open `http://127.0.0.1:8080` on Windows while the SSH session remains
connected.

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
