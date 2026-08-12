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
    ├── agent-ca-certificate
    ├── agent-ca-key
    ├── agent-proxy-auth
    ├── controller-ca
    ├── controller-server-certificate
    ├── controller-server-key
    ├── database-url
    ├── git-signing-key
    ├── host-runtime-grant-private-key
    ├── litellm-master-key
    ├── litellm-upstream-key
    ├── management-cidrs
    ├── postgres-password
    └── token-signing-key
```

Generate and validate that complete bundle on local operator storage, then copy
it as one generation. Do not hand-assemble only the original database/signing
files: agent PKI and LiteLLM cannot start without the remaining files. Do not
put a checkout, an image archive, or a production secret beside them. The
Compose file is replaceable, while `secrets/` and Docker named volumes survive
normal redeploys. The file contains secret *paths*, never secret values.
No `current/`, source tree, Dockerfiles, or `.env` file belongs in this
project.

## Clean-machine prerequisites

Before copying anything to the NAS, start from a clean local staging
directory on an operator workstation:

- Download the accepted workflow artifact containing
  `docker-compose.dev.yml` and `docker-compose.pinned.yml`.
- Generate the complete runtime secret bundle locally with
  `scripts/dev-runtime-secrets.py`; let the helper validate names, certificate
  constraints, line endings, and key relationships before publication.
- Store an encrypted backup of the complete bundle in 1Password or another
  approved operator secret store, but never paste their contents into shell
  history, terminal output, screenshots, or ticket comments.
- Confirm the NAS can pull public GHCR images and that its management-LAN
  firewall will allow only the required agent ingress described below.

## Prepare management-LAN names and firewall

Do not assume local DNS. Add the same management-LAN names to `/etc/hosts` on
the NAS and on every GPU node that will pair to this controller:

```text
<NAS_MANAGEMENT_IP> <ENROLLMENT_HOSTNAME> <CONTROLLER_HOSTNAME> <REGISTRY_HOSTNAME>
```

Those names are generic inputs, not constants. They are later reused as the
agent `enrollment_url`, post-certificate `controller_url`, and registry host.
Operational commands in this repository intentionally use
`<NAS_MANAGEMENT_IP>` rather than a site-specific constant.

On the NAS firewall, allow the backend agent TLS listener only from the GPU
node management network, for example `<NODE_MANAGEMENT_CIDR>` to
`<NAS_MANAGEMENT_IP>:8443`. Keep human control, inference, Grafana, and Hermes
access on their separate trusted paths; do not widen the management listener
for convenience.

Record the direct-fabric CIDRs separately from the management CIDR. The
project publisher requires an explicit choice: pass the canonical
comma-separated NVIDIA Sync networks for a multi-node fleet, or the literal
`none` for an intentionally single-node installation. It rejects overlap,
duplicates, host-bit CIDRs, and an omitted choice. The rendered API and worker
receive only this public network policy; no node credential is added to
Compose.

The publication workflows expose three clearly named files:

| File | Published by | Graph and image reference |
|---|---|---|
| `docker-compose.dev.yml` | Accepted `main` workflow | Development graph with bare mutable `:dev` references. This is the normal NAS artifact. |
| `docker-compose.production.yml` | Signed release workflow | Full production graph selected only by the trusted host updater; use its production deployment bundle and [production secret guide](../../deploy/compose/README.md#required-secret-file-paths). |
| `docker-compose.pinned.yml` | Accepted `main` workflow | Immutable development references for explicit reproduction or state-aware recovery. |

For normal development, give `docker-compose.dev.yml` to
`scripts/dev-runtime-project`; it publishes that artifact as
`docker-compose.yml`. A moved tag does not change a
running project: after a successful publication, pull/redeploy the unchanged
`docker-compose.yml`, not restart containers and not replace the file. The
pinned file is deliberately an exception for
reproduction or recovery; this development guide never installs the production
graph or its much larger production credential set.
The publisher removes the template's local-development project name so Docker
Compose uses the project name selected by the NAS UI or the directory name.
This preserves the same named volumes when an operator later uses the CLI;
never add a second hard-coded `name:` field.

The operator-owned bundle has narrow service projections:

| File | Consumer | Purpose |
|---|---|---|
| `postgres-password` | PostgreSQL only | Password for the development `control` database role. |
| `database-url` | initializer, migration, then separate API/worker projections | Matching SQLAlchemy URL for that role. |
| `git-signing-key` | initializer, then API projection only | Unencrypted Ed25519 SSH private key for development Git signing. |
| `host-runtime-grant-private-key` | initializer, then API projection only | Signs short-lived, operation-bound grants for the root helper on enrolled GPU nodes. Its matching public key is installed on nodes and is not copied to the NAS. |
| `agent-ca-certificate`, `agent-ca-key`, `agent-proxy-auth` | initializer, then separated API/Caddy projections | Agent certificate issuance and authenticated proxy boundary. |
| `controller-ca`, `controller-server-certificate`, `controller-server-key` | operator backup plus Caddy projection | Server trust for the two agent hostnames. |
| `litellm-master-key`, `litellm-upstream-key` | initializer, then LiteLLM-only projection | User-facing development inference and internal upstream authentication. |
| `management-cidrs` | initializer, API, worker, and Caddy projections | Exact management networks allowed to use agent ingress. |
| `token-signing-key` | initializer, then API projection only | Random authority used to sign short-lived development administrator tokens. |

On first start, the networked `dev-repository-init` service fetches and verifies
the public repository without receiving any runtime secret. The separate
`network_mode: none` `dev-init` service generates an admin-grant private key and
a worker API token without mounting the repository. It projects authority into distinct API, migration, worker,
Caddy, and LiteLLM named volumes: API gets its signing and enrollment
authority; migration gets only the database URL; worker gets the database URL,
management CIDRs, and worker token; Caddy gets only TLS/proxy material; and
LiteLLM gets only its two keys. Those values
are not host files, image contents, CI inputs, Compose environment values, or
worker/API-shared authority.

## Obtain the normal development artifact

Open the successful `Development images` workflow run for the accepted `main`
commit. Download the artifact named
`vonk-forge-dev-compose-<40-character-commit>`. It contains
`docker-compose.dev.yml` and `docker-compose.pinned.yml`. Keep both on local
operator storage. Pass `docker-compose.dev.yml` to
`scripts/dev-runtime-project` as shown below; the publisher validates and writes
it as the NAS project's `docker-compose.yml`. Do not rename or copy either
artifact into the NAS project by hand. Do not edit the first-party image
references or add digests: the mutable `:dev` channel is intentionally selected
when the Docker UI pulls.

In the NAS file manager, confirm that the artifact is named
`docker-compose.yml` and that the project UI identifies it as a Compose file;
do not edit image references, add a digest, or add a build section. Both GHCR
packages must be public. A pull-only NAS needs no registry login. If the Docker
UI requests credentials, stop and correct package visibility instead of
installing a GitHub token on the NAS.

## Generate and copy the NAS secret bundle

Generate secrets on a private local Linux filesystem, never directly on SMB:

```bash
set -euo pipefail
install -d -m 0700 '<LOCAL_STAGING_DIRECTORY>'
scripts/dev-runtime-secrets.py \
  --secrets-dir '<LOCAL_STAGING_DIRECTORY>/secrets' \
  --management-cidrs '<NODE_MANAGEMENT_CIDR>' \
  --enroll-hostname '<ENROLLMENT_HOSTNAME>' \
  --agent-hostname '<CONTROLLER_HOSTNAME>' \
  --registry-hostname '<REGISTRY_HOSTNAME>'
