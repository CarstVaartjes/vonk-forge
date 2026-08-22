# Vonk Forge

Vonk Forge is a private, local-first control plane for NVIDIA DGX Spark systems.
The control plane runs on a Docker-capable NAS; each Spark connects outbound with
the native Rust agent. PostgreSQL owns control state, Step CA owns agent identity,
and routine operation never requires SSH or a Git checkout.

## Install

Prepare the complete NAS directory on your workstation:

```bash
curl -fsSL https://install.vonkforge.ai/nas | sh
```

The interactive installer creates exactly:

```text
vonk-forge/
├── docker-compose.yaml
├── .env
└── secrets/
```

Drag that directory onto the NAS and start `docker-compose.yaml` with the NAS
Docker runner. The installer does not need Docker, root, Git, SSH, a mounted NAS,
or direct NAS access. Running the same command again prepares an upgrade while
preserving locally owned identity and secrets.

Create a one-use grant in the Vonk Forge Fleet page, then copy its generated
command onto the Spark. For a NAS reached through its reserved LAN address the
command has this shape:

```bash
curl -fsSL https://install.vonkforge.ai/spark | VONK_CONTROLLER_ADDRESS=192.168.1.231 sh
```

The script downloads as the current user, verifies the immutable release before
using `sudo`, installs the direct Rust agent service, completes pairing through
interactive prompts, and verifies that the service is healthy. It keeps the
controller hostnames for TLS while configuring their NAS LAN mapping
automatically; the Spark does not need Tailscale or manual DNS changes. The same
command performs an in-place upgrade on an installed Spark.

Development and production use the same services, networks, volumes, security
settings, and behavior. Only the selected immutable image and package identities
differ. Hermes is the sole optional Compose component.

## Develop

Required tools are Python 3.12+, [`uv`](https://docs.astral.sh/uv/), Rust, Node.js,
and Docker for integration tests.

```bash
uv sync --dev
uv run --frozen pytest -q
uv run --project control --frozen --with-editable . pytest -q control/tests
npm ci --prefix control/web
npm test --prefix control/web -- --run
uv run --frozen pytest -q deploy/compose/tests
scripts/verify-supply-chain --json
```

See the [documentation index](docs/README.md), [Compose deployment notes](deploy/compose/README.md),
and [testing policy](docs/testing-and-ci.md) for contributor details.

## Security

Do not commit credentials. Keep private keys, tokens, and passwords out of
configuration tracked by Git, command arguments, and captured diagnostics.

## License

Vonk Forge is available under the [MIT License](LICENSE).
