# Route Serving Lease Authority Implementation Plan

**Status:** Complete. The first review findings were fixed in
`27db102..f9182b3`; follow-up review of `de248c2..957e460` was clean.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make route expiry an exact request-admission rule enforced by Caddy,
with no direct network or host-port path around the lease authority.

**Architecture:** The LiteLLM supervisor exposes an internal authorization
endpoint backed by an atomic lease snapshot. Caddy uses `forward_auth` for all
browser and internal inference requests, and a dedicated internal Docker
network makes Caddy the only service able to reach LiteLLM. Process termination
remains best-effort cleanup.

**Tech Stack:** Python 3.12 standard library, Caddy 2.11.4, Docker Compose,
pytest, Docker-based integration tests.

## Global Constraints

- The execution-harness catalog remains v1; migration
  `0027_execution_harness_catalog` remains an exact fresh pre-production schema
  fence with no legacy data translation or compatibility reader.
- A request whose authorization check begins at or after the exact UTC
  `expires_at` must not be forwarded to LiteLLM.
- A request admitted before expiry may finish; timer-driven child termination
  remains cleanup and is not the admission authority.
- The authority returns only `204`, `503`, or `404`; it binds the exact active
  generation, activation digest, LiteLLM digest, and expiry.
- Same-configuration renewal must replace the authority snapshot and rearm
  cleanup before the renewed generation is acknowledged.
- LiteLLM port `4000` and authority port `4001` are never host-published in
  production; development's loopback inference port terminates at Caddy.
- Only Caddy and LiteLLM share the internal `litellm-edge` network. Hermes and
  other clients cannot resolve or dial LiteLLM directly.
- Preserve all DS4/Mia source, model, image, recipe, distribution, and evidence
  identities established by Task 8.
- Physical ARM64/GPU/two-Spark/RoCE acceptance remains Task 9.

---

### Task 1: Add an atomic supervisor lease authority

**Files:**
- Modify: `deploy/compose/litellm/config_supervisor.py`
- Modify: `control/src/vonk_control/resources/dev/litellm-supervisor.py`
- Modify: `deploy/compose/tests/test_litellm_supervisor.py`
- Modify: `control/tests/test_dev_runtime_assets.py`

**Interfaces:**
- Consumes: validated `ActiveRequest` values and the supervisor's child-health
  result.
- Produces: `_RouteLeaseAuthority` with `deny()`, `allow_bootstrap()`,
  `activate(request)`, and `authorized(now)`; an internal
  `GET /vonk/route-lease` endpoint on port `4001`; renewable
  `_ServingLeaseGuard` cleanup.

- [ ] **Step 1: Write failing authority-boundary tests**

Add tests that construct the production supervisor authority and assert:

```python
authority.deny()
assert authority.authorized(now) is False
authority.allow_bootstrap()
assert authority.authorized(now) is True
authority.activate(request_expires_one_second_later)
assert authority.authorized(expires_at - timedelta(microseconds=1)) is True
assert authority.authorized(expires_at) is False
```

Also start the real standard-library authority server on a loopback ephemeral
port and assert `GET /vonk/route-lease` returns `204` only for authorized state,
`503` for denied/expired state, and `404` for a different path or method.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --project control --frozen python -m pytest -q \
  deploy/compose/tests/test_litellm_supervisor.py \
  -k 'route_lease_authority or route_lease_http'
```

Expected: FAIL because the request-time authority and HTTP endpoint do not
exist.

- [ ] **Step 3: Implement immutable authority snapshots**

Use a frozen snapshot and one lock. Bootstrap and active states are explicit;
denied is the default. `authorized(now)` must take an aware `datetime`, compare
active state using `now < expires_at`, and reject malformed/missing values. The
HTTP handler must suppress server banners and logs, send `Cache-Control:
no-store`, and never echo route metadata.

- [ ] **Step 4: Write and verify the same-config renewal RED**

Add a supervisor-loop test with one child/config and two valid requests. The
second request has the same config bytes/digest but a later generation and
expiry. Advance beyond the old expiry and assert the authority remains allowed,
the child was not restarted, the acknowledgement names the new generation, and
the cleanup timer is bound to the new expiry.

Run the named test and confirm it fails against the first implementation until
renewal is wired into the unchanged-config branch.

- [ ] **Step 5: Wire lifecycle ordering and renewable cleanup**

Start the authority server once in `main()`. Keep it denied through startup.
After child health, allow bootstrap or activate the exact request before active
acknowledgement. On an unchanged-config active candidate, replace the snapshot,
cancel/rearm cleanup, then write acknowledgement. On reload, invalid candidate,
unhealthy child, shutdown, or expiry, deny before clearing acknowledgement or
attempting process cleanup.

- [ ] **Step 6: Prove production/development behavioral parity**

Run the same authority and renewal vectors against both supervisor modules.
Keep image-specific command/materialization differences outside the shared
behavior assertions.

- [ ] **Step 7: Run Task 1 verification and commit**

```bash
uv run --project control --frozen python -m pytest -q \
  deploy/compose/tests/test_litellm_supervisor.py \
  control/tests/test_dev_runtime_assets.py
