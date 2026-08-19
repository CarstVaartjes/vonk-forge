# Development NAS installation

Development is the development image channel of the production deployment.
Production and development are mutually exclusive choices for one network;
they are not two topologies designed to run together. The only selected
runtime difference is the API and worker image version published by GitHub
Actions.

## Project layout

Use the same project layout and site inputs as production:

```text
vonk-forge/
├── docker-compose.yaml
├── .env
└── secrets/
```

The Compose file is the production graph from `deploy/compose/compose.yaml`.
The `.env` file contains the same non-secret site configuration used by
production. The `secrets/` directory contains the same credential and Step CA
files. Docker named volumes are the same production volume names. Do not create
a `dev-*` volume, synthetic secret bundle, local source checkout, alternate
hostname, alternate port, or second PKI root.

Hermes is an opt-in profile, not a prerequisite for the control plane. The
default project starts without requiring a Hermes image, Hermes API key,
workspace, or dashboard origin. To enable Hermes, provide these additional
`.env` values with an immutable published Hermes image and create its
persistent data directories:

```dotenv
HERMES_AGENT_IMAGE=ghcr.io/carstvaartjes/vonk-forge-hermes:<version>@sha256:<digest>
HERMES_API_KEY_FILE=/srv/vonk-forge/secrets/hermes-api-key
HERMES_DATA_ROOT=/srv/vonk-forge/hermes
HERMES_DASHBOARD_ORIGIN=https://hermes-dashboard.<tailnet>.ts.net
```

Then start the profile alongside the normal project:

```bash
sudo install -d -m 0750 /srv/vonk-forge/hermes/{data,workspaces,cache}
docker compose --env-file .env --profile hermes up -d --wait
```

The optional setup container is run explicitly with the `setup` profile:

```bash
docker compose --env-file .env --profile setup run --rm hermes-setup
```

Do not enable the profile with a mutable tag or the upstream base image. The
published Vonk Forge Hermes image contains the fixed UID/GID and the Vonk
entrypoint that validates the API-key file.

Keep the project directory and secrets protected by the NAS filesystem. The
Compose file contains secret paths, not secret values. Back up the secret and
volume data according to the production backup policy before changing image
channels.

## Select the development release

GitHub Actions publishes immutable development image references after accepting
`main`. Set the exact references from that workflow run in the shell or in the
operator environment file:

```bash
export VONK_DEV_API_IMAGE='ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-<40-char-sha>@sha256:<api-digest>'
export VONK_DEV_WORKER_IMAGE='ghcr.io/carstvaartjes/vonk-forge-worker:dev-sha-<40-char-sha>@sha256:<worker-digest>'
```

The references must be GHCR `dev-sha-*` images with manifest digests. The
launcher rejects mutable tags, local images, and images from another registry.
It uses the same `.env`, secret paths, project name, endpoints, networks,
volumes, and backend port as production:

```bash
scripts/dev-compose up -d --wait --pull always
docker compose --env-file .env -f deploy/compose/compose.yaml ps
```

To select a later development version, change only the two image references
and redeploy. Do not delete PostgreSQL or other persistent volumes as part of a
normal image upgrade. A schema migration or data reset is a separate,
explicitly planned operation.

## Access and PKI

Use the configured production hostnames and the same Caddy/Tailscale path.
There is no loopback-only development endpoint and no development port mapping.
Agent enrollment, server trust, client trust, renewal, and health checks use
the same Step CA-backed contract as production. Follow
[Agent PKI](agent-pki.md) for CA state and certificate operations.

Before changing the channel, verify that the NAS clock is correct, the
configured Step CA state is present, and the secret files are readable by the
Compose project. If Caddy or the agent reports an expired certificate, repair
the shared certificate-provider state; do not generate a separate development
certificate or replace the trust root.

## Rollback

Rollback is an image selection change. Restore the previously accepted
immutable API and worker references, then redeploy the same Compose project.
Keep the database and persistent volumes intact unless the selected release's
migration policy explicitly requires a recovery procedure.

```bash
export VONK_DEV_API_IMAGE='<previous-api-image@sha256:digest>'
export VONK_DEV_WORKER_IMAGE='<previous-worker-image@sha256:digest>'
scripts/dev-compose up -d --wait --pull always
```

Production release selection remains governed by the production host updater.
The deployment graph and site contract are shared; only the chosen published
application image/version differs.
