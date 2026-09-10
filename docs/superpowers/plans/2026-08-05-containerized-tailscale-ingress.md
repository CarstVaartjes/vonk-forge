# Containerized Tailscale Ingress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put all human-facing Vonk Forge HTTPS access behind one containerized Tailscale Service while retaining a separate, source-restricted LAN TLS edge for GPU node enrollment, mTLS agent traffic, and registry pulls.

**Architecture:** The root Compose application includes a focused Tailscale model. An official digest-pinned gateway runs userspace networking with persisted state and file-backed OAuth credentials; a short-lived configurator applies and advertises a declarative Service map. Caddy gets an internal HTTP listener for tailnet traffic and a dedicated TLS backend listener published only on the reserved NAS LAN address.

**Tech Stack:** Docker Compose include, official Tailscale container, Tailscale Services huJSON, OAuth client credentials, Caddy 2, pytest, Docker Compose JSON rendering.

## Global Constraints

- Preserve one Compose project and `deploy/compose/compose.yaml` as the normal entry point.
- Tailscale human ingress publishes no Docker host ports and has no LAN fallback.
- Use Tailscale userspace networking; do not grant `/dev/net/tun`, `NET_ADMIN`, or `NET_RAW`.
- Store OAuth client ID and secret in file-backed Compose secrets, never `.env` values.
- Persist Tailscale state and request a non-ephemeral tagged identity with `TS_AUTH_ONCE=true`.
- Auto-approve only the named Vonk Forge Services for `tag:vonk-gateway`; never use `svc:*`.
- The only host-published port is Caddy's backend TLS port bound to `NAS_LAN_IP`.
- Human routes must not be available on the backend listener.
- Keep Caddy, LiteLLM, and internal control-plane network boundaries intact.

---

### Task 1: Add the modular Tailscale Compose component

**Files:**
- Create: `deploy/compose/tailscale/compose.yaml`
- Create: `deploy/compose/tailscale/configure.sh`
- Create: `deploy/compose/tailscale/serve.json`
- Create: `deploy/compose/tailscale/grants.example.hujson`
- Modify: `deploy/compose/compose.yaml`
- Modify: `deploy/compose/.env.example`
- Modify: `deploy/compose/images.lock.json`
- Create: `deploy/compose/tests/test_tailscale.py`
- Modify: `deploy/compose/tests/test_networking.py`

**Interfaces:**
- Produces services `tailscale-gateway` and `tailscale-configurator`.
- Produces networks `tailnet-web-edge` and `tailnet-ssh-edge`.
- Produces named Service `svc:vonk-forge` at `tcp:443` targeting `http://caddy:8080`.
- Consumes secret files `TAILSCALE_OAUTH_CLIENT_ID_FILE` and `TAILSCALE_OAUTH_CLIENT_SECRET_FILE`.

- [ ] **Step 1: Write failing rendered-Compose tests**

