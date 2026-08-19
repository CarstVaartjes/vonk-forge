# Fresh development installation

This is the shortest supported path from an empty NAS project and clean
Ubuntu 24.04 ARM64 GPU nodes to a working Vonk Forge development fleet. It
uses public `:dev` control images and the signed APT `dev` channel produced
from accepted `main` commits. Production uses the separate trusted host
updater and is not installed with this guide.

The finished NAS directory contains only `docker-compose.yaml` and `secrets/`.
GPU nodes receive only the public controller CA and their own locally generated
identity. No GitHub, GHCR, R2, database, signing, or model credential is copied
to a GPU node or baked into an image.
The normal operator endpoint is the stable private Tailscale HTTPS Service URL
for `svc:vonk-forge`; normal browser access does not require an SSH tunnel,
PowerShell forwarding process, bearer token, Windows hosts-file entry, or LAN
browser port.
There is no Windows hosts-file entry for the Tailscale Service.

The optional NAS acceptance tunnel does not bypass route authority:
`127.0.0.1:4000` terminates at Caddy's lease-gated internal `:8081` listener;
no LiteLLM port is published to the host. Caddy evaluates the current
route-serving lease at request admission. A request whose Caddy authorization
begins at or after lease expiry is never forwarded to LiteLLM. If the
supervisor authority is unavailable, Caddy fails closed without contacting
LiteLLM. A same-config lease renewal replaces the deadline without restarting
the healthy LiteLLM child.

A fresh pre-production reset removes every user, browser session, and agent
enrollment. Recreate the development administrator, sign in to establish a
fresh browser session, and re-enroll every Spark before acceptance. No login,
session cookie, pairing token, or enrollment from before the reset remains
valid.

Plan local NVMe capacity for images and model artifacts separately. The
qualified DS4 wrapper image is about 2.59 GB. Its immutable base and drafter
model files are separate cache objects of 86,720,111,488 and 6,971,241,504
bytes respectively (93,691,352,992 bytes total). Updating or rebuilding the
wrapper does not bake those files into the image and does not redownload an
already verified cache object. At runtime, the verified cache objects are
mounted read-only into the container.

NVIDIA Sync owns supported cluster networking and node-to-node SSH. DGX
Dashboard owns DGX OS, firmware, kernel, driver, Docker, and NVIDIA Toolkit
updates. This fresh-install path validates those platform prerequisites but
does not replace or reconfigure them. Do not run the archived SSH-controller
node policy, `disable-earlyoom`, runtime-release, or manual Netplan workflows
as part of a fresh install.

## 1. Record the site values

Choose these values once and use them consistently:

```text
NAS address:            <NAS_MANAGEMENT_IP>
GPU-node network:       <NODE_MANAGEMENT_CIDR>
Direct-fabric networks: <DIRECT_FABRIC_CIDRS_OR_NONE>
Enrollment name:        <ENROLLMENT_HOSTNAME>
Agent controller name:  <CONTROLLER_HOSTNAME>
Reserved registry name: <REGISTRY_HOSTNAME>
Tailnet administrator:   <TAILNET_ADMIN_IDENTITY>
```

Each GPU node also needs a unique identity generated with:

```bash
printf 'spk_%s\n' "$(openssl rand -hex 16)"
```

Never reuse a node identity. The examples in this repository use
`enroll.vonk-forge.lan`, `agents.vonk-forge.lan`, and
`registry.vonk-forge.lan`, but those names are not product defaults.
For a multi-node fleet, record the canonical comma-separated CIDRs configured
by NVIDIA Sync, for example `192.168.100.0/24,192.168.101.0/24`. They must not
overlap the management network. For an intentionally single-node installation,
record the literal `none` and omit both fabric fields from the agent config.

## 2. Download the accepted Compose artifact

Open the successful **Development images** GitHub Actions run for the accepted
`main` commit and download
`vonk-forge-dev-compose-<40-character-commit>`. Keep both files from the
artifact:

- retain `docker-compose.dev.yml` as the publisher input; the publisher writes
  it to the NAS as `docker-compose.yaml`;
- retain `docker-compose.pinned.yml` off the NAS for exact reproduction or
  guarded recovery.

Both GHCR packages must pull anonymously. Do not install a registry token on
the NAS. Normal updates pull/redeploy the unchanged `docker-compose.yaml`;
restarting containers alone does not fetch a moved `:dev` tag.

## 3. Generate and publish the NAS project

