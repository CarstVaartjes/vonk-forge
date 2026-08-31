# Operate tailnet-only NAS ingress

The NAS project contains one userspace Tailscale gateway and has no host
Tailscale dependency. Human control, inference, Grafana, and Hermes enter only
through named Tailscale Services. The sole LAN listener is Caddy's restricted
GPU node backend at the reserved NAS address.

## Names and environments: do not mix them

An operator tailnet contains only the canonical, unsuffixed Service names:

- `svc:vonk-forge` for every installation;
- `svc:hermes-api` only when Hermes is enabled; and
- `svc:hermes-dashboard` only when Hermes is enabled.

Never define or advertise any other Service names, add test-only tags, or merge
acceptance grants, tests, auto-approvers, or destinations into an operator
tailnet. Automated full-tailnet acceptance belongs in a separate disposable
test tailnet with separate OAuth credentials. The acceptance executable refuses
to start unless that environment identifies itself as
`isolated-disposable-test`. After a run, remove its gateway nodes, Service
definitions, grants/tests, auto-approvers, OAuth client, and any other test-only
policy. Repository and local checks that do not need a tailnet run with
Tailscale disabled.

Development and stable releases use the same canonical names. The enabled
feature set, not the release channel, determines whether one or three Services
are present.

## Fresh-install preflight

Complete this checklist before running the NAS installer. The installer repeats
it before asking for the OAuth client ID or secret, but it cannot inspect the
admin console for you.

- In **DNS**, enable MagicDNS and HTTPS certificates, and copy the tailnet DNS
  suffix exactly as displayed.
- In **Services**, define `svc:vonk-forge` with endpoint `tcp:443`. If Hermes
  will be enabled, also define `svc:hermes-api` and
  `svc:hermes-dashboard`, each with endpoint `tcp:443`.
- Merge the reviewed parts of
  `deploy/compose/tailscale/grants.example.hujson` into policy. Replace the
  GitHub-login placeholder with the exact identity from **Users**.
- Confirm `tag:vonk-gateway` owns only the exact Service auto-approvals and has
  TCP 443 self-access to every Service it hosts. Do not use `svc:*`, an
  allow-all ACL, Funnel, or an acceptance tag.
- Under **Trust credentials → Credential → OAuth**, create a machine OAuth
  client with only `auth_keys` write scope and only `tag:vonk-gateway`. Keep its
  raw ID and secret ready; do not add query parameters.
- When asked for the control hostname, enter
  `vonk-forge.<TAILNET_DNS_SUFFIX>.ts.net` exactly. Do not use an acceptance
  hostname or a hosts-file alias.

## Identity and access policy

GitHub login authenticates people to Tailscale. Use the exact
`USERNAME@github` identity shown on the Tailscale Users page. This identity
grants network reachability only: it is not the Hermes API key and gives Hermes
no repository credential. Tailnet reachability and Vonk Forge application
authentication are independent gates: an authorized tailnet user must still
complete the application administrator login at `svc:vonk-forge`.

Use **Services → Advertise → Define a Service** to define the Services selected
in the preflight with these exact endpoints:

- `svc:vonk-forge`, endpoint `tcp:443`;
- `svc:hermes-dashboard`, endpoint `tcp:443` when Hermes is enabled; and
- `svc:hermes-api`, endpoint `tcp:443` when Hermes is enabled.

The gateway never receives a GitHub token. After the Services exist, use
**Trust credentials → Credential → OAuth** to create a separate machine OAuth
client with only `auth_keys` write scope and `tag:vonk-gateway` as its only
tag. Do not create a human OAuth app.

Merge the reviewed sections of `deploy/compose/tailscale/grants.example.hujson`
into tailnet policy after replacing the GitHub-login placeholder. Administrators
reach only the `vonk-forge` Service through its grant. `group:hermes-users` reaches
only the two Hermes Services. Auto-approval permits only `tag:vonk-gateway` to
advertise the three named Services. The exact TCP 443 grant from
`tag:vonk-gateway` to those same Services is also required: it gives the
already-connected proxy no backend access it does not already have, while
allowing Tailscale to assign the Service TailVIP `PrimaryRoutes`. Without it,
the console can show an approved online host while clients still report
`no matching peer`. Never use `svc:*` or an allow-all ACL.
Hermes-disabled installations use only `svc:vonk-forge`; Hermes-enabled
installations use all three. In both cases, Tailscale Funnel is forbidden and
no human-facing LAN port is a fallback.

