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

The wizard creates `vonk-forge/docker-compose.yaml`, `.env`, `secrets/`,
`backups/` and `backups-offhost/`.
It asks for the site networking and imported provider credentials, generates
the remaining secrets and Step CA identity locally. It does not need Docker, Git, sudo, SSH, or NAS access.

Drag the entire generated directory onto the NAS and start it as one Compose
project. Keep the relative layout unchanged. Caddy publishes 8443 on all host
interfaces for the Spark backend and the browser UI (TLS plus application
login); Secure remote also serves the browser through the generated Tailscale
gateway.

PostgreSQL owns control state and Step CA owns agent certificates. The API
performs its bounded database and secret initialization before serving, so the
project has no exited setup containers. Neither API nor worker mounts a source
checkout.

After startup, open
`https://vonk-forge.<TAILNET_DNS_SUFFIX>.ts.net/` using the suffix copied during
the preflight, and run the runbook's
[post-install verification](tailscale.md#verification).
Choose **Enroll Spark** on the Overview page to create a one-use grant, then
run its generated `--enroll` command on that Spark; one grant enrolls one Spark.
The command includes the reserved NAS LAN address when the private service
hostnames do not resolve directly; its shape is in [Install or upgrade a Spark](../operations/install-vonk-agent.md).

The installer verifies the controller CA before trusting its bootstrap data and
manages the Spark's hostname mapping automatically. On each fresh Spark it
detects that node's management and fabric addresses plus its peer's fabric
address (asking only when detection is ambiguous), then writes the agent and
firewall configuration itself. The NAS
address comes from the generated command. Tailscale remains on the NAS gateway;
it is not required on the Sparks.

Rerun the NAS command from the existing bundle's parent directory to prepare a
NAS upgrade, then follow the [NAS Compose redeployment procedure](../maintainers/operator-cli-access.md#nas-compose-redeployment).
An approved headless host CLI can apply the bundle without a NAS browser session.
After the NAS is running the accepted release, use **Upgrade agents** on the
Overview page (or `vonkctl fleet upgrade`) for routine Spark package rollouts;
every eligible Spark upgrades independently over its existing agent connection,
each waiting only for its own running work, and no SSH is required. Rerun the
channel command without `--enroll` for package repair; run a newly generated
`--enroll` command for a fresh install or certificate replacement. No separate
bootstrap, migration, supervisor, or rollback-slot command is required.
