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
    ├── admin-password-verifier
    ├── database-url
    ├── git-signing-key
    ├── host-runtime-grant-private-key
    ├── litellm-master-key
    ├── litellm-upstream-key
    ├── management-cidrs
    ├── postgres-password
    ├── tailscale-oauth-client-id
    ├── tailscale-oauth-client-secret
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
- Create the scoped Tailscale OAuth client and save its ID and secret in two
  private mode `0600` local input files as described below. Never place either
  value in a command argument, terminal output, clipboard history, or Git.
- Store an encrypted backup of the complete bundle in 1Password or another
  approved operator secret store, but never paste their contents into shell
  history, terminal output, screenshots, or ticket comments.
- Confirm the NAS can pull public GHCR images and that its management-LAN
  firewall will allow only the required agent ingress described below.

## Prepare management-LAN names and firewall

Do not assume local DNS. Add the same management-LAN enrollment, agent, and
registry names to `/etc/hosts` on the NAS and on every GPU node that will pair
to this controller:

```text
<NAS_MANAGEMENT_IP> <ENROLLMENT_HOSTNAME> <CONTROLLER_HOSTNAME> <REGISTRY_HOSTNAME>
```

Those names are generic inputs, not constants. They are later reused as the
agent `enrollment_url`, post-certificate `controller_url`, and registry host.
Operational commands in this repository intentionally use
`<NAS_MANAGEMENT_IP>` rather than a site-specific constant.
Do not add these entries to the Windows operator machine, and never add the
Tailscale browser name to `/etc/hosts` or the Windows hosts file. Tailscale
provides the stable browser DNS name and trusted HTTPS certificate.

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
| `admin-password-verifier` | `dev-init`, then the authentication-initializer projection | Argon2id verifier for exact subject `admin`; the plaintext password is local-only and the API reads the resulting database row. |
| `postgres-password` | PostgreSQL only | Password for the development `control` database role. |
| `database-url` | initializer, migration, then separate API/worker projections | Matching SQLAlchemy URL for that role. |
| `git-signing-key` | initializer, then API projection only | Unencrypted Ed25519 SSH private key for development Git signing. |
| `host-runtime-grant-private-key` | initializer, then API projection only | Signs short-lived, operation-bound grants for the root helper on enrolled GPU nodes. Its matching public key is installed on nodes and is not copied to the NAS. |
| `agent-ca-certificate`, `agent-ca-key`, `agent-proxy-auth` | initializer, then separated API/Caddy projections | Agent certificate issuance and authenticated proxy boundary. |
| `controller-ca`, `controller-server-certificate`, `controller-server-key` | operator backup plus Caddy projection | Server trust for the two agent hostnames. |
| `litellm-master-key`, `litellm-upstream-key` | initializer, then LiteLLM-only projection | User-facing development inference and internal upstream authentication. |
| `management-cidrs` | initializer, API, worker, and Caddy projections | Exact management networks allowed to use agent ingress. |
| `token-signing-key` | initializer, then API projection only | Random authority used to sign short-lived development administrator tokens. |
| `tailscale-oauth-client-id`, `tailscale-oauth-client-secret` | initializer, then Tailscale-only projection | Scoped unattended gateway enrollment; no API, worker, Caddy, or GPU-node consumer. |

On first start, the networked `dev-repository-init` service fetches and verifies
the public repository without receiving any runtime secret. The separate
`network_mode: none` `dev-init` service generates an admin-grant private key and
a worker API token without mounting the repository. It projects authority into distinct API, migration, worker,
Caddy, and LiteLLM named volumes: API gets its signing and enrollment
authority; migration gets only the database URL; worker gets the database URL,
management CIDRs, and worker token; Caddy gets only TLS/proxy material; and
LiteLLM gets only its two keys. The authentication initializer gets only the
database URL and administrator verifier. The Tailscale gateway gets only its
OAuth pair and persistent node state. The worker cannot read the verifier,
session authority, OAuth pair, or Git signing key. Those values
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

## Prepare private Tailscale browser access

These are explicit operator actions in the Tailscale admin console. Tailnet
authorization controls who can reach Vonk Forge; the separate Vonk Forge
administrator login still controls application access.

1. Open **Tailscale admin console → DNS** and enable **MagicDNS**. Enable
   **HTTPS certificates** for the tailnet before configuring Serve; accept the
   certificate-domain acknowledgement shown by Tailscale.
2. Open **Services**, define exact Service `svc:vonk-forge`, and add endpoint
   `tcp:443`. Do this before grants or auto-approval so policy references a
   defined Service.
3. Open **Settings → Trust credentials → OAuth clients**, then create one OAuth
   client for this gateway.
4. Grant only `auth_keys` write scope (**Auth Keys — Write**) and restrict the
   client to its only tag, `tag:vonk-gateway`. Do not grant Devices, DNS,
   Policy, Key Value, or any other scope.
5. Merge the `tag:vonk-gateway`, `svc:vonk-forge`, grant, auto-approver, and
   test entries from
   [`deploy/compose/tailscale/grants.example.hujson`](../../deploy/compose/tailscale/grants.example.hujson)
   into the existing tailnet policy. Limit `svc:vonk-forge:443` to the exact
   administrator identity or administrator group. Never use `svc:*`, an
   allow-all grant, or a Funnel rule.