uvx --from ruff==0.16.1 ruff check \
  deploy/compose/litellm/config_supervisor.py \
  control/src/vonk_control/resources/dev/litellm-supervisor.py \
  deploy/compose/tests/test_litellm_supervisor.py \
  control/tests/test_dev_runtime_assets.py
uvx --from ruff==0.16.1 ruff format --check \
  deploy/compose/litellm/config_supervisor.py \
  control/src/vonk_control/resources/dev/litellm-supervisor.py \
  deploy/compose/tests/test_litellm_supervisor.py \
  control/tests/test_dev_runtime_assets.py
git diff --check
git add deploy/compose/litellm/config_supervisor.py \
  control/src/vonk_control/resources/dev/litellm-supervisor.py \
  deploy/compose/tests/test_litellm_supervisor.py \
  control/tests/test_dev_runtime_assets.py
git commit -m "fix: make route leases request authoritative"
```

### Task 2: Put every inference path behind Caddy

**Files:**
- Modify: `deploy/compose/Caddyfile`
- Modify: `control/src/vonk_control/resources/dev/Caddyfile`
- Modify: `deploy/compose/compose.yaml`
- Modify: `deploy/compose/compose.dev.images.yaml`
- Modify: `deploy/compose/hermes-agent/compose.yaml`
- Create: `deploy/compose/tests/test_litellm_lease_edge.py`
- Modify: `deploy/compose/tests/test_litellm_admin.py`
- Modify: `deploy/compose/tests/test_networking.py`
- Modify: `deploy/compose/tests/test_dev_compose.py`
- Modify: `deploy/compose/tests/test_dev_complete_stack.py`
- Modify: `deploy/compose/tests/test_hermes_agent.py`
- Modify: `deploy/compose/tests/test_agent_ingress.py`
- Modify: `scripts/render-dev-compose`
- Modify: `scripts/tests/test_render_dev_compose.py`

**Interfaces:**
- Consumes: `GET http://litellm:4001/vonk/route-lease` from Task 1.
- Produces: `litellm_route_lease` Caddy snippet; internal
  `http://caddy:8081/v1`; internal `litellm-edge` network; loopback development
  inference mapping owned by Caddy.

- [ ] **Step 1: Write failing topology tests**

Assert both Caddyfiles authorize before proxying `/v1/*` and `/litellm/*`, and
define an internal `:8081` listener that accepts only `/v1/*`. Render both
Compose models and assert:

```python
assert services["litellm"].get("ports") in (None, [])
assert caddy_owns_loopback_inference_port
assert set(network_members["litellm-edge"]) == {"caddy", "litellm"}
assert "hermes-inference" not in services["litellm"]["networks"]
assert hermes_environment["OPENAI_BASE_URL"] == "http://caddy:8081/v1"
```

- [ ] **Step 2: Run topology tests and verify RED**

```bash
uv run --project control --frozen python -m pytest -q \
  deploy/compose/tests/test_litellm_admin.py \
  deploy/compose/tests/test_networking.py \
  deploy/compose/tests/test_dev_compose.py \
  deploy/compose/tests/test_dev_complete_stack.py \
  deploy/compose/tests/test_hermes_agent.py \
  deploy/compose/tests/test_agent_ingress.py \
  scripts/tests/test_render_dev_compose.py
```

Expected: FAIL on direct LiteLLM paths, missing authority, and missing network.

- [ ] **Step 3: Implement Caddy authorization and network isolation**

Add the shared forward-auth snippet to both Caddyfiles and import it immediately
before each LiteLLM reverse proxy. Add the internal listener. Add an internal
`litellm-edge` network, join only Caddy and LiteLLM, remove LiteLLM from ingress
and Hermes networks, and join Caddy to the Hermes network. Point Hermes at
Caddy. Move the development loopback inference mapping from LiteLLM `4000` to
Caddy `8081`; preserve `VONK_DEV_INFERENCE_PORT` as the operator-facing knob.

