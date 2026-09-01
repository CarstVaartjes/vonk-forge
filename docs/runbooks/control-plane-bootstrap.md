# Bootstrap the control plane

Before downloading anything, complete the
[Tailscale fresh-install preflight](tailscale.md#fresh-install-preflight). It
defines the exact unsuffixed Service names, policy/self-access grants,
auto-approval, MagicDNS/HTTPS settings, OAuth scope/tag, and control hostname.
Do not create any other Service names in an operator tailnet.

The published installer channel and release manifests are schema-2-only for
this greenfield deployment.

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

After startup, open
`https://vonk-forge.<TAILNET_DNS_SUFFIX>.ts.net/` using the suffix copied during
the preflight, and run the runbook's
[post-install verification](tailscale.md#verification).
Create a one-use Spark pairing grant in that interface, then copy its generated
command to each Spark. The command includes the reserved NAS LAN address when
the private service hostnames do not resolve directly:

```sh
curl -fsSL https://install.vonkforge.ai/spark | VONK_CONTROLLER_ADDRESS=192.168.1.231 sh
```

The installer verifies the controller CA before trusting its bootstrap data and
manages the Spark's hostname mapping automatically. On each fresh Spark it also
asks for that node's management and fabric addresses plus its peer's fabric
address, then writes the agent and firewall configuration itself. The NAS
address comes from the generated command. Tailscale remains on the NAS gateway;
it is not required on the Sparks.

Rerun the NAS command from the existing bundle's parent directory to prepare a
NAS upgrade. After the NAS is running the accepted release, use Fleet's signed
one-at-a-time upgrade action for routine Spark package rollouts; it uses the
existing agent connection, requires the exact target identity before advancing,
and does not require SSH. Rerun the Spark command for package repair, a fresh
installation, or an explicit `--enroll` certificate replacement. No separate
bootstrap, migration, supervisor, or rollback-slot command is required.