The Services page must show at least one connected host for every Service in
use. A defined Service showing `0 hosts` has no active ingress. Allow the
configurator's bounded two-minute approval window to complete, then verify the
gateway carries `tag:vonk-gateway` and the exact named Service is present under
`autoApprovers.services`. Also verify the gateway tag has TCP 443 access to
every Service it hosts. If the console shows a pending host, approve that
specific gateway advertisement. If it shows an online host but the
configurator remains unhealthy, inspect `.Self.PrimaryRoutes` in `tailscale
status --json`; do not repeatedly recreate an approved host to mask a missing
self-access grant. Never add the `funnel` node attribute as a workaround; it
enables public internet exposure rather than approving a tailnet-only Service
host.

## Secrets and unattended startup

The OAuth client is created under **Tailscale admin console → Trust credentials
→ Credential → OAuth** with only `auth_keys` write scope and only
`tag:vonk-gateway`. Enter the ID and secret when the one-command NAS installer
asks for them:

```sh
curl -fsSL https://install.vonkforge.ai/nas | sh
```

The terminal hides secret input and writes the values directly into the local
upload bundle. Do not place either credential in a command argument, `.env`, or
shell history.

Keep the OAuth client secret file equal to the raw value issued by Tailscale;
do not append query parameters to the operator copy. At startup, the Compose
gateway writes a mode-`0400` derivative containing
`?ephemeral=false&preauthorized=true` into its bounded tmpfs without placing
the raw value in a shell variable or output, and passes only the tmpfs path to
the official Tailscale bootstrap. The tag-scoped preauthorization covers device
enrollment after state loss; it does not grant access to a Service or approve a
Service advertisement. Persisted state, explicit non-ephemeral enrollment, and
`TS_AUTH_ONCE=true` retain node identity across container, NAS, and extended
offline restarts. Tagged device identity also disables node-key expiry by
default; revoking the OAuth client, node, tag, or Service remains the recovery
boundary. After clean state loss, the scoped OAuth client performs unattended
tagged enrollment and the exact Service auto-approvals restore advertisements.
Authentication or approval failure leaves ingress closed; there is no LAN
fallback.

If the Tailscale console labels a permanent gateway **Ephemeral**, do not treat
its Service approval as final. Stop only the Tailscale gateway/configurator,
remove only the stack's Tailscale state and socket volumes, and start them
again.
Approve the replacement advertisement if the exact Service auto-approval has
not yet been installed, verify it is no longer ephemeral, then revoke the old
gateway entry. Never delete database, repository, model, control-state, or
other application volumes during this repair.

The configurator tolerates the bounded control-plane propagation delay after a
new advertisement and requires the exact HTTPS listeners and upstream map
before publishing browser readiness.

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
`https://vonk-forge.<TAILNET_DNS_SUFFIX>.ts.net/`. Copy the suffix from the
Tailscale DNS page; do not invent it. This is also the exact control hostname
entered during bundle preparation.

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

From the NAS project directory, first verify containers and collect bounded,
non-secret diagnostics:

```bash
docker compose ps --all
docker compose logs --no-color --tail 100 tailscale-gateway tailscale-configurator
```

Both Tailscale containers must be running and healthy. Then inspect the
tailnet-facing state:

```bash
docker compose exec tailscale-gateway tailscale status --json
docker compose exec tailscale-gateway tailscale serve status --json
docker compose logs --since 30m tailscale-configurator
```

`Self.PrimaryRoutes` must contain each mapped Service IPv4 as `/32` and IPv6 as
`/128`. An approved `service-host` mapping without those routes is fail-closed,
not ready.

From a separate authorized Tailscale-connected workstation, verify MagicDNS,
the Service route, TLS, and the application health endpoint:

```bash
tailscale dns status
tailscale ping vonk-forge.<TAILNET_DNS_SUFFIX>.ts.net
curl --fail --show-error --silent \
  https://vonk-forge.<TAILNET_DNS_SUFFIX>.ts.net/healthz \
  --output /dev/null
```

