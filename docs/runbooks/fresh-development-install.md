# Fresh development installation

Development uses the production topology with immutable development release
identities. It is not a second stack and does not use local images, alternate
ports, synthetic deployment behavior, repository mounts, or a separate PKI.

## Prepare the NAS upload

On a Linux or macOS workstation, run:

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