```

The helper prints only the destination plus public certificate fingerprints
and expiry dates. It never prints secret values. It creates regular files with
mode `0600` in an operator-owned mode `0700` directory and refuses an
incomplete, unknown, symlinked, or inconsistent existing bundle.

If this exact directory is an otherwise valid original 15-file generation,
upgrade it in place once by repeating the command with
`--upgrade-host-runtime-authority`. This add-only migration preserves every
existing key, certificate, token, database credential, byte, and modification
time, and creates only `host-runtime-grant-private-key` and
`host-runtime-grant-public-key`. Back up the complete 17-file result before
publishing. Do not use this switch on a fresh directory or to repair any other
partial or inconsistent generation; the helper rejects those states.

Publish the accepted Compose and that exact bundle to the mounted NAS share:

```bash
scripts/dev-runtime-project \
  --source-compose '<DOWNLOAD_DIRECTORY>/docker-compose.dev.yml' \
  --secrets-dir '<LOCAL_STAGING_DIRECTORY>/secrets' \
  --destination '<MOUNTED_NAS_PARENT>/vonk-forge' \
  --nas-address '<NAS_MANAGEMENT_IP>' \
  --management-cidrs '<NODE_MANAGEMENT_CIDR>' \
  --direct-fabric-cidrs '<DIRECT_FABRIC_CIDRS_OR_NONE>' \
  --enroll-hostname '<ENROLLMENT_HOSTNAME>' \
  --agent-hostname '<CONTROLLER_HOSTNAME>' \
  --registry-hostname '<REGISTRY_HOSTNAME>'
