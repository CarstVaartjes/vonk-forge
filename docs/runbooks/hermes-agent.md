# Operate Hermes Agent

Hermes Agent runs inside the main NAS Compose project. It has no Docker-published
port, SSH server, Docker socket, host network, or control-plane credential.
Authorized users reach its dashboard and API through exact Tailscale HTTPS
Services. Hermes sends model requests only to Caddy on the private
`hermes-inference` network; Caddy forwards lease-authorized requests to LiteLLM
on `litellm-edge`. Hermes uses the fixed model alias `hermes-agent`, which is
created only by publishing an accepted v1 `RecipeRun` with that exact alias.

## Keep the identities separate

Three identities serve different purposes and never imply one another:

- the GitHub-backed Tailscale identity permits a person to reach the two named
  Services;
- the Hermes API key authenticates requests after they reach the gateway; and
- an optional repository credential permits Git operations from Hermes.

The Tailscale gateway receives neither the Hermes key nor a GitHub repository
token. Hermes receives neither Tailscale OAuth credentials nor a control-plane
administrator credential. Install a repository credential only when required,
scope it to the necessary repositories and actions, and store it under
`/opt/data` with owner-only permissions.

## Prepare persistent paths and the API key

Create the three writable trees and a separate secret file for the official
release runtime UID/GID `1100:1100`:

```bash
sudo install -d -m 0700 -o 1100 -g 1100 \
  /srv/vonk-forge/hermes/data \
  /srv/vonk-forge/hermes/workspaces \
  /srv/vonk-forge/hermes/cache
sudo install -d -m 0700 -o root -g root /srv/vonk-forge/secrets
sudo sh -c 'umask 077; openssl rand -base64 32 | tr "+/" "-_" | tr -d "=\n" > /srv/vonk-forge/secrets/hermes-api-key; printf "\n" >> /srv/vonk-forge/secrets/hermes-api-key'
sudo chown root:root /srv/vonk-forge/secrets/hermes-api-key
sudo chmod 0400 /srv/vonk-forge/secrets/hermes-api-key
```

Set these non-secret paths and values in the host-local `.env`:

```dotenv
HERMES_UID=1100
HERMES_GID=1100
HERMES_DATA_ROOT=/srv/vonk-forge/hermes
HERMES_API_KEY_FILE=/srv/vonk-forge/secrets/hermes-api-key
HERMES_DASHBOARD_ORIGIN=https://EXACT-SVC-HERMES-DASHBOARD-URL
```

GitHub Actions fixes the UID/GID when it builds the published Hermes wrapper;
official releases require `1100:1100`. They are not freely selectable runtime
settings, and operators must not rebuild an official release to change them.
The wrapper's fixed identity lets the upstream supervisor run without editing
`/etc/passwd` on the read-only root. The external API key file must remain
root-owned mode `0400`; the supervisor has no reason to grant it to the
unprivileged Hermes user because PID 1 injects only the value.

The origin is the one exact HTTPS origin shown by Tailscale. Do not use `*`,
HTTP, or a fallback list.

## Apply the one-off host egress boundary

Compose networks do not replace a host firewall. The hardening program is part
of the signed deployment bundle; never run the similarly named file from a Git
checkout. After the updater has selected a generation and Docker has created
the networks, resolve the immutable active generation, review the plan, apply
it once, and verify it:

```bash
active=$(sudo cat /srv/vonk-forge/control-host/active-generation)
hardener="/srv/vonk-forge/control-host/generations/$active/bin/harden-hermes-egress"
export COMPOSE_PROJECT_NAME=vonk-forge-control
export VONK_MANAGEMENT_CIDRS=10.0.0.0/24
export VONK_DIRECT_FABRIC_CIDRS=192.168.100.0/24,192.168.101.0/24
sudo --preserve-env=COMPOSE_PROJECT_NAME,VONK_MANAGEMENT_CIDRS,VONK_DIRECT_FABRIC_CIDRS \
  "$hardener" --check
sudo --preserve-env=COMPOSE_PROJECT_NAME,VONK_MANAGEMENT_CIDRS,VONK_DIRECT_FABRIC_CIDRS \
  "$hardener" --apply
sudo --preserve-env=COMPOSE_PROJECT_NAME,VONK_MANAGEMENT_CIDRS,VONK_DIRECT_FABRIC_CIDRS \
  "$hardener" --verify
```

