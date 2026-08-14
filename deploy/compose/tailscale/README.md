# Tailscale gateway component

This included Compose model exposes three named tailnet Services from one tagged,
userspace Tailscale node:

- The `vonk-forge` service forwards tailnet TCP 443 to Caddy's private port 8080.
- `svc:hermes-dashboard` terminates tailnet HTTPS 443 and forwards to Hermes port 9119.
- `svc:hermes-api` terminates tailnet HTTPS 443 and forwards to Hermes port 8642.

It publishes no Docker host port, routes no LAN subnet, and receives no tunnel
device or network capability. OAuth client ID and secret values are read from
Compose secret files. The gateway derives a mode-`0400` credential in its
bounded `/tmp` tmpfs with Tailscale's
`?ephemeral=false&preauthorized=true` parameters, never places the raw value in
a shell variable or output, and then starts the official `containerboot`. The
tag-scoped preauthorization covers device enrollment only; tailnet grants and
Service-host approval remain separate. State persists in
`tailscale-state`; the configurator continuously reconciles and advertises only
the three explicit Services. It uses the explicit `--https=443` CLI form and
verifies that Serve status reports HTTPS, never plaintext HTTP, on port 443.

Before use, define all three Services in the Tailscale admin console, apply a
reviewed version of `grants.example.hujson`, and replace the GitHub-login
placeholder with the exact identity shown by Tailscale. Create an OAuth client
with only `auth_keys` write scope for `tag:vonk-gateway`. The OAuth client is for
unattended gateway enrollment; it is not the operator's GitHub credential. Keep
the captured client secret raw: Compose adds the non-ephemeral enrollment
parameter only inside the gateway tmpfs.

See [the gateway runbook](../../../docs/runbooks/tailscale.md) for setup,
verification, backup, and recovery.