```

This is the supported copy operation: it renders only the site hostnames,
the explicit direct-fabric policy, verifies every source and destination byte,
and permits only
`docker-compose.yml` plus `secrets/` at the destination. The helper takes a
nonblocking exclusive Linux file lock in a stable hidden sibling of the project
before inspecting or recovering a transaction. The lock remains outside the
two-item project and is reused by later publications. A live second publisher
is rejected without touching the active
journal; an unlocked file left by a dead process is safely reused. Publication
fails closed if the mounted SMB filesystem cannot provide that lock. The
helper keeps a private, hidden transaction journal while replacing files
because SMB directory rename is not a dependable atomic generation switch. It
restores the complete previous generation after an ordinary write failure. If
the workstation, mount, or process disappears, remount the same share and
rerun the same command: the helper verifies the stale journal, restores the
prior generation, and then safely retries publication.
After verifying a completed generation, it atomically retires the rollback
journal as `.vonk-forge-publish.cleanup` before deleting it. An interrupted
cleanup is therefore disposable and is removed on the next locked invocation
without being mistaken for rollback state. Do not manually delete, move,
inspect, or copy either hidden path; both can temporarily contain private
copies. A successful invocation removes them and leaves exactly the two visible
project items.

The content classes are:

| File | Exact content rule |
|---|---|
| `postgres-password` | 64 lowercase hexadecimal characters followed by one newline. |
| `database-url` | `postgresql+psycopg://control:<postgres-password>@postgres:5432/control` followed by one newline, where `<postgres-password>` is the exact value in `postgres-password`. |
| `git-signing-key` | One unencrypted Ed25519 OpenSSH private key followed by one newline; the initializer has no interactive passphrase input. |
| `host-runtime-grant-private-key` | One unencrypted Ed25519 PKCS#8 PEM private key; it never leaves the API projection or encrypted operator backup. |
| `*-certificate`, `controller-ca` | PEM certificates generated as one validated PKI generation for the configured hostnames. |
| `agent-ca-key`, `controller-server-key` | Matching unencrypted PEM private keys; never shared with a GPU node. |
| `agent-proxy-auth`, `litellm-master-key`, `litellm-upstream-key`, `token-signing-key` | Independent URL-safe random tokens followed by one newline. |
| `management-cidrs` | Canonical network CIDRs, one per line, followed by one newline. |

Do not overwrite existing secret files during a normal redeploy. If you copied
them through an SMB share or NAS file manager, safely eject/disconnect the
share after copying and use the NAS file manager to confirm only the 14
expected names appear.
Back up that exact host bundle before first start and after every rotation, but
confirm the backup by filename, size, and timestamp only; never reveal the
secret values during the check.

Do not change host files to container UIDs. The Docker daemon reads the
Compose file-backed secrets, and `dev-init` creates service-owned mode `0400`
projections inside separate named volumes. File sizes are safe to display.
Never use `cat`, `Get-Content`, or a screenshot that reveals values. Confirm
names, presence, and sizes through the NAS file manager; do not copy
configuration output into diagnostics.

## SMB/file-manager preparation

Use an SMB client only as the mounted destination of
`scripts/dev-runtime-project`; secret generation occurs on private local
storage. The SMB client must create regular files with the exact names and
content rules above; it must not leave a public key, temporary copy, or
duplicate filename in the project directory. If publication is interrupted,
leave its hidden journal untouched and rerun the publisher before importing or
redeploying the NAS project.

