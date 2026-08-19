# NAS Compose Bundle Design

## Goal

Provide a self-contained, drag-and-drop deployment bundle for the NAS Docker
runner. Bundle preparation must not require Docker, root privileges, or access
to the NAS runtime.

## Operator flow

On the publishing/development machine:

```bash
scripts/build-nas-compose-bundle \
  --api-image 'ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-<sha>@sha256:<digest>' \
  --worker-image 'ghcr.io/carstvaartjes/vonk-forge-worker:dev-sha-<sha>@sha256:<digest>' \
  --output vonk-forge-bundle.tar.gz
```

The resulting archive contains a single `vonk-forge/` directory. The operator
extracts it into a NAS shared folder and runs the included setup wizard from
that directory. The wizard never runs Docker or requires root; it prepares the
folder for the NAS Docker runner.

```bash
./install.sh
```

The wizard asks for the deployment hostname, NAS paths, and external
credentials. For every generated secret it offers:

1. generate a cryptographically secure value;
2. enter or import an existing value; or
3. leave it unset when the selected components do not need it.

Secret values are never echoed or printed in the summary. Existing files are
never overwritten without an explicit confirmation. Generated files receive
the strongest permissions supported by the target filesystem.

Before writing `.env`, the wizard asks which optional components to include:

- Hermes Agent and its data directories;
- the Step CA overlay, or the built-in CA overlay;
- Tailscale access configuration.

The default selection is the control plane with the existing site access
contract and Hermes disabled. Component choices determine the Compose files,
variables, and secret prompts included in the final bundle.

## Bundle contents

The archive contains:

```text
vonk-forge/
├── docker-compose.yaml
├── .env.example
├── secrets/
├── caddy/
├── grafana/
├── litellm/
├── prometheus/
├── registry/
├── tailscale/
├── trust/
└── hermes-agent/
```

The archive contains `.env.example` and no secret values. `install.sh` creates
the site-local `.env` and `secrets/` files after the operator chooses whether
to generate or provide each value. Hermes files are shipped as part of the
graph, but its services are disabled by default and its image/key inputs are
requested only when the `hermes` profile is selected.

## Validation

The builder must fail before writing an archive when:

- API or worker images are not immutable GHCR `dev-sha-*` references;
- a required canonical Compose asset is absent or a symlink;
- the rendered Compose graph does not match the production service graph;
- `.env.example` contains secret values or mutable application image tags.

The builder writes files with deterministic relative paths and rejects paths
that escape the bundle root. It may validate YAML and image references using
Python libraries already present in the repository, but it must not invoke
Docker.

## NAS-side startup

The bundle includes a short README with these commands:

```bash
docker compose --env-file .env config -q
docker compose --env-file .env up -d --wait
```

Hermes is explicitly separate:

```bash
docker compose --env-file .env --profile hermes up -d --wait
```

The README explains that Hermes requires a published digest-pinned Vonk Forge
image, API key, data root, and dashboard origin before enabling the profile.

## Testing

Tests cover image validation, archive file inventory, path traversal refusal,
absence of Docker invocation, absence of secret values, interactive generation
and import flows, refusal to overwrite existing secrets, optional-component
selection, and the default disabled Hermes profile. A fixture bundle is
extracted into a temporary directory and checked using filesystem inspection
only.