- [ ] **Step 4: Write a real Caddy fail-closed integration test**

The test starts the production `_RouteLeaseAuthority` server and an independent
HTTP upstream on host loopback. Run the exact pinned Caddy image with host
networking and a generated Caddyfile using real `forward_auth`. Prove:

1. authorized/bootstrap request reaches upstream and returns its `200`;
2. exact-at/after-expiry request returns non-`2xx` while the upstream process is
   still alive and its request counter does not advance;
3. authority shutdown returns non-`2xx` and does not reach upstream;
4. same-config renewal admits requests after the old deadline and denies at the
   renewed deadline.

Do not stub Caddy, the HTTP authority, or the upstream request counter. Use
bounded startup and teardown; fail rather than silently skip when Docker is
available but the path is broken.

- [ ] **Step 5: Run the real integration and rendered-Compose tests**

```bash
uv run --project control --frozen python -m pytest -q \
  deploy/compose/tests/test_litellm_lease_edge.py \
  deploy/compose/tests/test_litellm_admin.py \
  deploy/compose/tests/test_networking.py \
  deploy/compose/tests/test_dev_compose.py \
  deploy/compose/tests/test_dev_complete_stack.py \
  deploy/compose/tests/test_hermes_agent.py \
  deploy/compose/tests/test_agent_ingress.py \
  scripts/tests/test_render_dev_compose.py
```

Expected: PASS with a real post-expiry Caddy denial and no direct LiteLLM
published port or client network.

- [ ] **Step 6: Run format/config gates and commit**

```bash
validation_root=$(mktemp -d)
trap 'rm -rf -- "$validation_root"' EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj '/CN=vonk-caddy-validation' \
  -keyout "$validation_root/ca-key.pem" \
  -out "$validation_root/ca.pem" >/dev/null 2>&1
docker run --rm -v "$PWD/deploy/compose/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -e VONK_CONTROL_HOSTNAME=control.test.example \
  -v "$validation_root/ca.pem:/run/secrets/agent-client-ca:ro" \
  caddy:2.11.4@sha256:844f60b64e4724a5aa8245e019dace0d3f199f7433ce6c57676cb30a920dbad9 \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
uvx --from ruff==0.16.1 ruff check deploy/compose/tests scripts/tests
uvx --from ruff==0.16.1 ruff format --check deploy/compose/tests scripts/tests
git diff --check
git add deploy/compose control/src/vonk_control/resources/dev/Caddyfile \
  scripts/render-dev-compose scripts/tests/test_render_dev_compose.py
git commit -m "fix: enforce route leases at every inference edge"
```

### Task 3: Close the Task 8 gate and update operator guidance

**Files:**
- Modify: `control/src/vonk_control/dev_cohort.py`
- Modify: `control/src/vonk_control/dev_init.py`
- Modify: `deploy/compose/compose.dev.yaml`
- Modify: `scripts/tests/test_dev_image_acceptance.py`
- Modify: `scripts/tests/test_verify_dev_image_secrets.py`
- Modify: `deploy/compose/tests/test_dev_compose.py`
- Modify: `deploy/compose/tests/test_dev_complete_stack.py`
- Modify: `docs/runbooks/development-nas-installation.md`
- Modify: `docs/runbooks/development-agent-workloads.md`
- Modify: `docs/runbooks/fresh-development-install.md`
- Modify: `docs/runbooks/hermes-agent.md`
- Modify: `docs/superpowers/plans/2026-08-15-execution-harness-foundation.md`
- Modify: `.superpowers/sdd/2026-08-15-execution-harness-foundation/progress.md`
- Modify: `.superpowers/sdd/2026-08-15-execution-harness-foundation/task-8-report.md`

**Interfaces:**
- Consumes: reviewed request-time lease boundary from Tasks 1-2.
- Produces: accurate fresh-install/tunnel/Hermes guidance and a reopened Task 8
  completion gate before Task 9.

- [ ] **Step 1: Prove and fix one fresh development database identity**

Write/extend the development identity contract so Alembic's actual single head,
`DEVELOPMENT_DATABASE_REVISION`, `_DATABASE_REVISION`, source development
Compose, development-image acceptance fixtures, and secret-verifier fixtures
must all equal `0027_execution_harness_catalog`. Run it first and observe the
existing `0021_browser_authentication` mismatch. Replace those stale constants
directly; do not add an alias, compatibility reader, or data translation.

