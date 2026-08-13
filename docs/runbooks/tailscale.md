# Operate tailnet-only NAS ingress

The NAS project contains one userspace Tailscale gateway and has no host
Tailscale dependency. Human control, inference, Grafana, and Hermes enter only
through named Tailscale Services. The sole LAN listener is Caddy's restricted
GPU node backend at the reserved NAS address.

## Identity and access policy

GitHub login authenticates people to Tailscale. Use the exact
`USERNAME@github` identity shown on the Tailscale Users page. This identity
grants network reachability only: it is not the Hermes API key and gives Hermes
no repository credential. Tailnet reachability and Vonk Forge application
authentication are independent gates: an authorized tailnet user must still
complete the application administrator login at `svc:vonk-forge`.

The gateway never receives a GitHub token. Create a separate OAuth client under
Trust credentials with only `auth_keys` write scope and `tag:vonk-gateway` as its
only tag. Define these exact Services in the admin console:

- `svc:vonk-forge`, endpoint `tcp:443`;
- `svc:hermes-dashboard`, endpoint `tcp:443`; and
- `svc:hermes-api`, endpoint `tcp:443`.

Merge the reviewed sections of `deploy/compose/tailscale/grants.example.hujson`
into tailnet policy after replacing the GitHub-login placeholder. Administrators
reach only the `vonk-forge` Service through its grant. `group:hermes-users` reaches
only the two Hermes Services. Auto-approval permits only `tag:vonk-gateway` to
advertise the three named Services. Never use `svc:*` or an allow-all ACL.
Development uses only `svc:vonk-forge`; the two Hermes Services belong to the
full production graph. In both graphs, Tailscale Funnel is forbidden and no
human-facing LAN port is a fallback.

## Secrets and unattended startup

The OAuth client is created under **Tailscale admin console → Settings → Trust
credentials → OAuth clients** with only `auth_keys` write scope and only
`tag:vonk-gateway`. Capture its values once into separate mode `0600` files
without putting either value in a command argument or terminal output.

For development, use the silent-input procedure in
[Prepare private Tailscale browser access](development-nas-installation.md#prepare-private-tailscale-browser-access),
pass those files to `scripts/dev-runtime-secrets.py`, and let
`scripts/dev-runtime-project` publish the exact files. For production, create
the two empty root-owned mode `0600` NAS files, edit them with the host's
privileged secret editor, and verify only metadata—never file contents:

```bash
sudo install -d -m 0700 -o root -g root /srv/vonk-forge/secrets
sudo install -m 0600 -o root -g root /dev/null \
  /srv/vonk-forge/secrets/tailscale-oauth-client-id
sudo install -m 0600 -o root -g root /dev/null \
  /srv/vonk-forge/secrets/tailscale-oauth-client-secret
sudoedit /srv/vonk-forge/secrets/tailscale-oauth-client-id
sudoedit /srv/vonk-forge/secrets/tailscale-oauth-client-secret
sudo stat -c '%n %U:%G %a %s bytes' \
  /srv/vonk-forge/secrets/tailscale-oauth-client-id \
  /srv/vonk-forge/secrets/tailscale-oauth-client-secret
```

Set only the file paths in the root-owned site environment. The installed host
updater starts the complete selected generation during first install, upgrade,
rollback, or recovery. Verify it without invoking Compose from a checkout:

```bash
sudo vonk-control-offline doctor
sudo vonk-control-offline maintenance status
```

Persisted state and `TS_AUTH_ONCE=true` retain node identity. After clean state
loss, the scoped OAuth client performs unattended tagged enrollment and the
exact auto-approvals restore advertisements. Authentication or approval failure
leaves ingress closed; there is no LAN fallback.

The configurator waits for Caddy and Hermes health. It resets any missing,
extra, downgraded, or retargeted Serve map and deterministically creates:

```text
svc:vonk-forge         HTTPS 443 -> http://caddy:8080
svc:hermes-api        HTTPS 443 -> http://hermes-agent:8642
svc:hermes-dashboard  HTTPS 443 -> http://hermes-agent:9119
```

All listeners use explicit `--https=443`; plaintext HTTP on 443 is rejected.
This is HTTPS-only Serve. Funnel is never enabled.

## Stable browser URL and application login

The `svc:vonk-forge` Service has the stable Service URL
`https://vonk-forge.<TAILNET_NAME>.ts.net/`. The development
`tailscale-configurator` logs the exact non-secret URL as `Vonk Forge browser
URL: ...`; production exposes the same Service through the installed
maintenance status boundary. Use the reported suffix instead of inventing one.

Open that URL from an authorized Tailscale-connected browser, then complete the
separate Vonk Forge application administrator login. No SSH or PowerShell
tunnel, bearer token, or TLS exception is required. No Windows hosts-file entry
is required for this Service. The management-LAN names below are only for the
NAS and GPU nodes.

## LAN boundary

Reserve `10.0.0.2` for the NAS and resolve these only on the management LAN:

```text
enroll.vonk-forge.lan   10.0.0.2
agents.vonk-forge.lan   10.0.0.2
registry.vonk-forge.lan 10.0.0.2
```

Allow TCP 8443 only from `10.0.0.0/24`, preferably narrowed to reserved GPU node
leases. Do not allow LAN access to human or Hermes endpoints. GPU node DHCP
reservations improve stability, but identity and routing use authenticated
agent presence rather than a hard-coded address.
Do not put the Tailscale Service hostname in the Windows hosts file; MagicDNS
and Tailscale HTTPS own that name and certificate.

## Verification

```bash
sudo vonk-control-offline maintenance tailscale-status
sudo vonk-control-offline maintenance tailscale-serve-status
sudo vonk-control-offline maintenance tailscale-serve-config
sudo vonk-control-offline maintenance logs \
  --service tailscale-configurator --since-minutes 30
```

Status must report `HTTPS: true` on all three Services and never `HTTP: true`.
The export must contain exactly the three upstreams above. Test dashboard and
API reachability as an authorized GitHub-backed user, then confirm a user
outside `group:hermes-users` is denied. Even an authorized user must supply the
separate Hermes key to invoke the API. Confirm an ordinary LAN client cannot
reach either Hermes endpoint.

## Drain, revocation, and recovery

Do not run `docker compose down`; it bypasses the selected-generation journal.
A platform transition or recovery uses the updater's fixed stop/start sequence.
For a full host drain, withdraw GPU node routes and human access, complete the
encrypted control-host backup, then stop the Docker host through its normal OS
shutdown procedure. Back up `tailscale-state` and the OAuth files with the same
encrypted generation as the control database and Hermes state. Restore state
before startup when possible.

If state cannot be restored, recreate the project with the OAuth files. Verify
exactly one current tagged node advertises all three Services and revoke the
orphan. For compromise, revoke OAuth, the node, and its tag/Service approvals;
then rotate and recover through reviewed policy. Never add a temporary LAN
human endpoint.
