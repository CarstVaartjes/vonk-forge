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

The command downloads a verified native setup program, asks for the selected
mode and its inputs through the terminal, generates internal credentials and
the local CA locally, and creates exactly:

```text
vonk-forge/
├── docker-compose.yaml
├── .env
├── secrets/
└── backups/
```

No Docker daemon, Git checkout, root access, SSH connection, mounted share, or
repository file is needed on the preparation workstation. Keep the whole
`vonk-forge/` directory on this computer or move it to another local controller
host without changing its internal layout. Start `docker-compose.yaml` with the
host's shell or Docker/Compose application. The Compose file uses relative paths,
so the four entries must remain together.

The `/nas` segment in the public installer URL is historical; it is not a
hardware restriction.

Lab mode asks for the reserved LAN address and optional Hugging Face token.
Secure remote asks for:

- the controller host's LAN address, Spark management/fabric CIDRs, and
  operator jurisdiction;
- the control, enrollment, agent, and registry hostnames;
- Tailscale OAuth credentials;
- the LiteLLM upstream provider key;
- whether to enable optional Hermes, plus its values when selected;
- an optional Hugging Face token in either mode.

Passwords, service tokens, signing keys, database URLs, and a coherent Step CA
PKI are generated locally in Lab mode. Secure remote also offers existing
credential imports. Secret values are written only under `secrets/`; `.env` contains non-secret site
configuration and relative secret paths.

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
docker compose pull
docker compose up -d --wait --remove-orphans
docker compose ps
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

Run the same workstation command from the directory that already contains
`vonk-forge/`:

```sh
curl -fsSL https://install.vonkforge.ai/nas | sh
```

Upgrade mode preserves `.env`, `secrets/`, and site identity while atomically
replacing the release-controlled Compose file and adding any newly required
inputs. Place the resulting directory over the controller project, pull, and
redeploy. Keep named volumes during normal upgrades.

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