Obtain the unencrypted private key through the approved secret-management
process, then copy it as a regular file from protected local storage. SMB is a
copy path, not a secret-generation environment; its ordinary cleanup does not
guarantee secure erasure from snapshots or managed Windows storage.

Windows ACLs on an SMB drive do not establish Linux numeric container
ownership. Do not compensate with permissive public ACLs; the Docker daemon
needs read access and `dev-init` establishes the container-side identities.

## Restrict operator loopback forwarding

The acceptance runner needs the loopback-only API and inference listeners.
Keep them bound to `127.0.0.1` in Compose and permit one NAS operator to open
local forwards only to those two destinations. On an OpenSSH NAS, create
`/etc/ssh/sshd_config.d/00-vonk-operator-forwarding.conf` with:

```sshconfig
Match User <NAS_OPERATOR>
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:8080 127.0.0.1:4000
    AllowAgentForwarding no
    GatewayPorts no

Match all
```

The trailing `Match all` is required because the file is included by the main
server configuration. Validate both the syntax and the effective per-user
policy before reloading SSH:

```bash
sudo sshd -t
sudo sshd -T -C user='<NAS_OPERATOR>',host='<NAS_SSH_HOST>',addr='<OPERATOR_IP>' \
  | grep -E '^(allowtcpforwarding|allowagentforwarding|gatewayports|permitopen)'
sudo systemctl reload ssh
```

Require `allowtcpforwarding local`, `allowagentforwarding no`,
`gatewayports no`, and exactly the two `permitopen` destinations above. Test a
new SSH session before closing the current one. An appliance with a managed SSH
configuration must express the same restrictions through its supported UI;
never enable unrestricted forwarding or bind either application port to the
LAN.

## Create and redeploy the Compose project

In a generic NAS Docker UI (UGREEN calls this a Docker Project):

1. Create or import a project from the NAS-local `vonk-forge/` directory.
2. Select `docker-compose.yml`; retain its relative `./secrets/...` paths.
3. Verify that `secrets/` contains exactly the 14 names in the project tree
   above, without opening their contents in the UI.
4. Choose **Pull** then **Redeploy** for the project. Do not choose build or
   restart; there is no build context and restart cannot fetch a moved `:dev`
   image.
5. Keep every named volume. Do not choose a remove-volumes or clean-project
   option during normal development.

After the UI reports the deployment, follow the two prerequisite lanes in the
job and container status: the cohort reset, API and worker cohort reporters,
and cohort verifier complete in one lane while PostgreSQL becomes healthy in
the other. Only then may `dev-repository-init`, the offline `dev-init`, and
`migrate` complete, followed by the
long-running API and worker becoming healthy. A one-shot service that exits
successfully is complete, not failed. The API binds only to NAS loopback; use
the restricted SSH forwarding boundary below rather than widening the Compose
listener.

## Update after an accepted development publication

Keep `docker-compose.yml` unchanged. In the NAS Docker UI, open the existing
project and pull/redeploy it. Do not replace the Compose file, restart existing
containers, delete `secrets/`, or delete named volumes. The repository
volume has two deliberately separate branches: `main` is the accepted
origin-tracking baseline from public `origin/main`, while `deploy` is the
mutable runtime branch used for locally signed development changes.
`refs/vonk/deploy-base` is the exact merge-base between those branches. On each
start, `dev-repository-init` fetches public `origin/main`, verifies the artifact's exact
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

Download `docker-compose.pinned.yml` from the accepted workflow artifact for
the target cohort, verify its workflow commit and immutable image digests, and
replace `docker-compose.yml` with that exact pinned artifact before running the
procedure. Merely keeping the pinned file elsewhere does not select it. Record
the pinned 40-character commit as `expected_commit`; the checks below refuse a
mutable tag, a different pinned commit, or a target that is not in the current
repository history.

For a documented repository-only rollback, discover the actual volume name
from the running API before stopping the project. This remains correct if a NAS
UI changes the Compose project name. Replace `<NAS_PROJECT_DIRECTORY>` with the
absolute NAS-local directory selected by the Docker Project UI. This is a
site input: the validation below refuses a relative path, a symlinked project
root, a missing `docker-compose.yml` or `secrets/`, and unexpected top-level
project entries before it runs a Docker command:

