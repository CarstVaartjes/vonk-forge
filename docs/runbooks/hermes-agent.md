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

## Prepare Hermes

Run the NAS curl installer and select Hermes when prompted. The installer asks
for the exact HTTPS dashboard origin and creates the API key, immutable image
selection, and persistent named volumes inside the upload directory. There is
no host preparation, privileged helper, firewall script, or separate setup
container.

Hermes has no general network egress. Its only networks are the internal
dashboard/API path to the Tailscale gateway and the internal inference path to
Caddy. The origin must be the one exact HTTPS origin shown by Tailscale; do not
use `*`, HTTP, or a fallback list.

## Enable Hermes

Provision a dedicated LiteLLM client key for Hermes; never use the LiteLLM
master key. Re-run the NAS curl installer, select Hermes, and replace the upload
directory on the NAS. The generated directory contains the immutable Hermes
image, its dedicated keys, and its persistent named volumes.

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
docker compose ps
docker compose logs --since 30m hermes-agent tailscale-configurator
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

Use the NAS platform's supported encrypted volume backup for Hermes `data` and
`workspaces`; cache is disposable. Back up the generated `secrets` directory
and Tailscale state with the same snapshot. Restore those volumes and files
before starting Compose. Fresh GPU node presence and a new LiteLLM lease are
required; restored routes do not become live merely because they existed in a
backup.

If Tailscale state is lost, the scoped OAuth client performs unattended tagged
re-enrollment. Verify the replacement and revoke the orphan. Loss of Hermes
`data` is identity/state loss and requires setup again. Never open a temporary
LAN port or enable a cloud model to work around recovery.