Run:

```bash
uv run --project control --frozen python -m pytest -q \
  deploy/compose/tests/test_dev_compose.py \
  control/tests/test_dev_init.py \
  control/tests/test_dev_cohort.py \
  scripts/tests/test_dev_image_acceptance.py \
  scripts/tests/test_verify_dev_image_secrets.py \
  -k 'database_revision or alembic_head or image_identity'
```

Expected after edits: PASS with one exact fresh-schema identity.

- [ ] **Step 2: Rebuild branch-current local images and diagnose the full stack**

Build both current control targets before the image-only complete-stack test so
stale `dev-local` images cannot masquerade as a source failure:

```bash
docker build -f control/Dockerfile --target api \
  --build-arg VONK_DEV_SOURCE_COMMIT="$(git rev-parse HEAD)" \
  -t vonk-forge-api:dev-local .
docker build -f control/Dockerfile --target worker \
  --build-arg VONK_DEV_SOURCE_COMMIT="$(git rev-parse HEAD)" \
  -t vonk-forge-worker:dev-local .
scripts/verify-dev-image-secrets \
  vonk-forge-api:dev-local vonk-forge-worker:dev-local
uv run --project control --frozen python -m pytest -q \
  deploy/compose/tests/test_dev_complete_stack.py
```

Expected: PASS. If repository initialization still fails, capture its exact
container output, identify the source/image identity boundary that differs,
write a focused failing regression, and fix the root cause before continuing.

- [ ] **Step 3: Write failing documentation contracts**

Update documentation tests to require that the operator-facing loopback port is
Caddy-gated, Hermes uses `caddy:8081`, no guide recommends direct LiteLLM
access, and the clean-reset language says users/sessions/enrollments are
recreated.

- [ ] **Step 4: Update runbooks and architecture wording**

Keep the existing SSH tunnel port and Pi/OpenAI-compatible URL instructions,
but state that the port terminates at Caddy's lease gate. Replace internal
Hermes URLs with `http://caddy:8081/v1`. Document the admission guarantee,
fail-closed authority outage, and same-config renewal behavior. Do not add any
migration or legacy preservation instructions.

- [ ] **Step 5: Run retained Task 8 and documentation suites**

```bash
uv run --project control --frozen python -m pytest -q \
  control/tests/test_recipe_operations.py control/tests/test_recipe_routes.py \
  control/tests/test_recipe_builds.py control/tests/test_source_policy.py \
  control/tests/test_development_recipe_fixture.py \
  control/tests/test_builtin_harnesses.py \
  control/tests/test_recipe_runtime_specs.py \
  control/tests/test_distributed_lifecycle.py \
  control/tests/test_development_catalog.py \
  deploy/compose/tests/test_litellm_supervisor.py \
  deploy/compose/tests/test_litellm_lease_edge.py \
  scripts/tests/test_qualify_recipe.py \
  scripts/tests/test_recipe_source_bundle.py \
  scripts/tests/test_native_development_entrypoints.py \
  tests/recipes/test_deepseek_v4_flash_ds4.py \
  tests/recipes/test_mia_deepseek_v4_flash.py \
  tests/runbooks/test_development_nas_installation.py \
  tests/test_docs_contract.py
cargo test -p vonk-agent-protocol -p vonk-agent \
  -p vonk-agent-helper --all-targets
uv run --project agent_protocol --frozen python -m pytest -q agent_protocol/tests
uv run --project control --frozen python -m pytest -q \
  scripts/tests/test_run_development_slices.py
uv run --project control --frozen python -m pytest -q \
  control/tests/security/test_agent_protocol.py \
  tests/scripts/test_verify_supply_chain.py
scripts/verify-supply-chain --json
git diff --check
```

- [ ] **Step 6: Record evidence, commit, and request independent review**

Append exact RED/GREEN commands and outputs to the Task 8 report. Record the
new focused-task commits in the existing SDD ledger, then generate one review
package from `3642c36` through the new head. The reviewer must verify the two
breaker findings, Caddy/network bypass closure, same-config renewal, and no
new Critical/Important breakage.

```bash
git add docs
git commit -m "docs: document the route lease edge"
```

- [ ] **Step 7: Resume Task 9 only after review is clean**

Mark Task 8 complete in the ledger, update the parent implementation plan, and
start the fresh destructive pre-production reset, administrator recreation,
Spark re-enrollment, and physical DS4/Mia acceptance. Do not claim physical
acceptance from local x86 tests.