```bash
set -euo pipefail
NAS_PROJECT_DIRECTORY='<NAS_PROJECT_DIRECTORY>'
case "$NAS_PROJECT_DIRECTORY" in
  /*) ;;
  *) echo 'NAS_PROJECT_DIRECTORY must be an absolute path' >&2; exit 1 ;;
esac
test -d "$NAS_PROJECT_DIRECTORY"
test ! -L "$NAS_PROJECT_DIRECTORY"
test -f "$NAS_PROJECT_DIRECTORY/docker-compose.yml"
test -d "$NAS_PROJECT_DIRECTORY/secrets"
mapfile -d '' -t unexpected_entries < <(
  find "$NAS_PROJECT_DIRECTORY" -mindepth 1 -maxdepth 1 \
    ! -name docker-compose.yml ! -name secrets -print0
)
test "${#unexpected_entries[@]}" -eq 0
cd -- "$NAS_PROJECT_DIRECTORY"
expected_commit=REPLACE_WITH_PINNED_40_CHARACTER_COMMIT
[[ "$expected_commit" =~ ^[0-9a-f]{40}$ ]]
mapfile -t selected_images < <(
  sudo docker compose -f docker-compose.yml config --images
)
selected_first_party=0
for image in "${selected_images[@]}"; do
  case "$image" in
    ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-$expected_commit@sha256:*|\
    ghcr.io/carstvaartjes/vonk-forge-worker:dev-sha-$expected_commit@sha256:*)
      [[ "$image" =~ @sha256:[0-9a-f]{64}$ ]]
      selected_first_party=$((selected_first_party + 1))
      ;;
    ghcr.io/carstvaartjes/vonk-forge-api:*|\
    ghcr.io/carstvaartjes/vonk-forge-worker:*)
      echo 'refusing a mutable or mismatched first-party image' >&2
      exit 1
      ;;
  esac
done
test "$selected_first_party" -eq 2
api_container=$(sudo docker compose -f docker-compose.yml ps -q control-api)
test -n "$api_container"
current_commit=$(sudo docker exec "$api_container" \
  git -C /repository rev-parse refs/heads/main)
[[ "$current_commit" =~ ^[0-9a-f]{40}$ ]]
test "$current_commit" != "$expected_commit"
sudo docker exec "$api_container" \
  git -C /repository cat-file -e "$expected_commit^{commit}"
sudo docker exec "$api_container" \
  git -C /repository merge-base --is-ancestor \
    "$expected_commit" "$current_commit"
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
The restart still uses the already-selected pinned `docker-compose.yml`; after
it becomes healthy, confirm the repository `main` ref equals
`expected_commit`. Do not use `down --volumes`. A rollback that requires
database state must use a tested, matching backup rather than ad hoc volume
deletion.

### Recovery after an interrupted repository reset

`dev-repository-init` atomically advances `main`, `deploy`, and
`refs/vonk/deploy-base`
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
`dev-repository-init` will recheck origin, refs, merge-base, and the exact artifact
commit. Do not move one ref alone, delete `.git`, or use this repository reset
as a substitute for restoring PostgreSQL or generated-secret state.

## Rotation and recovery

- To rotate the Git signing key, create a complete replacement local generation
  with the secret generator, retain the former public key wherever historical
  development signatures are verified, and publish the complete validated
  generation with `scripts/dev-runtime-project`. Never edit the active NAS
  `secrets/` directory file by file. The offline `dev-init` refreshes the
  API-only projection during redeploy.
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
- Rotate agent/controller PKI and LiteLLM/proxy tokens as one planned bundle
  generation with `scripts/dev-runtime-secrets.py` in a new private staging
  directory. Pairing identities and clients trust the existing generation, so
  a blind file-by-file replacement causes an outage. Follow the complete
  rotation window in [Development agent workloads](development-agent-workloads.md#rollback-and-secret-rotation).

Back up all 14 host secret files and every named volume needed for continuity
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
  validate the local bundle again, check the NAS Docker daemon's share access,
  and redeploy so service projections are recreated.
- PostgreSQL authentication failure after editing secrets: restore the matching
  password/URL pair and database state. Recreating containers does not change
  the password stored in an existing database volume.
- `dev-repository-init` failure: inspect its status and the public GitHub
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

Continue with the end-to-end [development agent workload acceptance
runbook](development-agent-workloads.md) after the stack is healthy.