The owned chain denies direct access from Hermes to GPU node management,
direct-fabric, link-local metadata, and sibling project networks. Docker DNS
and ordinary Internet tools remain available. The default action is the
non-mutating `--check`.

## Enable Hermes

Provision a dedicated LiteLLM client key for Hermes; never use the LiteLLM
master key. Run the NAS preparation command again, select Hermes when prompted,
and replace the upload directory on the NAS. The generated bundle contains the
immutable Hermes image, its dedicated key, and its persistent named volumes;
there is no setup container or separate mutation step.

Resolve, map, and install the exact recipe revision in the browser Library
workflow. Start it under a temporary alias, run its source, placement, health,
and canary gates, and only then stop the previous `hermes-agent` run and start
the accepted replacement with the exact alias `hermes-agent`. The route
publisher rejects duplicate aliases and does not synthesize a fallback group.

Configure the provider/model prompts as:

```text
OpenAI-compatible base URL: http://caddy:8081/v1
Model: hermes-agent
API key: the dedicated Hermes LiteLLM client key
```

Hermes reaches `caddy:8081/v1` over `hermes-inference`. Caddy authorizes every
inference request against the current route-serving lease, then reaches LiteLLM
over the dedicated internal `litellm-edge` network. Only Caddy and LiteLLM
share `litellm-edge`; Hermes cannot resolve or dial LiteLLM directly. Do not
configure a direct LiteLLM address or an ingress-network path.

Caddy evaluates the current route-serving lease at request admission. A
request whose Caddy authorization begins at or after lease expiry is never
forwarded to LiteLLM. If the supervisor authority is unavailable, Caddy fails
closed without contacting LiteLLM. A same-config lease renewal replaces the
deadline without restarting the healthy LiteLLM child.

Do not configure Nous Portal, OpenRouter, OpenAI, Anthropic, or another remote
model provider as a fallback. Hermes does not select models from Git, a
repository maturity report, or a hidden compatibility alias. If the exact
accepted `hermes-agent` run is not healthy and published, the alias is absent
and Hermes receives an unavailable response.

Setup state, sessions, memory, skills, logs, and provider configuration persist
under `data`. Repositories and output persist under `workspaces`. Cache is
disposable.

## Start and verify

```bash
sudo vonk-control-offline doctor
sudo vonk-control-offline maintenance status
sudo vonk-control-offline maintenance logs --service hermes-agent --since-minutes 30
sudo vonk-control-offline maintenance logs --service tailscale-configurator --since-minutes 30
```

Confirm Hermes and LiteLLM are healthy. Serve status must show HTTPS 443 for
`svc:hermes-dashboard` and `svc:hermes-api`, with exact upstreams
`hermes-agent:9119` and `hermes-agent:8642`. No LAN client should reach them.
An authorized tailnet user reaches the dashboard login and signs in as
`hermes` with the Hermes API key. API calls require the same key; absent and
incorrect keys must fail. The key is injected into dashboard authentication by
PID 1 and is not present in the rendered Compose environment. A model call must
identify only a selected local workload and never a cloud provider.

The opt-in Docker-host acceptance harness is:

```bash
bash deploy/compose/tests/hermes-agent-runtime.sh
```

It checks the read-only root, exact five-capability supervisor allowlist,
bounded mounts, gateway/dashboard health, persistence, and absence of the
Docker socket and private control networks. Physical tailnet authorization,
NAS firewall enforcement, and live GPU node inference remain deployment
acceptance checks.

## Backup and recovery

The root-owned `HostBackupBoundary` includes Hermes `data` and `workspaces` in
the authenticated encrypted upgrade archive and excludes cache. Back up the
external API-key file and Tailscale state/OAuth files in the same encrypted
off-host generation.

Journaled control-host recovery verifies the exact backup receipt before its
fixed boundary restores the selected Hermes trees with their configured
UID/GID and owner-only permissions. There is no repository restore script or
operator-supplied decryption command. Restore and verify the API-key file
separately, then let the updater start the selected generation. Fresh GPU node
presence and a new LiteLLM lease are required; restored routes do not become
live merely because they existed in a backup.

If Tailscale state is lost, the scoped OAuth client performs unattended tagged
re-enrollment. Verify the replacement and revoke the orphan. Loss of Hermes
`data` is identity/state loss and requires setup again. Never open a temporary
LAN port or enable a cloud model to work around recovery.