Then open the same HTTPS origin in a browser and complete the separate Vonk
Forge administrator login. A same-host container probe does not replace this
independent client check.

If the name does not resolve but the Services page shows an online host, use
its displayed TailVIP only to isolate the fault:

```bash
curl --fail --show-error --silent \
  --resolve vonk-forge.<TAILNET_DNS_SUFFIX>.ts.net:443:<DISPLAYED_TAILVIP> \
  https://vonk-forge.<TAILNET_DNS_SUFFIX>.ts.net/healthz \
  --output /dev/null
```

If this succeeds, repair the workstation's Tailscale client or DNS integration;
do not change Caddy, the certificate, Service identity, or NAS ports.

Hermes-disabled installs must report exactly `svc:vonk-forge` with `HTTPS:
true`, never `HTTP: true`, and only the `http://caddy:8080` upstream.
Hermes-enabled installs must report exactly all three Services, each with
`HTTPS: true`, never `HTTP: true`, and exactly the three upstreams above. Test
the control URL from an authorized Tailscale-connected client. With Hermes
enabled, also test dashboard and API reachability as an authorized
GitHub-backed user, then confirm a user outside `group:hermes-users` is denied.
Even an authorized user must supply the separate Hermes key to invoke the API.
Confirm an ordinary LAN client cannot reach either Hermes endpoint.

## Diagnosis by symptom

| Symptom | Meaning | Action |
| --- | --- | --- |
| Service shows `0 hosts` | The definition exists, but no gateway currently advertises it. | Confirm the gateway is online, the Service name is exact and unsuffixed, and the configurator is healthy. Do not create a second similarly named Service. |
| Gateway advertisement is pending | Policy did not auto-approve this exact tag/Service pair. | Fix `autoApprovers.services` or approve that one advertisement, then repair policy so recovery is unattended. |
| Client reports `no matching peer` | DNS returned a TailVIP, but no active gateway owns its primary route. | Inspect `Self.PrimaryRoutes`; add the exact `tag:vonk-gateway` TCP 443 self-access grant and wait for reconciliation. |
| `service-host` exists but `PrimaryRoutes` is empty or incomplete | Approval exists, but routing is fail-closed. | Check the self-access grant for every hosted Service; do not recreate the node or enable Funnel. |
| Service hostname does not resolve | MagicDNS is off, the Service is undefined, the suffix/hostname is mistyped, or the client is not connected to the tailnet. | Recheck DNS settings, the exact Service definition, the copied tailnet suffix, and client Tailscale status. Never add a hosts-file entry. |
| HTTPS returns `421` | The installer control hostname does not equal the canonical Service FQDN. | Regenerate or repair the bundle with `vonk-forge.<TAILNET_DNS_SUFFIX>.ts.net`; do not weaken Caddy host checks. |
| The wrong login or backend answers | Another gateway is advertising the same canonical Service. | Find and drain the stale gateway, confirm one intended online host, and recheck `PrimaryRoutes`; do not create another Service name. |
| Gateway remains unhealthy before any Service appears | Enrollment failed before advertisement. | Check that the OAuth values are raw, current, limited to `auth_keys` write, and tagged only `tag:vonk-gateway`; then inspect the bounded gateway/configurator logs above. |

Do not solve any of these states by creating another Service in the operator
tailnet. That creates a second identity rather than repairing the canonical one.

## Drain, revocation, and recovery

For a full host drain, withdraw GPU node routes and human access, complete the
NAS platform's encrypted volume backup, then stop the Docker host through its
normal OS shutdown procedure. Back up `tailscale-state` and the OAuth files
with the control database and Hermes state. Restore state before startup when
possible.

If state cannot be restored, recreate the project with the OAuth files. Verify
exactly one current tagged node advertises the one development Service or all
three production Services, as appropriate, and revoke the orphan. For
compromise, revoke OAuth, the node, and its tag/Service approvals; for
development, capture both replacement values and run the documented
`--rotate-tailscale-oauth` transaction with one stable non-secret UUIDv4
`--tailscale-oauth-rotation-id` before republishing and choosing
**Pull** then **Redeploy** with every named volume preserved. Never add a
temporary LAN human endpoint.
