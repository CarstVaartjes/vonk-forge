# Controller-host deployment

The supported controller installation starts on an ordinary Linux or macOS
workstation. The eventual controller host can be this same laptop or any local
NAS or server that runs Docker Compose. The installer defaults to Secure
remote for the existing Tailscale gateway; Lab mode for LAN-only access is an
explicit choice (answer `lab` at the install mode prompt). Complete
the [Tailscale fresh-install preflight](../../docs/runbooks/tailscale.md#fresh-install-preflight)
for Secure remote; it covers the canonical unsuffixed names,
MagicDNS/HTTPS, exact grants and auto-approvals, gateway self-access, and the
scoped OAuth client. The [Lab quickstart](../../docs/QUICKSTART.md) covers the
local CA and LAN DNS requirements.

Provide `curl`, `openssl` and installed `uv` on the Intel or ARM Linux
or macOS preparation workstation. `uv` manages the setup wheel's isolated
Python 3.14 environment. Then run one command as your normal user:

```sh
curl -fsSL https://install.vonkforge.ai/nas | sh
```

Until the first stable release is published, the unqualified `/nas`, `/spark` and `/vonkctl` URLs serve the newest accepted development release; once a stable release exists they serve stable. To choose a channel explicitly, use `https://install.vonkforge.ai/dev/nas` (or `/dev/spark`, `/dev/vonkctl`).

The command verifies the signed release, exact Python setup wheel and payload,
asks for the selected
mode and its inputs through the terminal, generates internal credentials and
the local CA locally, and creates exactly:

```text
vonk-forge/
├── docker-compose.yaml
├── .env
├── secrets/
├── backups/
└── backups-offhost/
```

No Docker daemon, Git checkout, root access, SSH connection, mounted share, or
repository file is needed on the preparation workstation. Keep the whole
`vonk-forge/` directory on this computer or move it to another local controller
host without changing its internal layout. Start `docker-compose.yaml` with the
host's shell or Docker/Compose application. The Compose file uses relative paths,
so the entries must remain together.

The `/nas` segment in the public installer URL is historical; it is not a
hardware restriction.

Lab mode asks only for the reserved LAN address and the two optional keys
below. Secure remote also asks for:

- the control hostname (`vonk-forge.<tailnet>.ts.net`); the enrollment and agent
  names are derived from it;
- the Tailscale OAuth client ID and secret;

Both modes offer an optional LiteLLM upstream provider key and an optional
Hugging Face token. The Spark management CIDRs default to the controller's own
/24 and the direct fabric CIDRs to `192.168.100.0/24,192.168.101.0/24`; the
installer prints both, and you can change them in `.env`. Passwords, service
tokens, signing keys, database URLs, and a coherent Step CA PKI are always
generated locally (the administrator password is in `secrets/admin-password`).
Secret values are written only under `secrets/`; `.env` holds the few
non-secret site values. Rerunning the installer on an existing bundle retains valid site values and
credentials while regenerating `docker-compose.yaml`. Private staging, durable
credential replay and certificate/key validation apply to upgrades as well as
fresh preparation. It does not install a resident NAS setup service.

For gated or private Hugging Face model-cache downloads, see the
[Hugging Face model-cache authentication guide](../../docs/model-cache-huggingface-auth.md).
The background worker has a dedicated outbound `artifact-egress` network for
model and OCI downloads and for the signed recipe library releases on GitHub
(the worker runs the automatic catalog sync). It publishes no ports. The API,
PostgreSQL, and worker authority channel keep their internal networks; the
API never contacts GitHub.

The Hugging Face token is saved as an optional secret; leaving it blank keeps
public model downloads anonymous. The LiteLLM upstream key is optional for
local model routes.

## Start and verify

In a Docker UI, select the complete directory as one Compose project, pull the
referenced images, and start it. On any controller host with a shell, the
equivalent is:

```sh
cd vonk-forge
sudo docker compose pull
sudo docker compose up -d --wait --remove-orphans
sudo docker compose ps
```

Use the same project when upgrading. `--remove-orphans` stops and removes
containers for services removed from the new bundle while preserving named
volumes and retained service data.

The installer resolves the install mode (`COMPOSE_PROFILES`
in `.env`) into `docker-compose.yaml`: it holds exactly the selected services and
no `profiles:` key, so the Docker UI and a plain `docker compose` deploy the
same set (the UGREEN redeploy was observed not to apply the `.env` profile
selection, while stock Compose does). Never pass `--profile`; rerun the installer to change the selection.

For Secure remote, complete the runbook's
[post-install verification](../../docs/runbooks/tailscale.md#verification),
including `Self.PrimaryRoutes` and a browser test from an authorized
Tailscale-connected client. For Lab mode, trust the generated local CA and
verify the controller from a LAN client.

Do not add host-path overrides. Persistent data belongs to the named Docker
volumes declared by the generated project. Caddy binds to the configured LAN
address and serves the browser UI with the generated local CA certificate;
Secure remote additionally enables the Tailscale gateway. PostgreSQL is the
control authority and Step CA is the agent identity authority. There is no
runtime Git checkout, host updater, migration helper, or A/B agent supervisor.
The one setup service is `runtime-init` (below); like every service it stays
running.

## Upgrade

Run the same command from the directory that already contains `vonk-forge/`.
Use the account that owns the existing private files. For a root-owned bundle,
or an explicitly authorized repair of the configured secret group, put `sudo`
on the `sh` side of the pipe (`sudo curl ... | sh` does not work). Installed `uv`
must be available to the account running the installer:

```sh
curl -fsSL https://install.vonkforge.ai/nas | sudo sh
```

Upgrade mode preserves `secrets/`, site identity, and the values of current
`.env` settings while atomically replacing the release-controlled Compose file
and adding any newly required inputs. Settings a release no longer uses are
dropped from `.env` and listed in the installer output. Place the resulting directory over the controller project, pull, and
redeploy. Keep named volumes during normal upgrades.

The installer leaves `docker-compose.yaml`, `.env` and `secrets/` untouched
until every check, the PKI step and Compose resolution included, has passed;
it then publishes secrets and `.env` together and writes the Compose file last.
A failed run therefore leaves the previous, coherent bundle, which a redeploy
in between still starts. Fix the reported cause and rerun the installer.

Non-secret runtime configuration (the Caddyfile, service entrypoints,
Prometheus and LiteLLM supervisor files) ships inside the
Controller API image. The `runtime-init` helper, which runs that image, stages
it into the `runtime-assets` volume that the other services read (see
[Runtime staging](#runtime-staging-runtime-init)). A pull and redeploy therefore
rolls out configuration too; the installer only needs to run again when `.env`,
secrets or the Compose graph itself change.
The LiteLLM supervisor script comes from the API image, but the route
activation contract it imports is baked into the LiteLLM image. Each may run
with the other's previous release. Transitions follow deploy order, with no
compatibility code: a new supervisor script that meets an older contract module
fails at import, before any LiteLLM child starts, so the container restarts
(`restart: unless-stopped`) until the matching LiteLLM image runs (one redeploy;
the release ships both images together).
On a route change the supervisor restarts LiteLLM. It writes a `drained`
acknowledgement to the `litellm-supervisor-state` volume once the old child has
exited, and a `ready` acknowledgement once the new child is healthy. A Controller
withdrawal (for example a Stop) waits only for `drained`; a publication waits for
`ready`. All routes stay unavailable until the new child is ready.
The Caddy startup wrapper forwards an explicit Compose `command` after its
native secret checks. With no command, its native
entrypoint selects `/run/vonk-runtime-assets/caddy/Caddyfile` and watches it,
so a restaged Caddyfile applies without a Caddy restart.

Development and production use this exact topology and configuration contract.
They use development `:dev` or production `:latest` application images and channel-specific Spark package versions
selected by the installer publication channel.


Generated deployment Compose and the NAS curl installer payload use floating image
tags: Vonk images follow `:dev` or production `:latest`; upstream services follow
`:latest`. Every service pulls on startup/redeploy. The source templates and pinned
publication artifact provide evidence inputs, not the installed image policy.

## Database backups

PostgreSQL writes automatic dumps to `./backups/` in the project folder. See
[backup settings and restoration](../../docs/postgres-backups.md).

Controller connections commit with `synchronous_commit=off`, set per connection
in `postgresql_connect_args`: model downloads share the NAS pool and made every
commit wait on the WAL fsync. LiteLLM's database in the same cluster stays
synchronous. Acknowledged enrollment/certificate state and browser logout commit
synchronously (`require_durable_commit`); local CA revocation rows commit
asynchronously but are flushed by the durable confirm that follows. Any unclean
PostgreSQL stop may lose the Controller's other commits of the last moments:
about 0.6 s, longer while the disk is stalled. Data is never corrupted; the
platform self-heals.

## Runtime staging (`runtime-init`)

`runtime-init` runs the Controller API image (`restart: unless-stopped`, root
with only `CHOWN`, `FOWNER` and `DAC_OVERRIDE`, no network, no database). Each
time its container starts it stages, before the services that read it start:

- each consumer's private copy of the installer's Compose secrets in
  `normalized-private-keys` (owner and mode per consumer, the administrator
  password included), and the gateway master key under
  `secrets/gateway/authority`;
- this release's runtime kit in `runtime-assets`, with files a release no
  longer ships removed;
- the ownership of the shared volume roots (`/state`, the object stores,
  `/routes`, `/supervisor`, `secrets/gateway`).

Every file is a bounded regular file (at most 64 KiB) published with fsync and
atomic replacement, so a reader sees the old or the new copy, never a partial
one. A staging run retries three times and then exits 1, and the restart policy
runs it again.

After a completed run it writes a marker on its own tmpfs (`/tmp/runtime-staged`)
and idles in a blocking wait, doing no work. Its healthcheck reads the marker.
The tmpfs is empty on every container start, so a restarted or recreated
container is unhealthy until its own run has completed. It stays running
because NAS Docker UIs (UGREEN) show a project with an exited container as
failed.

Its readers declare `runtime-init: {condition: service_healthy, required:
false}`. Compose starts them only once it is healthy, so nothing polls for a
staged file. The edge orders startup and never gates it: after a failed run
Compose starts them anyway on the previous staging kept in the volumes, while
the restart policy keeps running `runtime-init`. Caddy, LiteLLM, Prometheus and
the Tailscale configurator also declare `restart: true`: when a new image
recreates `runtime-init`, Compose restarts them after the new kit is staged.
The healthcheck's `start_period` (45 s, about twice the 23 s the NAS took from
container start to healthy) is that wait's budget: a slow staging run that
completes within it restarts the readers once, on the new kit. A crash loop
whose runs exit quickly releases `up` within seconds, and Compose starts the
readers on the previous kit. Every restart resets the health to `starting` and
starts the grace again, so runs that fail after 10 s or more can hold `up` and
its dependants (PostgreSQL, the API and the worker included) for several runs,
each at most the grace plus 15 s. When staging later succeeds, Caddy
(`--watch`) and Prometheus (`--config.auto-reload`) apply it in place; that
reload is the self-heal for a late recovery. LiteLLM and the Tailscale
configurator pick it up only when they are next restarted or recreated; an
`up` that changes nothing restarts nothing.
PostgreSQL does not restart with a release. The API keeps only the database
half of startup (schema and administrator) in its pre-exec, because PostgreSQL
itself starts from the staged entrypoint.

`control-api` runs as 10001 with every capability dropped and no Compose
secret: everything it reads, the administrator password included, is a staged
copy. An older Compose file (before the NAS installer reran) still starts the
API image as root; the pre-exec then steps down to 10001 at once and keeps
serving, so it never writes a root-owned file into a volume the worker shares.
That file stages no administrator password, so the administrator bootstrap is
skipped (an existing administrator is unchanged), and `NasComposeBundleStale`
asks for the installer rerun.

After a NAS reboot Docker restarts every service by its restart policy, without
Compose ordering. `runtime-init` restages the same release while the others
start from the retained volumes.

## NAS daemon platform-image retention and network repair

`image-janitor` is an independent, stdlib-only Python service using the reviewed
slim Python image. It alone mounts the NAS Docker socket; neither Controller
service has socket access. The renderer embeds its small script in the Python command of the signed NAS
bundle, with no Controller or worker package in the janitor. This keeps the
root filesystem read-only without Compose inline-config injection.

The hourly scan considers images carrying the exact Vonk Forge source label or
an exact API, worker or LiteLLM repository identity (including repository
digests after a tag moves). All third-party images are excluded, including old
PostgreSQL, Caddy and Tailscale pins. A container reference from any project,
running or stopped, protects an image and resets its unused grace. First-seen
unused timestamps live in `image-janitor-state`, published atomically with fsync;
restarting the service preserves them. Build age and disk pressure do not select
an image for deletion. The first scan starts the 24-hour grace; it does not
immediately remove old builds.

Deletion addresses the exact image ID, with `force=false` and parent pruning
disabled. Docker remains the final guard against a concurrent container create.
A refusal (including multiple repository tags) retains the image, logs its cause
and next hourly attempt, then re-plans from fresh inventory. The service never
removes containers or volumes. Logs include every exclusion/grace refusal and
Docker's exact deletion receipts. Reported image size includes shared layers
and is not a measurement of freed disk space. Failed scans leave health stale;
the healthcheck reports unhealthy after two hours without a successful scan.

The same process repairs lost network attachments of its own Compose project
every 30 seconds. `compose up` does not: it starts a stopped container as-is
and checks networks only on running ones, and a restart reuses the container
record. A network recreated under a stopped container (new ID) therefore
leaves it exited with `network ... not found`; for Caddy that closes `:8443`.
Losing Docker's network store across a daemon restart produces exactly this.

The janitor heals against the networks the rendered bundle declares for each
service (`VONK_COMPOSE_NETWORKS`, rendered by `scripts/render-dev-compose`),
not against the container's own record: Docker can lose a network and drop it
from the record together, and a healer that trusted the record saw nothing
wrong (the 2026-11-19 outage, the third after 11-03 and 11-05: Caddy running
without `ingress`, `:8443` closed, no outbound DNS). Once per tick, against one
inspect per network, a declared network that does not exist is broken whatever
the record says, but only for a container that should run (running, a failed
start, or a journalled start) and is still in the state it was inspected in:
Compose removes the network of a container it stopped cleanly and reconnects it
itself, so the janitor waits (`WAIT_NETWORK`) instead of recreating the network
under it. A recorded entry is broken when it names an older network ID,
or when the container runs and the network lists no endpoint for it. A declared
network that exists but is missing from the record is left to Compose (a
redeploy may be attaching it), unless a journalled repair owes it or the
network carries the janitor's own mark. A missing declared network is recreated
only inside a journalled, backed-off repair, from the bundle's definition
(`internal` flag, Compose project and network labels). It carries a
`config-hash` that Compose never computes, so the next `compose up` sees it as
diverged and recreates it from the bundle: Compose stays the owner. An
undeclared missing network waits for the next `compose up`. A non-empty,
unparsable `VONK_COMPOSE_NETWORKS` logs `declared-networks-invalid-blind` and
the janitor falls back to the records alone.

State to action, first match: Docker-owned state (`restarting`, `paused`,
`removing`, `dead`) waits; a missing undeclared network waits; a broken or owed
network is repaired; a container that is not running but should be (failed
start with an `always`/`unless-stopped` policy, or owed a start by the journal)
is started; a container left `created` by an aborted `up` is started after a
grace; otherwise nothing. The repair is journalled in `image-janitor-state`
(`/state/repairing.json`) before its first effect, then stops a running
container, disconnects and re-attaches each broken network by name with its
aliases, and starts the container if it should run. A repair that fails
half-way still starts the container, but the missing network stays owed,
so the journal (or the network mark) owes it again on the next tick;
each repair backs off exponentially with jitter (30 s doubling to five minutes)
while it does not converge. Containers sharing another container's network
namespace are not touched.

This uses more of the Docker API than image collection: the janitor stops and
starts project containers, creates a declared network that vanished, and
disconnects and connects their networks.

The janitor's health check also requires a completed repair pass in the last 15
minutes. Caddy's health check requires a default route, which exists only
while `ingress` (its one non-internal network) is attached, so a half-attached
Caddy shows `unhealthy`. If every active Spark is offline for five minutes,
the critical `AgentIngressDown` alert fires and names this cause (runbook:
`docs/runbooks/observability.md#agent-ingress-down`). If the janitor log shows
`repair-backoff` repeating, a repair is not converging; as a manual recovery
delete the Caddy container (not volumes) and Redeploy.

**Deployment requires rerunning the accepted NAS installer in the existing bundle
directory**, preserving `.env`, secrets and volumes, then redeploying Compose.
Pulling newer API/worker images alone cannot install this service or update its
embedded script. Its named state volume must survive upgrades.

This daemon store is separate from recipe availability: recipe preparation
copies registry bytes with skopeo into the Controller's `image-cache/oci` layout,
and the existing unused-storage and image-store collectors expire receipts and
unreferenced OCI blobs there. They do not operate the NAS Docker daemon.

## Remove it

See [Remove Vonk Forge](../../docs/operations/uninstall.md) for the Controller and Spark removal steps.
