# Fresh development installation

Development uses the production topology with immutable development release
identities. It is not a second stack and does not use local images, alternate
ports, synthetic deployment behavior, repository mounts, or a separate PKI.
It also uses the normal unsuffixed Tailscale Service names; do not create a
`-dev` or `-acceptance` Service. Complete the one-time Tailscale preparation in
[`tailscale.md`](tailscale.md#fresh-install-preflight) before running the NAS
installer.
The installer publication path accepts schema 2 only; schema-1 release
manifests are intentionally rejected.

## Prepare the NAS upload

First complete the
[Tailscale fresh-install preflight](tailscale.md#fresh-install-preflight).
Development and stable use the same canonical Services; test-only names and
policy never belong in an operator tailnet.

On an Intel or ARM Linux or macOS workstation with `curl`, `openssl`
and installed `uv`, run the command below. It verifies the exact Python NAS
setup wheel and payload, then uses an isolated Python 3.15 environment:

```sh
curl -fsSL https://install.vonkforge.ai/dev/nas | sh
```

Answer the prompts, then drag the generated `vonk-forge/` directory onto the
NAS and start `docker-compose.yaml` in its Docker application. The directory
contains the Compose file, `.env`, and all required secrets. No repository
checkout, Docker daemon, sudo, SSH, or NAS mount is needed to prepare it.

## Add each Spark

Create a one-use pairing grant in Vonk Forge, then copy its generated command to
the Spark as its normal administrator account. It includes
`VONK_CONTROLLER_ADDRESS=<reserved NAS LAN IP>` when required:

```sh
curl -fsSL https://install.vonkforge.ai/dev/spark | sh
```

The command verifies the development release before requesting sudo, installs
the direct Rust agent, exchanges the token for the Spark certificate, starts
the service, configures the verified NAS hostname mapping without Spark-side
Tailscale or manual DNS changes, and verifies sustained readiness. Repeat the
controller-generated command later to upgrade the same Spark.

Stable installation uses the same two commands without `/dev`; that channel
selection is the only installation-flow difference.

## Tailscale OAuth secrets

`tailscale-gateway` drops every Linux capability. It never reads the
installer's files: `runtime-init` (root, with `DAC_OVERRIDE`) copies
`tailscale-oauth-client-id` and `tailscale-oauth-client-secret` into the
`tailscale-oauth` volume, owned by root with mode `0400`, and only the gateway
mounts it. The installer may therefore run as any user, on the Compose host or
on another machine before the directory is moved; no group or `sudo` is needed.