Extend the render environment with a digest-pinned `TAILSCALE_IMAGE`, OAuth secret paths, `NAS_LAN_IP=192.0.2.10`, `VONK_MANAGEMENT_CIDRS=192.0.2.0/24`, and `VONK_BACKEND_PORT=8443`. Assert the two Tailscale services exist, the gateway has no `ports`, `devices`, or `cap_add`, uses `read_only: true`, joins only both tailnet edge networks, mounts persistent state, and reads OAuth values from `/run/secrets`. Assert the configurator shares the gateway network namespace and socket volume.

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest deploy/compose/tests/test_tailscale.py deploy/compose/tests/test_networking.py -v`

Expected: FAIL because the included Tailscale model does not exist.

- [ ] **Step 3: Resolve and pin the current stable image**

Resolve `tailscale/tailscale:v1.98.8` and record its linux/arm64-capable manifest-list digest `sha256:d54b2e6a9c09f0e5ec52e82b9ad4af3d446b54a7c08075e92f11c39dd410105f` in `.env.example` and `images.lock.json`. Do not use `latest` and do not copy a platform-child digest as the multi-platform pin.

- [ ] **Step 4: Implement the Tailscale Compose model**

Configure `TS_STATE_DIR=/var/lib/tailscale`, `TS_AUTH_ONCE=true`, `TS_USERSPACE=true`, `TS_SOCKET=/var/run/tailscale/tailscaled.sock`, `TS_CLIENT_ID=file:/run/secrets/tailscale-oauth-client-id`, `TS_CLIENT_SECRET=file:/run/secrets/tailscale-oauth-client-secret`, and `TS_EXTRA_ARGS=--advertise-tags=tag:vonk-gateway`. Mount named volumes for state and the shared local API socket. Use bounded logging, a read-only root, bounded `/tmp`, and a health check based on `tailscale status --json`.

- [ ] **Step 5: Implement idempotent Service configuration**

`configure.sh` waits up to 120 seconds for the local API socket and a successful status, applies `/config/serve.json` with `tailscale serve set-config --all`, advertises only `svc:vonk-forge`, verifies the service-host capability in JSON status, and exits non-zero on any failure. The configuration file uses version `0.0.1` and exactly `"tcp:443": "http://caddy:8080"`.

- [ ] **Step 6: Add least-privilege tailnet policy example**

Define `tagOwners` for `tag:vonk-gateway`, a grant from `autogroup:admin` to `svc:vonk-forge` on `tcp:443`, an exact `autoApprovers.services` entry for `svc:vonk-forge`, and positive/negative access tests. Do not include credentials, tailnet IDs, email addresses, wildcard Services, or default-allow policy.

- [ ] **Step 7: Include the component from the root model**

Add top-level `include: [tailscale/compose.yaml]`. Attach Caddy to `tailnet-web-edge`. Update every existing Compose render fixture with the new mandatory non-secret and secret-file inputs.

- [ ] **Step 8: Run focused tests**

Run: `uv run pytest deploy/compose/tests/test_tailscale.py deploy/compose/tests/test_networking.py -q && docker compose --env-file deploy/compose/tests/test.env -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml config --quiet`

Expected: PASS.

- [ ] **Step 9: Commit the gateway component**

```bash
git add deploy/compose/compose.yaml deploy/compose/.env.example deploy/compose/images.lock.json deploy/compose/tailscale deploy/compose/tests
git commit -m "feat: add containerized Tailscale gateway"
```

### Task 2: Split Caddy's tailnet and GPU node LAN listeners

**Files:**
- Modify: `deploy/compose/Caddyfile`
- Modify: `deploy/compose/compose.yaml`
- Modify: `deploy/compose/tests/test_networking.py`
- Modify: `deploy/compose/tests/test_agent_ingress.py`

**Interfaces:**
- Produces: internal HTTP listener `:8080` reachable only on `tailnet-web-edge`.
- Produces: TLS backend listener `${VONK_BACKEND_PORT:-8443}` published as `${NAS_LAN_IP}:${VONK_BACKEND_PORT}:${VONK_BACKEND_PORT}`.
- Consumes: `X-Vonk-Agent-Source` contract from the dynamic-endpoint plan.

- [ ] **Step 1: Add failing listener-isolation tests**

Render Compose and adapt Caddy JSON. Assert the control/LiteLLM/Grafana routes exist only on port 8080, enrollment/agent/registry SNI routes exist only on port 8443, and Docker publishes only `192.0.2.10:8443`. Assert no control hostname, `/v1/*`, or `/grafana/*` route exists on the backend server.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run pytest deploy/compose/tests/test_networking.py deploy/compose/tests/test_agent_ingress.py -v`

Expected: FAIL because Caddy still serves every SNI on the published HTTPS listener.

- [ ] **Step 3: Refactor reusable route snippets**

Keep `edge_guards` and `control_proxy`. Add a reusable human-route snippet imported only by `:8080`; it denies `/agent/*`, proxies `/v1/*` to LiteLLM, `/grafana/*` to Grafana, and the fallback to control API. Move enrollment, agent, and registry sites to explicit `https://{$HOST}:{$VONK_BACKEND_PORT}` addresses.

- [ ] **Step 4: Bind only the backend port on the NAS LAN IP**

Replace `HTTPS_BIND` with `NAS_LAN_IP` and `VONK_BACKEND_PORT`. Publish the long-form host-IP mapping or quoted short mapping and keep the internal 8080 listener unpublished. Pass `VONK_BACKEND_PORT` into Caddy.

- [ ] **Step 5: Add the trusted source-address header**

On the verified mTLS agent reverse proxy, delete all caller-supplied `X-Vonk-Agent-*` headers, then set existing identity headers plus `X-Vonk-Agent-Source {http.request.remote.host}`. Do not set this header on enrollment, human, or registry routes.

- [ ] **Step 6: Run focused and full Compose tests**

Run: `uv run pytest deploy/compose/tests -q && docker compose --env-file deploy/compose/tests/test.env -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml config --quiet`

Expected: PASS.

- [ ] **Step 7: Commit listener separation**

```bash
git add deploy/compose/Caddyfile deploy/compose/compose.yaml deploy/compose/tests/test_networking.py deploy/compose/tests/test_agent_ingress.py
git commit -m "feat: separate tailnet and GPU node ingress"
```

### Task 3: Add recovery and operations documentation

**Files:**
- Create: `deploy/compose/tailscale/README.md`
- Create: `docs/runbooks/tailscale.md`
- Modify: `docs/runbooks/control-plane-bootstrap.md`
- Modify: `docs/runbooks/control-plane-recovery.md`
- Modify: `docs/runbooks/agent-pki.md`
- Modify: `docs/security/threat-model.md`

**Interfaces:**
- Documents: GitHub-backed human login versus the separate Tailscale OAuth client.
- Documents: exact DNS, DHCP, firewall, Services, grants, and auto-approval inputs.

- [ ] **Step 1: Write the operator runbook**

Document creation of `svc:vonk-forge`, `tag:vonk-gateway`, an OAuth client limited to auth-key creation for that tag, file modes for client ID/secret, exact service auto-approval, access-control tests, state backup, revocation, drain, restore, and `tailscale serve status --json` verification. State explicitly that GitHub credentials are never placed in Compose.

- [ ] **Step 2: Update bootstrap and PKI paths**

Document the three local DNS records sharing the reserved NAS address, backend port 8443, firewall restriction to management CIDRs, GPU node trust of the Caddy backend certificate, and tailnet-only human hostname. Remove instructions that publish user HTTPS on `0.0.0.0:443`.

- [ ] **Step 3: Update recovery and threat boundaries**

Document persisted Tailscale state, OAuth re-enrollment, exact Service auto-approval, node/tag revocation, failure-closed behavior, and that `docker compose down` removes human ingress.

- [ ] **Step 4: Verify documentation and configuration**

Run: `uv run pytest deploy/compose/tests -q && git diff --check`

Expected: PASS.

- [ ] **Step 5: Commit documentation**

```bash
git add deploy/compose/tailscale/README.md docs/runbooks/tailscale.md docs/runbooks/control-plane-bootstrap.md docs/runbooks/control-plane-recovery.md docs/runbooks/agent-pki.md docs/security/threat-model.md
git commit -m "docs: operate tailnet-only NAS ingress"
```