6. Save the newly shown client ID and secret immediately into two private
   local input files. Tailscale shows the secret only at creation time. This
   silent-input example keeps values out of command arguments and output:

   ```bash
   umask 077
   install -d -m 0700 '<LOCAL_OAUTH_INPUT_DIRECTORY>'
   IFS= read -r -s -p 'Tailscale OAuth client ID: ' oauth_client_id
   printf '\n'
   printf '%s' "$oauth_client_id" > '<LOCAL_OAUTH_INPUT_DIRECTORY>/client-id'
   unset oauth_client_id
   IFS= read -r -s -p 'Tailscale OAuth client secret: ' oauth_client_secret
   printf '\n'
   printf '%s' "$oauth_client_secret" > '<LOCAL_OAUTH_INPUT_DIRECTORY>/client-secret'
   unset oauth_client_secret
   chmod 0600 '<LOCAL_OAUTH_INPUT_DIRECTORY>/client-id' \
     '<LOCAL_OAUTH_INPUT_DIRECTORY>/client-secret'
   ```

   Do not substitute values into the command, echo them, or paste them into a
   transcript. The generator reads these mode `0600` files through
   `--tailscale-oauth-client-id-file` and
   `--tailscale-oauth-client-secret-file`; the publisher creates the matching
   NAS files without printing their contents.

The gateway reconciles one HTTPS-only Serve endpoint:
`svc:vonk-forge` HTTPS 443 to `http://caddy:8080`. Tailscale Funnel remains
disabled, Caddy's browser edge stays private to Docker, and there is no
human-facing LAN port.

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
  --registry-hostname '<REGISTRY_HOSTNAME>' \
  --tailscale-oauth-client-id-file '<LOCAL_OAUTH_INPUT_DIRECTORY>/client-id' \
  --tailscale-oauth-client-secret-file '<LOCAL_OAUTH_INPUT_DIRECTORY>/client-secret'
```

The helper prints only the destination plus public certificate fingerprints
and expiry dates. It never prints secret values. It creates regular files with
mode `0600` in an operator-owned mode `0700` directory and refuses an
incomplete, unknown, symlinked, or inconsistent existing bundle.

A fresh generation contains exactly 21 local source files. Before publication,
create a 1Password Password item named **Vonk Forge NAS Development
Administrator**, set its username to exact `admin`, and transfer the local
`admin-password` into its password field through the 1Password application.
Do not put the password in a CLI argument, terminal output, note field, or
browser storage. Keep the complete 21-file generation as an encrypted backup;
the 1Password item is the normal login copy, not a replacement for the
generation backup.

The publisher copies exactly 17 files into the NAS `secrets/` directory. The
four local-only files are `admin-password`, `controller-ca-key`,
`git-signing-key.pub`, and `host-runtime-grant-public-key`. In particular, the
plaintext administrator password is never published to the NAS. The publisher
and `dev-init` then enforce the disjoint service ownership boundaries described
above; a read-only mount is not used as a substitute for separating secrets.

For an existing valid pre-browser 17-file local source generation, repeat the
same command with `--upgrade-browser-access`. This add-only operation preserves
every existing secret byte and key relationship and creates only
`admin-password`, `admin-password-verifier`, `tailscale-oauth-client-id`, and
`tailscale-oauth-client-secret`. Back up the resulting 21-file generation
before publication. If the source is the older valid 15-file generation, first
run the separately documented `--upgrade-host-runtime-authority` transition,
then run `--upgrade-browser-access`. Neither switch repairs partial, unknown,
symlinked, or inconsistent state.

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
| `admin-password` | Local-only 43-character unpadded base64url administrator password; store it in the named 1Password item and encrypted backup. |
| `admin-password-verifier` | Argon2id PHC verifier published to the authentication projection; it is not a login credential. |
| `git-signing-key` | One unencrypted Ed25519 OpenSSH private key followed by one newline; the initializer has no interactive passphrase input. |
| `host-runtime-grant-private-key` | One unencrypted Ed25519 PKCS#8 PEM private key; it never leaves the API projection or encrypted operator backup. |
| `*-certificate`, `controller-ca` | PEM certificates generated as one validated PKI generation for the configured hostnames. |
| `agent-ca-key`, `controller-server-key` | Matching unencrypted PEM private keys; never shared with a GPU node. |
| `agent-proxy-auth`, `litellm-master-key`, `litellm-upstream-key`, `token-signing-key` | Independent URL-safe random tokens followed by one newline. |
| `tailscale-oauth-client-id`, `tailscale-oauth-client-secret` | Exact validated bytes from the two private OAuth input files; published only to the Tailscale projection. |
| `management-cidrs` | Canonical network CIDRs, one per line, followed by one newline. |

Do not overwrite existing secret files during a normal redeploy. If you copied
them through an SMB share or NAS file manager, safely eject/disconnect the
share after copying and use the NAS file manager to confirm only the 17
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

## Restrict acceptance and break-glass loopback forwarding

This boundary is for bounded acceptance and break-glass recovery only; it is
not the normal browser path. The acceptance runner needs the loopback-only API
and inference listeners.
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
3. Verify that `secrets/` contains exactly the 17 names in the project tree
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
`migrate` complete. The isolated `dev-auth-init` then installs the exact
administrator verifier before the long-running API and worker can become
healthy. A one-shot service that exits
successfully is complete, not failed. The API remains bound to NAS loopback for
acceptance and recovery; normal operator access uses the private Tailscale
Service below.

## Open the stable browser URL

In the NAS Docker UI, open the `tailscale-configurator` logs and find its
non-secret line:

```text
Vonk Forge browser URL: https://vonk-forge.<TAILNET_NAME>.ts.net/
```

That is the stable Tailscale Service URL. Do not invent the suffix and do not
use a container's transient node name or `100.x` address. Confirm the
configurator is healthy and reports the exact HTTPS-only `svc:vonk-forge` map.
An authorized Windows operator with Tailscale connected can open
`https://vonk-forge.<TAILNET_NAME>.ts.net/` directly; no Windows hosts entry,
SSH session, PowerShell forwarding process, bearer token, or TLS exception is
part of this normal route.