First complete the exact numbered Tailscale console and safe OAuth-file steps
in [Prepare private Tailscale browser access](development-nas-installation.md#prepare-private-tailscale-browser-access).
In order: enable **MagicDNS** and **HTTPS certificates**; use
**Services → Advertise → Define a Service** to define `vonk-forge` (resulting
identifier `svc:vonk-forge`) with endpoint `tcp:443`; use
**Trust credentials → Credential → OAuth** to create the machine OAuth client;
grant only `auth_keys` write to `tag:vonk-gateway`; then merge the exact
Service grant, service auto-approval, and policy tests under
**Access controls**. Leave Funnel disabled and do not add a LAN browser port.
Save
the ID and secret only in the two mode `0600` local input files; never put
their values in command arguments or output.

Generate secrets on a private Linux filesystem, not directly on SMB:
Run these commands from the repository checkout with `uv` installed; the
locked control environment supplies the cryptography and Argon2 dependencies.

```bash
set -euo pipefail
install -d -m 0700 '<LOCAL_STAGING_DIRECTORY>'
uv run --project control --frozen scripts/dev-runtime-secrets.py \
  --secrets-dir '<LOCAL_STAGING_DIRECTORY>/secrets' \
  --management-cidrs '<NODE_MANAGEMENT_CIDR>' \
  --enroll-hostname '<ENROLLMENT_HOSTNAME>' \
  --agent-hostname '<CONTROLLER_HOSTNAME>' \
  --registry-hostname '<REGISTRY_HOSTNAME>' \
  --tailscale-oauth-client-id-file '<LOCAL_OAUTH_INPUT_DIRECTORY>/client-id' \
  --tailscale-oauth-client-secret-file '<LOCAL_OAUTH_INPUT_DIRECTORY>/client-secret'
```

Use the generic remote publisher for a NAS reached from Windows or WSL. This is
the recommended Windows/WSL path because publication occurs on the NAS's real
Linux filesystem, not through the `Z:`/SMB client view:

```bash
uv run --project control --frozen scripts/dev-runtime-project-remote \
  --source-compose '<DOWNLOAD_DIRECTORY>/docker-compose.dev.yml' \
  --secrets-dir '<LOCAL_STAGING_DIRECTORY>/secrets' \
  --ssh-target '<NAS_SSH_TARGET>' \
  --identity-file '<ABSOLUTE_SSH_IDENTITY_FILE>' \
  --remote-destination '<NAS_LINUX_DOCKER_PARENT>/vonk-forge' \
  --docker-mode sudo \
  --nas-address '<NAS_MANAGEMENT_IP>' \
  --management-cidrs '<NODE_MANAGEMENT_CIDR>' \
  --direct-fabric-cidrs '<DIRECT_FABRIC_CIDRS_OR_NONE>' \
  --enroll-hostname '<ENROLLMENT_HOSTNAME>' \
  --agent-hostname '<CONTROLLER_HOSTNAME>' \
  --registry-hostname '<REGISTRY_HOSTNAME>'
```

The remote operator must already have strict SSH host-key trust and either
direct Docker access (`--docker-mode direct`) or non-interactive Docker access
through `sudo -n` (`--docker-mode sudo`). Docker access is root-equivalent; do
not grant `NOPASSWD: ALL`. The helper uses batch-mode SSH, pulls the public
accepted API image anonymously, stages inputs RAM-only under NAS tmpfs, and
runs the existing publisher without network, capabilities, a writable root, or
additional host mounts. It removes the exact tmpfs stage after success, error,
interrupt, or SSH disconnect. It does not clone this repository onto the NAS.

After success, the Windows share or NAS file manager must show only
`docker-compose.yaml` plus `secrets/` inside `vonk-forge/`. Keep
`docker-compose.pinned.yml` and the complete 22-file source generation off the
NAS project.

An advanced Linux operator may instead give `docker-compose.dev.yml` to
`scripts/dev-runtime-project` with a directly mounted absolute destination.
That path is supported only when the mount itself provides the required POSIX
ownership, `fchmod`, local staging, and exclusive `flock`. WSL `9p`, DrvFs,
CIFS, and many SMB mounts do not. A failure there is a security result: do not
weaken modes, locking, or publisher checks; use the remote command above.

Supply the raw OAuth client secret exactly as issued. The generated Compose
derives the required non-ephemeral enrollment credential only in the
Tailscale gateway's tmpfs; neither the local secret generation nor the NAS
project contains a second plaintext credential file.

Back up exactly 22 local source files as one encrypted generation. Create a
1Password Password item named **Vonk Forge NAS Development Administrator**,
set its username to exact `admin`, and store the local `admin-password` there
without placing it in a command argument or terminal output. The publisher
copies exactly 18 files to the NAS. The four local-only files are
`admin-password`, `controller-ca-key`, `git-signing-key.pub`, and
`host-runtime-grant-public-key`; the plaintext administrator password is never
published to the NAS. Do not display secret contents while checking the
result. The underlying publisher takes a nonblocking Linux file lock on the
NAS filesystem and rejects a concurrent invocation; it fails closed if locking
is unavailable. If a publication, SSH connection, or workstation is interrupted, do not
edit the destination or delete the hidden `.vonk-forge-publish` recovery
journal or `.vonk-forge-publish.cleanup` tombstone. Rerun the same command;
under the exclusive lock, a stale rollback journal is
restored and a stale cleanup tombstone is safely discarded before publication
retries. A stable hidden lock file beside (never inside) the project coordinates
publishers across workstations and is safe to retain. A successful run removes
both hidden transaction states from the project and leaves exactly
`docker-compose.yaml` plus `secrets/`.

This is a fresh-install bundle. If an older or incomplete secret directory is
present, preserve any required evidence separately and generate a new bundle
in a new directory; the clean-slate generator does not upgrade old layouts.

## 4. Configure names and start the NAS stack

Add this line to `/etc/hosts` on the NAS and every GPU node for the enrollment,
agent, and registry names:

```text
<NAS_MANAGEMENT_IP> <ENROLLMENT_HOSTNAME> <CONTROLLER_HOSTNAME> <REGISTRY_HOSTNAME>
```

The operator must never add the Tailscale browser name to `/etc/hosts` or the
Windows hosts file.
Tailscale supplies its DNS name and trusted HTTPS certificate.

Allow the GPU-node management CIDR to reach NAS TCP 8443 and reject other
sources. In the NAS Docker/Compose UI:

1. Import the `vonk-forge/` directory as a project.
2. Select `docker-compose.yaml` and choose **Pull**, then **Redeploy**.
3. Keep all named volumes.
4. Wait for PostgreSQL, API, worker, Caddy, and LiteLLM to become healthy.

One-shot cohort, initializer, and migration containers should exit with status
zero. They are completed prerequisites, not failed services. See
[Development NAS installation](development-nas-installation.md) if startup
does not reach healthy state. Do not expose ports 8080 or 4000 on the LAN.

Open the `tailscale-configurator` logs, copy only the reported stable private
Tailscale HTTPS Service URL (`https://vonk-forge.<TAILNET_NAME>.ts.net/`), and
open it in a Tailscale-connected browser. Log in as exact subject `admin` with
the password from **Vonk Forge NAS Development Administrator**. Confirm the
Development marker, then use this authenticated browser for the pairing steps
below. Tailnet membership is only the reachability gate; it does not replace
the application login.

Before selecting a model, clone the public standard recipe library beside the
platform checkout on the administrator workstation and preview its exact
contents:

```bash
git clone https://github.com/CarstVaartjes/vonk-forge-recipes ../vonk-forge-recipes
cd vonk-forge
./scripts/validate-recipe-library \
  --library-root ../vonk-forge-recipes \
  --platform-root . \
  --json
./scripts/import-recipe-library \
  --library-root ../vonk-forge-recipes \
  --platform-root .
```

The preview imports only accepted recipes and records the exact library commit
when applied. Candidate recipes require an explicit opt-in; the library
checkout is never copied to the NAS or mounted into a workload. See the
[standard recipe library](../operators/recipe-library.md) guide for candidate
qualification and production release-tag rules.

## 5. Install and configure each GPU node

On every Ubuntu 24.04 ARM64 node:

1. Complete the verified one-time
   [APT `dev` channel setup](../operations/agent-package-release.md#install-the-dev-channel).
2. Run the installation guide's NVIDIA Docker/CDI preflight, then
   `sudo apt update && sudo apt install vonk-forge-agent`.
   Require `Linger=yes`, the `vonk-agent` user bus, and rootless Podman with the
   systemd cgroup manager exactly as shown in that guide before pairing.
3. Registration is the authority. In Fleet, **Add Spark** records the
   node-bound bootstrap grant and defines the runtime inputs for that Spark.
   Copy only the public CA and `host-runtime-grant-public-key`; manual
   `agent.toml` editing is unsupported. Run the displayed
   `/var/lib/vonk-forge/supervisor/current/vonk-agent bootstrap` command exactly
   as issued, approve the pending evidence, then run the same command again to
   collect the issued identity.
4. Create the root-owned `docker-firewall.conf`, including every accepted
   bridge-published and host-network endpoint port, then enable the signed
   `vonk-forge-docker-firewall.service` before the package-helper socket. It
   installs persistent Docker-aware `DOCKER-USER` policy for every accepted
   original published host port, host-network endpoint, and peer-only fabric
   flow. UFW `INPUT` policy does not protect Docker-published ports, while
   host-network workloads require an explicit `INPUT` policy; use the exact
   contract from the workload runbook and run the packaged check after Docker
   restart and host reboot.

Use the exact installation commands and reviewed registration contract in
[Install the Vonk Forge agent](../operations/install-vonk-agent.md). Do not add
the agent account to Docker, sudo, or an NVIDIA administration group.

After the generic synthetic and DS4 acceptance slices pass, use the dedicated
[MIA DeepSeek V4 Flash runbook](mia-deepseek-v4-flash.md) for the current
source-first two-Spark tensor-parallel workload.

## 6. Pair and start one node at a time

For each node, complete this order without reusing its one-use grant:

1. Create the node-bound registration intent in Fleet through **Add Spark**.
2. Run the exact Fleet-issued
   `/var/lib/vonk-forge/supervisor/current/vonk-agent bootstrap` command. It
   uses `/etc/vonk-forge-agent/agent.toml`, `/var/lib/vonk-forge-agent`, and
   `/etc/vonk-forge-agent/controller-ca.pem`; do not add path flags or
   hand-author the TOML.
3. Review the pending approval evidence in Fleet and approve only the matching
   node.
4. Run the same command once more to collect the issued identity, then start
   the supervisor.

The controller must show the same certificate-bound node ID, Rust protocol 3,
and fresh inventory. Repeat for each additional node.
If a resolved controller outage left the node at `start-limit-hit`, use the
installation guide's bounded
[start-limit recovery](../operations/install-vonk-agent.md#rotation-recovery-and-removal);
do not re-pair the node or delete its state.

If this installation is replacing any earlier prototype or recipe-domain
state, do not continue with a retained database or selected volumes. Run the
development-only procedure in
[Clean development reset](../operators/execution-harnesses.md#clean-development-reset).
That procedure removes every control volume and verifies exact fresh head
`0001_fleet_library_baseline`; it has no compatibility or prototype import
path. Afterward, the initializer recreates administrator subject `admin` from
the retained verifier, but the operator must establish a fresh browser session
and repeat the reviewed registration and approval flow for every Spark. Use
new acceptance paths. A pre-reset login, session, pairing token, enrollment,
route, or evidence file is never proof for the fresh installation. Spark-local
caches may survive only as untrusted candidates until their exact content
digests are verified again.

## 7. Prove the installation

The commands below are deterministic acceptance, not normal browser access.
Create a private admin token, configure the guide's
[restricted acceptance and break-glass loopback forwarding](development-nas-installation.md#restrict-acceptance-and-break-glass-loopback-forwarding),
forward the NAS loopback API and inference ports, and run the native-v1
synthetic public-API lifecycle:

```bash
install -d -m 0700 .state/development-acceptance
scripts/dev-admin-token \
  --output .state/development-acceptance/admin-token \
  --signing-key-file '<LOCAL_STAGING_DIRECTORY>/secrets/token-signing-key' \
  --ttl-seconds 21600

# Keep this tunnel open in another terminal.
ssh -N \
  -L 18080:127.0.0.1:8080 \
  -L 14000:127.0.0.1:4000 \
  '<NAS_SSH_TARGET>'

scripts/run-development-slices \
  --api-base http://127.0.0.1:18080 \
  --inference-base http://127.0.0.1:14000 \
  --admin-token-file .state/development-acceptance/admin-token \
  --inference-token-file '<LOCAL_STAGING_DIRECTORY>/secrets/litellm-master-key' \
  --phase synthetic \
  --builder-node '<NODE_ID>' \
  --target-node '<NODE_ID>' \
  --evidence-file .state/development-acceptance/synthetic.json
```

Success proves source verification, isolated rootless image build, signed
Docker import/start, install, route publication, inference, stop, route
withdrawal, and uninstall. For native DS4/Mia model qualification, restart
persistence, and rank failure/recovery, continue with
[Development agent workload acceptance](development-agent-workloads.md) and
the canonical
[recipe acceptance sequence](../operators/execution-harnesses.md#controller-execution-sequence).

Finish the supported installation in the browser: open the stable private
Tailscale HTTPS Service URL, log in as exact subject `admin`, and confirm the
authenticated Fleet page shows both Sparks with their certificate-bound
identities and fresh inventory. Use **Logout** and confirm the login page
returns. Closing every terminal must not affect browser availability.

## Normal updates

- NAS development stack: in the existing Docker UI project choose **Pull**,
  then **Redeploy**. Keep the Compose file, secrets, and named volumes.
  Reopen the same stable Service URL, log in, verify both Sparks, and use
  **Logout** when finished. A restart without Pull is not an update.
- GPU nodes: after the accepted APT `dev` publication is complete, follow
  [Update and switch channels](../operations/agent-package-release.md#update-and-switch-channels).
  Apt stages the signed inactive slot; activate and prove one canary node,
  wait for fresh controller inventory, then repeat on the next node.
- Never substitute production `latest` tags or a local build. Production is
  selected only through its signed release and trusted host updater.
