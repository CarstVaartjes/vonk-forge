# Bootstrap the control plane

Prepare the complete control-plane project on a Linux or macOS workstation:

```sh
curl -fsSL https://install.vonkforge.ai/nas | sh
```

The wizard creates `vonk-forge/docker-compose.yaml`, `.env`, and `secrets/`.
It asks for the site networking and imported provider credentials, generates
the remaining secrets and Step CA identity locally, and optionally enables
Hermes. It does not need Docker, Git, sudo, SSH, or NAS access.

Drag the entire generated directory onto the NAS and start it as one Compose
project. Keep the relative layout unchanged. Only Caddy publishes the Spark
backend port; browser access, Grafana, inference, and optional Hermes enter
through the generated Tailscale gateway.

PostgreSQL owns control state and Step CA owns agent certificates. The API
performs its bounded database and secret initialization before serving, so the
project has no exited setup containers. Neither API nor worker mounts a source
checkout.

After startup, get the private browser URL from `tailscale-configurator` logs.
Create a one-use Spark pairing token in that interface, then install each Spark
with its own one-command flow:

```sh
curl -fsSL https://install.vonkforge.ai/spark | sh
```

Rerun the NAS command from the existing bundle's parent directory to prepare a
NAS upgrade. Rerun the Spark command on an installed Spark to upgrade it. No
separate bootstrap, migration, supervisor, or updater command is supported.