Log in as exact subject `admin` using the password from the 1Password item
**Vonk Forge NAS Development Administrator**. Tailnet access alone never logs a
user into the application. After login, verify the Development marker, the
`admin` / `administrator` identity, and the expected fleet. Use **Logout** and
confirm the login page returns; logout revokes the server-side browser session.

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

After every successful Pull then Redeploy, use the same stable Tailscale
Service URL, log in, verify the Development marker and expected fleet, and use
Logout when finished. A changing container or gateway node identity does not
change the Service URL.

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

- To rotate or recover the development administrator password, use the same
  complete local generation and OAuth file inputs with
  `--rotate-admin-password`. The operation changes only `admin-password` and
  `admin-password-verifier`. Update the **Vonk Forge NAS Development
  Administrator** 1Password item and encrypted backup, republish the complete
  project, then choose **Pull** then **Redeploy** while preserving every named
  volume. Authentication initialization installs the changed verifier and
  revokes every existing browser session. Never edit the verifier on the NAS
  or create a default password.
- Preserve the `dev-tailscale-state` named volume during every normal update.
  If Tailscale state is lost, the scoped OAuth client may create one replacement
  tagged gateway. Verify exactly one current gateway advertises
  `svc:vonk-forge`, revoke any orphan, and recheck the exact HTTPS-only Serve
  map. Application data and browser session rows remain separate from
  Tailscale state.
- For OAuth compromise, revoke the OAuth client in **Trust credentials**, revoke
  the affected gateway node and Service approval, and create a replacement
  client with the same narrow scope. Capture both replacement values in new
  private mode-`0600` input files, then rerun the exact complete generator
  command from this runbook with those two inputs and
  `--rotate-tailscale-oauth`. Generate one non-secret UUIDv4 rotation ID,
  record it with the replacement OAuth item, and pass it as
  `--tailscale-oauth-rotation-id <uuid>` on the first attempt and every retry.
  The generator keeps a mode-`0600` hash-only receipt beside the 21-file
  generation; include that receipt and the rotation ID in the encrypted source
  backup. The locked transaction validates the existing
  generation, changes exactly `tailscale-oauth-client-id` and
  `tailscale-oauth-client-secret`, preserves every other file byte-for-byte
  with its mode and ownership, and rolls back an interrupted install before a
  retry. A retry with the same UUID is idempotent even after final transaction
  cleanup; a different UUID cannot authorize an unchanged pair. Back up the
  completed 21-file generation and its receipt, republish it with
  `scripts/dev-runtime-project`, then choose **Pull** then **Redeploy** while
  preserving every named volume. Rotate the
  administrator password and revoke browser sessions separately if application
  authority may also have been exposed.
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

Back up all 17 host secret files and every named volume needed for continuity
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
- Browser URL unavailable: inspect the `tailscale-gateway` and
  `tailscale-configurator` health/logs, verify the OAuth client remains active,
  confirm `tag:vonk-gateway` may advertise `svc:vonk-forge`, and compare the
  exact HTTPS-only Serve map. Do not add a LAN port or enable Funnel.
- Browser returns 401 after successful prior use: sign in again. If the
  password was rotated, use the current 1Password item; do not reuse an old
  session cookie or expose the verifier in diagnostics.
- Loopback API unavailable during bounded acceptance or recovery: verify the
  NAS-local listener and the restricted SSH forwarding boundary above. This is
  not a substitute for repairing the normal Tailscale browser route.
- Migration failure: preserve the PostgreSQL volume and diagnose the migration.
  Deleting data is not a migration repair.

When sharing diagnostics, include service state, exit codes, image digests, and
the accepted commit. Do not paste secret files, unredacted environment output,
or unrestricted service logs.

Continue with the end-to-end [development agent workload acceptance
runbook](development-agent-workloads.md) after the stack is healthy.
