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

Then run one command as your normal user:

```sh
curl -fsSL https://install.vonkforge.ai/nas | sh
```

Until the first stable release is published, the unqualified `/nas`, `/spark` and `/vonkctl` URLs serve the newest accepted development release; once a stable release exists they serve stable. To choose a channel explicitly, use `https://install.vonkforge.ai/dev/nas` (or `/dev/spark`, `/dev/vonkctl`).

The command downloads a verified native setup program, asks for the selected
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

- the control hostname (`vonk-forge.<tailnet>.ts.net`); the enrollment, agent,
  and registry names are derived from it;
- the Tailscale OAuth client ID and secret;
- whether to enable the optional Hermes agent.

Both modes offer an optional LiteLLM upstream provider key and an optional
Hugging Face token. The Spark management CIDRs default to the controller's own
/24 and the direct fabric CIDRs to `192.168.100.0/24,192.168.101.0/24`; the
installer prints both, and you can change them in `.env`. Passwords, service
tokens, signing keys, database URLs, and a coherent Step CA PKI are always
generated locally (the administrator password is in `secrets/admin-password`).
Secret values are written only under `secrets/`; `.env` holds the few
non-secret site values. Rerunning the installer on an existing bundle keeps
them and regenerates `docker-compose.yaml`.

For gated or private Hugging Face model-cache downloads, see the
[Hugging Face model-cache authentication guide](../../docs/model-cache-huggingface-auth.md).
The background worker has a dedicated outbound `artifact-egress` network for
model and OCI downloads. It publishes no ports. The API, PostgreSQL, and worker
authority channel keep their internal networks.

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
runtime Git checkout, host updater, migration helper, one-shot initializer, or
A/B agent supervisor.

## Upgrade

Run the same command from the directory that already contains `vonk-forge/`.
The existing bundle files belong to root, so put `sudo` on the `sh` side of the
pipe (`sudo curl ... | sh` does not work):

```sh
curl -fsSL https://install.vonkforge.ai/nas | sudo sh
```

Upgrade mode preserves `secrets/`, site identity, and the values of current
`.env` settings while atomically replacing the release-controlled Compose file
and adding any newly required inputs. Settings a release no longer uses are
dropped from `.env` and listed in the installer output. Place the resulting directory over the controller project, pull, and
redeploy. Keep named volumes during normal upgrades.

Non-secret runtime configuration (the Caddyfile, service entrypoints,
Prometheus, Grafana, registry and LiteLLM supervisor files) ships inside the
Controller API image. On every start the API stages it into the
`runtime-assets` volume that the other services read, and those services
restart with it. A pull and redeploy therefore rolls out configuration too; the
installer only needs to run again when `.env`, secrets or the Compose graph
itself change.
The Caddy startup wrapper forwards an explicit Compose `command` after the
runtime-asset wait and native secret checks. With no command, its native
entrypoint selects `/run/vonk-runtime-assets/caddy/Caddyfile`.

Development and production use this exact topology and configuration contract.
They use development `:dev` or production `:latest` application images and channel-specific Spark package versions
selected by the installer publication channel. Hermes is the sole optional
service.


Generated deployment Compose and the NAS curl installer payload use floating image
tags: Vonk images follow `:dev` or production `:latest`; upstream services follow
`:latest`. Every service pulls on startup/redeploy. The source templates and pinned
publication artifact provide evidence inputs, not the installed image policy.

## Database backups

PostgreSQL writes automatic dumps to `./backups/` in the project folder. See
[backup settings and restoration](../../docs/postgres-backups.md).

### Runtime asset startup observation

The Controller pre-exec stages public runtime assets before opening PostgreSQL.
Consumers poll once per second for up to 120 checks (119 nominal sleeps), then
log the exact missing asset and exit. This two-minute observation window matches
the Controller database startup window and bounds one startup attempt, not the
time required to recover an outage. Scheduling and file I/O can add elapsed time.
The existing `unless-stopped` restart policy retries automatically when staging
becomes available. PostgreSQL must not depend on a healthy Controller: staging
precedes the Controller's database initialization and avoids a dependency cycle.

Each shipped file is a bounded regular file (at most 64 KiB) published with fsync
and atomic replacement. The startup window is an observation policy, not a
measured guarantee of storage throughput. Local shell tests prove timeout and
execution after staging; hosted Compose tests separately prove Docker restart
and actual PostgreSQL readiness.
