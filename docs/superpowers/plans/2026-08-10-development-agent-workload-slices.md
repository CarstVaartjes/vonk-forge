# Development Agent and Workload Slices Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the public development NAS stack into a reproducible, secure two-node agent and real-workload validation environment while preserving production release boundaries.

**Architecture:** Extend the existing public `:dev` Compose cohort with file-projected development PKI, split enrollment/mTLS ingress through Caddy, and the existing acknowledged LiteLLM route supervisor. Keep workload execution on the existing source-first recipe path: a rootless Rust agent builds an exact OCI archive, the controller stores it, and target agents import and run it. Validate progressively with authenticated inventory, a deterministic synthetic recipe, then an audited real model on one and two DGX Sparks.

**Tech Stack:** Rust/Tokio/Reqwest, Python 3.12/FastAPI/SQLAlchemy, Docker Compose, Caddy 2.10, LiteLLM, PostgreSQL 18, rootless Podman, systemd, pytest, Cargo tests, GitHub Actions, PowerShell/WSL deployment.

## Global Constraints

- Production continues to use immutable selected releases, the host updater, step-ca, TUF, and release signers.
- The NAS project directory contains only `docker-compose.yml` and `secrets/`.
- Runtime secrets never enter Git, images, image labels, Compose environment values, CI artifacts, command output, or logs.
- `enrollment_url` is used only for pairing; `controller_url` is used only after certificate issuance.
- Accepted upstream state remains local `main`; API-authored development commits remain on local `deploy` with `refs/vonk/deploy-base` preserving update safety.
- Agent ingress is TCP 8443 and management-CIDR restricted; the human API remains loopback-only.
- Workload execution uses the existing source-first recipe engine with no synthetic-only production branch and no development registry.
- Mutable `:dev` publication and development APT publication occur only in GitHub Actions from accepted `main` history.
- Site addresses, hostnames, CIDRs, node IDs, secret locations, and model selections are validated inputs. Carst-specific values are examples, never product constants.
- Every implementation change starts with a failing test and ends with focused passing tests plus an intentional commit.

---

## File structure

New focused files:

- `control/src/vonk_control/dev_runtime_assets.py`: validate and stage Caddy/LiteLLM package resources with exact ownership.
- `control/src/vonk_control/resources/dev/Caddyfile`: development-only two-SNI agent ingress; no browser or registry routes.
- `control/src/vonk_control/resources/dev/caddy-entrypoint.sh`: read the proxy token from a file and start Caddy without exposing it in Compose.
- `control/src/vonk_control/resources/dev/litellm-bootstrap.json`: empty fail-closed LiteLLM bootstrap.
- `control/src/vonk_control/resources/dev/litellm-entrypoint.sh`: read three file secrets and start the supervisor.
- `control/src/vonk_control/resources/dev/litellm-supervisor.py`: packaged copy of the exact acknowledged route supervisor.
- `scripts/dev-runtime-secrets.py`: idempotently create/validate development PKI and random runtime files in a protected directory.
- `scripts/dev-runtime-project`: render/copy only the Compose artifact and validated secret files to a chosen local destination.
- `scripts/run-development-slices`: API-driven, restart-safe acceptance runner for inventory, synthetic recipe, and selected model recipes.
- `control/tests/fixtures/recipes/dev-http-smoke/`: canonical synthetic recipe document, source tree, and expected response.
- `docs/runbooks/development-agent-workloads.md`: complete slice execution, evidence, recovery, and cleanup guide.

Existing responsibility changes:

- `rust/crates/vonk-agent/src/config.rs`, `pair.rs`, `client.rs`, and `main.rs`: split pre-identity and post-identity controller origins.
- `packaging/config/agent.toml` and agent installation docs: ship the new mandatory configuration schema.
- `control/src/vonk_control/settings.py`: permit explicit built-in CA only in development and production, read CIDRs from a protected file, and keep provider settings fail closed.
- `control/src/vonk_control/dev_init.py`: create disjoint API/worker/migration/Caddy/LiteLLM projections and stage runtime assets.
- `deploy/compose/compose.dev.images.yaml`: wire Caddy, LiteLLM, dedicated volumes, secret files, and enabled agent services.
- `scripts/render-dev-compose`, `scripts/dev-compose-secrets.py`, and development image workflow tests: know the complete secret contract without publishing secret bytes.
- `.github/workflows/dev-images.yml`: continue rendering both mutable and pinned artifacts after the expanded complete-stack acceptance test.
- development NAS, PKI, onboarding, package, supply-chain, and architecture docs: present one reproducible generic contract.

---

### Task 1: Split Rust enrollment and controller origins

**Files:**
- Modify: `rust/crates/vonk-agent/src/config.rs`
- Modify: `rust/crates/vonk-agent/src/pair.rs`
- Modify: `rust/crates/vonk-agent/src/main.rs`
- Modify: `rust/crates/vonk-agent/tests/pairing.rs`
- Modify: `rust/crates/vonk-agent/tests/polling.rs`
- Modify: `packaging/config/agent.toml`
- Modify: `tests/test_agent_release_workflow.py`

**Interfaces:**
- Produces: `AgentConfig { enrollment_url: Url, controller_url: Url, ... }`.
- Produces: `pair(config, enrollment, token, ca_sha256, evidence)` validating `enrollment == config.enrollment_url`.
- Preserves: `AgentHttpClient::from_config` using only `config.controller_url`.

- [ ] **Step 1: Write failing parser and pairing tests**

Add parser cases equivalent to:

```rust
let config = AgentConfig::parse(&format!(
    "enrollment_url = \"https://enroll.vonk.test/\"\ncontroller_url = \"https://agents.vonk.test/\"\n{COMMON}"
)).unwrap();
assert_eq!(config.enrollment_url.as_str(), "https://enroll.vonk.test/");
assert_eq!(config.controller_url.as_str(), "https://agents.vonk.test/");
```

Add rejection tests for HTTP, userinfo, non-root paths, query/fragment, and a pairing CLI URL that equals `controller_url` but not `enrollment_url`.

- [ ] **Step 2: Prove the tests fail**

Run:

```bash
cargo test -p vonk-agent --test pairing --test polling
```

Expected: compile/test failure because `enrollment_url` does not exist and pairing still compares `controller_url`.

- [ ] **Step 3: Implement the split minimally**

Add `enrollment_url: Url`, factor canonical HTTPS-root validation into:

```rust
fn validate_origin(url: &Url, field: &'static str) -> Result<(), ConfigError>
```

Call it for both origins. Change only pairing to compare/use `enrollment_url`; leave the authenticated client on `controller_url`.

- [ ] **Step 4: Update the packaged schema and release assertion**

Set both invalid example URLs in `packaging/config/agent.toml`, and assert the built `.deb` contains both keys exactly once.

- [ ] **Step 5: Run focused verification**

```bash
cargo fmt --all -- --check
cargo clippy -p vonk-agent --all-targets -- -D warnings
cargo test -p vonk-agent --test pairing --test polling
uv run pytest tests/test_agent_release_workflow.py -q
```

- [ ] **Step 6: Commit**

```bash
git add rust/crates/vonk-agent packaging/config/agent.toml tests/test_agent_release_workflow.py
git commit -m "feat(agent): split enrollment and controller origins"
```

### Task 2: Enable the development agent authority fail closed

**Files:**
- Modify: `control/src/vonk_control/settings.py`
- Modify: `control/tests/test_settings.py`
- Modify: `deploy/compose/tests/test_agent_ingress.py`

**Interfaces:**
- Produces: `_secret_or_file("VONK_MANAGEMENT_CIDRS", "VONK_MANAGEMENT_CIDRS_FILE") -> str` with mutual exclusion.
- Changes: `agent_enabled = agent_runtime == "enabled" and mode in {"development", "production"}`.
- Preserves: production built-in CA requiring explicit bootstrap and production step-ca behavior.

- [ ] **Step 1: Add the complete setting matrix tests**

Cover these exact outcomes:

```python
@pytest.mark.parametrize(
    ("mode", "runtime", "provider", "bootstrap", "accepted"),
    [
        ("development", "disabled", "", "", True),
        ("development", "enabled", "builtin", "1", True),
        ("development", "enabled", "step-ca", "", False),
        ("development", "enabled", "", "", False),
        ("production", "enabled", "step-ca", "", True),
        ("production", "enabled", "builtin", "1", True),
        ("production", "enabled", "", "", False),
    ],
)
```

Also test CIDR file/env mutual exclusion, symlink rejection, empty file rejection, and overlap with direct-fabric CIDRs.

- [ ] **Step 2: Run tests and observe the development-enabled failures**

```bash
uv run --project control pytest control/tests/test_settings.py -q
```

- [ ] **Step 3: Implement explicit development enablement**

Require development runtime enablement to select `builtin` and bootstrap `1`; reject development step-ca. Load the client CA, intermediate certificate/key, proxy auth, and worker token whenever the runtime is enabled. Read management CIDRs from one protected regular file when the file variable is set.

While in this function, correct the duplicated concatenated default for `VONK_AGENT_TUF_TARGET_ROOT` to exactly `/state/agent-tuf/targets`, and add a regression assertion.

- [ ] **Step 4: Run focused settings and ingress tests**

```bash
uv run --project control pytest control/tests/test_settings.py deploy/compose/tests/test_agent_ingress.py -q
```

- [ ] **Step 5: Commit**

```bash
git add control/src/vonk_control/settings.py control/tests/test_settings.py deploy/compose/tests/test_agent_ingress.py
git commit -m "feat(control): enable explicit development agent authority"
```

### Task 3: Stage disjoint runtime secrets and packaged assets

**Files:**
- Create: `control/src/vonk_control/dev_runtime_assets.py`
- Create: `control/src/vonk_control/resources/dev/Caddyfile`
- Create: `control/src/vonk_control/resources/dev/caddy-entrypoint.sh`
- Create: `control/src/vonk_control/resources/dev/litellm-bootstrap.json`
- Create: `control/src/vonk_control/resources/dev/litellm-entrypoint.sh`
- Create: `control/src/vonk_control/resources/dev/litellm-supervisor.py`
- Modify: `control/pyproject.toml`
- Modify: `control/src/vonk_control/dev_init.py`
- Create: `control/tests/test_dev_runtime_assets.py`
- Modify: `control/tests/test_dev_init.py`

**Interfaces:**
- Produces: `stage_development_assets(source_package: str, destination: Path) -> None`.
- Produces projections with UIDs: API `10001:10001`, worker `10001:10001`, Caddy `10000:10000`, LiteLLM `10002:10001`.
- Consumes host files named in the design secret list and existing cohort identity.

- [ ] **Step 1: Add failing resource and projection tests**

Assert every resource is present through `importlib.resources`, byte-bounded, non-symlink, and staged atomically. Assert exact projected filenames/modes and that API-only bytes do not appear in worker, Caddy, migration, or LiteLLM roots.

Use distinct sentinel bytes per source and assert:

```python
assert not set(api_private_sentinels) & bytes_visible_to("worker")
assert agent_ca_key not in bytes_visible_to("caddy")
assert controller_server_key not in bytes_visible_to("api")
```

- [ ] **Step 2: Prove resource tests fail**

```bash
uv run --project control pytest control/tests/test_dev_runtime_assets.py control/tests/test_dev_init.py -q
```

- [ ] **Step 3: Package and stage deterministic assets**

Use `importlib.resources.files("vonk_control.resources.dev")`; validate every expected resource against a hard-coded filename allowlist, maximum size, and SHA-256 computed before writing. Stage to a temporary sibling, `fsync`, set exact mode/ownership, and atomically replace only regular files.

The development Caddyfile contains only enrollment and authenticated agent sites. It uses the existing header-scrubbing/mTLS contract and reads server TLS files explicitly. It does not expose control UI, LiteLLM, registry, or internal routes.

- [ ] **Step 4: Extend `dev_init` projections**

Add dedicated roots `VONK_DEV_CADDY_SECRET_ROOT`, `VONK_DEV_LITELLM_SECRET_ROOT`, and `VONK_DEV_RUNTIME_CONFIG_ROOT`. Derive LiteLLM's database URL from the parsed PostgreSQL URL without logging it. Validate reruns preserve generated admin/worker credentials and replace only derived projections.

- [ ] **Step 5: Verify package wheel contents and tests**

```bash
uv build --project control
python -m zipfile -l control/dist/*.whl | rg 'vonk_control/resources/dev/'
uv run --project control pytest control/tests/test_dev_runtime_assets.py control/tests/test_dev_init.py -q
```

- [ ] **Step 6: Commit**

```bash
git add control/pyproject.toml control/src/vonk_control control/tests/test_dev_runtime_assets.py control/tests/test_dev_init.py
git commit -m "feat(control): stage isolated development runtime assets"
```

### Task 4: Wire secure Caddy and acknowledged LiteLLM into development Compose

**Files:**
- Modify: `deploy/compose/compose.dev.images.yaml`
- Modify: `deploy/compose/tests/test_dev_compose.py`
- Modify: `deploy/compose/tests/test_dev_compose_secrets.py`
- Modify: `deploy/compose/tests/test_agent_ingress.py`
- Modify: `deploy/compose/tests/test_litellm_supervisor.py`

**Interfaces:**
- Consumes Task 3's runtime-config and service secret volumes.
- Publishes only `127.0.0.1:${VONK_DEV_PORT:-8080}:8000` and `${VONK_AGENT_PORT:-8443}:8443`.
- Provides `litellm:4000` only on internal application/data networks.

- [ ] **Step 1: Add failing Compose topology tests**

Assert exact images/digests, pull policy, user, read-only filesystem, capability drop, no-new-privileges, tmpfs, volumes, networks, health checks, and dependency conditions for Caddy and LiteLLM. Assert no service other than Caddy publishes a LAN address and no CA private key is readable outside API.

- [ ] **Step 2: Run the Compose tests to establish failure**

```bash
uv run pytest deploy/compose/tests/test_dev_compose.py deploy/compose/tests/test_dev_compose_secrets.py -q
```

- [ ] **Step 3: Add the services and projections**

Use the existing pinned Caddy and LiteLLM image identities. Mount staged configuration read-only, dedicated service secrets read-only, routes read-only in LiteLLM, and supervisor state read-write only where acknowledgement requires it. Keep worker route publication write access and worker supervisor access read-only.

Set control services to:

```yaml
VONK_AGENT_RUNTIME: enabled
VONK_AGENT_CA_PROVIDER: builtin
VONK_AGENT_BUILTIN_CA_BOOTSTRAP: "1"
VONK_MANAGEMENT_CIDRS_FILE: /run/secrets/management-cidrs
```

Retain `VONK_DEPLOYMENT_BRANCH: deploy`.

- [ ] **Step 4: Render and adapt configuration**

```bash
docker compose -f deploy/compose/compose.dev.images.yaml config -q
uv run pytest deploy/compose/tests/test_agent_ingress.py deploy/compose/tests/test_litellm_supervisor.py -q
```

- [ ] **Step 5: Commit**

```bash
git add deploy/compose/compose.dev.images.yaml deploy/compose/tests
git commit -m "feat(compose): add secure development agent and route ingress"
```

### Task 5: Create reproducible development PKI and project preparation

**Files:**
- Modify: `.gitignore`
- Create: `scripts/dev-runtime-secrets.py`
- Create: `scripts/dev-runtime-project`
- Create: `scripts/tests/test_dev_runtime_secrets.py`
- Create: `scripts/tests/test_dev_runtime_project.py`
- Modify: `scripts/dev-compose-secrets.py`

**Interfaces:**
- Produces protected source directory `.dev/vonk-forge-secrets/` or explicit `--secrets-dir`.
- Produces a deployment directory containing exactly `docker-compose.yml` and `secrets/`.
- Accepts `--nas-address`, `--management-cidrs`, `--enroll-hostname`, `--agent-hostname`, and `--registry-hostname`.

- [ ] **Step 1: Add failing idempotence and safety tests**

Test generation in a mode-0700 local directory, refusal of symlinks/hardlinks/group-writable parents, exact mode 0600 files, unique CA subjects/serials, required SANs, Ed25519 agent CA constraints, 32-byte-or-stronger random tokens, and no overwrite on rerun. Capture stdout/stderr and assert no generated private bytes or passwords appear.

- [ ] **Step 2: Add failing project-layout tests**

Assert output contains exactly:

```text
docker-compose.yml
secrets/agent-ca-certificate
secrets/agent-ca-key
secrets/agent-proxy-auth
secrets/controller-ca
secrets/controller-server-certificate
secrets/controller-server-key
secrets/database-url
secrets/git-signing-key
secrets/litellm-master-key
secrets/litellm-upstream-key
secrets/management-cidrs
secrets/postgres-password
```

and rejects an SMB/Windows destination for generation while permitting a validated copy from local source to a mounted destination.

- [ ] **Step 3: Run tests and confirm failure**

```bash
uv run pytest scripts/tests/test_dev_runtime_secrets.py scripts/tests/test_dev_runtime_project.py -q
```

- [ ] **Step 4: Implement generation with `cryptography`**

Generate separate Ed25519 CAs, a server certificate with exact DNS SANs, random URL-safe proxy and LiteLLM tokens, and validated public fingerprints. Reuse the existing safe descriptor/openat patterns from `dev-compose-secrets.py`; never shell out with private values.

- [ ] **Step 5: Implement atomic project publication**

Validate the source Compose artifact and all source secret files, copy through a local staging directory, verify size/SHA-256 after each copy, then replace destination children individually. Do not add `current/`, timestamp folders, `.env`, Dockerfiles, or source code.

- [ ] **Step 6: Run tests and secret scanner**

```bash
uv run pytest scripts/tests/test_dev_runtime_secrets.py scripts/tests/test_dev_runtime_project.py -q
scripts/verify-dev-image-secrets
```

- [ ] **Step 7: Commit**

```bash
git add .gitignore scripts/dev-runtime-secrets.py scripts/dev-runtime-project scripts/dev-compose-secrets.py scripts/tests
git commit -m "feat(dev): prepare reproducible runtime secrets and NAS project"
```

### Task 6: Expand mutable and pinned Compose publication contracts

**Files:**
- Modify: `scripts/render-dev-compose`
- Modify: `scripts/tests/test_render_dev_compose.py`
- Modify: `scripts/verify-dev-image-secrets`
- Modify: `scripts/tests/test_verify_dev_image_secrets.py`
- Modify: `.github/workflows/dev-images.yml`
- Modify: `scripts/tests/test_dev_image_workflow.py`

**Interfaces:**
- Preserves mutable output with bare `ghcr.io/carstvaartjes/vonk-forge-{api,worker}:dev`.
- Preserves pinned output with exact `dev-sha-<commit>@sha256:<digest>` references and accepted baseline.
- Uses synthetic file contents only inside a deleted validation directory.

- [ ] **Step 1: Update renderer tests before constants**

Assert the renderer supplies every Compose file secret with non-secret synthetic material, permits only the two documented port interpolations, rejects every unresolved token, and produces byte-stable output on repeated runs.

- [ ] **Step 2: Run the focused tests and observe failures**

```bash
uv run pytest scripts/tests/test_render_dev_compose.py scripts/tests/test_verify_dev_image_secrets.py scripts/tests/test_dev_image_workflow.py -q
```

- [ ] **Step 3: Extend renderer and secret scan**

Add only synthetic validation values. Scan Compose, generated files, image history/config, SBOM, provenance, and workflow artifacts for known canary secret bytes and private-key markers. Continue forbidding local publication.

- [ ] **Step 4: Add complete-stack CI smoke ordering**

Have the workflow render, validate, start the stack with disposable secrets, wait for PostgreSQL/migration/API/Caddy/LiteLLM health, test TLS enrollment reachability and mTLS rejection, then tear down volumes. Publication remains dependent on this read-only acceptance job and the accepted-main ancestry check.

- [ ] **Step 5: Run workflow contract tests**

```bash
uv run pytest scripts/tests/test_render_dev_compose.py scripts/tests/test_verify_dev_image_secrets.py scripts/tests/test_dev_image_workflow.py -q
```

- [ ] **Step 6: Commit**

```bash
git add scripts/render-dev-compose scripts/verify-dev-image-secrets scripts/tests .github/workflows/dev-images.yml
git commit -m "ci(dev): validate complete public development stack"
```

### Task 7: Update package and generic onboarding documentation

**Files:**
- Modify: `docs/operations/install-vonk-agent.md`
- Modify: `docs/runbooks/node-onboarding.md`
- Modify: `docs/runbooks/agent-pki.md`
- Modify: `docs/runbooks/development-nas-installation.md`
- Modify: `deploy/compose/README.md`
- Modify: `docs/architecture-overview.md`
- Modify: `docs/runbooks/supply-chain.md`
- Modify: `README.md`
- Modify: `tests/test_docs_contract.py`

**Interfaces:**
- Documents generic `<NAS_MANAGEMENT_IP>` inputs and labels `192.168.1.231` as one example.
- Documents both agent URLs, exact CA fingerprint command, `/etc/hosts`, firewall, service ordering, and recovery.

- [ ] **Step 1: Add docs contract failures**

Assert no active agent example has `controller_url` without `enrollment_url`; the NAS guide says only `docker-compose.yml` and `secrets/`; `/etc/hosts` uses placeholders in generic commands; production says `latest` is informational and host-updater selection remains authoritative.

- [ ] **Step 2: Run docs test**

```bash
uv run pytest tests/test_docs_contract.py -q
```

- [ ] **Step 3: Rewrite the connected runbooks**

Include clean-machine prerequisites, key generation, 1Password backup without reveal output, project copy, Compose startup, firewall, hosts entries, grant/pair/approve, restart, rotation, backup, recovery, and removal. Keep dev and production headings visibly separate.

- [ ] **Step 4: Search stale contracts**

```bash
rg -n 'controller_url|agent.toml|compose.*secrets|latest|:dev|/etc/hosts' README.md docs deploy packaging
uv run pytest tests/test_docs_contract.py -q
```

- [ ] **Step 5: Commit**

```bash
git add README.md docs deploy/compose/README.md tests/test_docs_contract.py
git commit -m "docs: publish reproducible agent development installation"
```

### Task 8: Add an exact synthetic source-first acceptance fixture

**Files:**
- Create: `control/tests/fixtures/recipes/dev-http-smoke/recipe.json`
- Create: `control/tests/fixtures/recipes/dev-http-smoke/context/Dockerfile`
- Create: `control/tests/fixtures/recipes/dev-http-smoke/context/server.py`
- Create: `control/tests/fixtures/recipes/dev-http-smoke/expected.json`
- Create: `control/tests/test_development_recipe_fixture.py`
- Create: `scripts/run-development-slices`
- Create: `scripts/tests/test_run_development_slices.py`

**Interfaces:**
- Produces deterministic tar bytes and SHA-256 via existing `SourceBundle` canonicalization.
- Drives lifecycle mutations only through public `/api/v1/catalog/*` and
  `/api/v1/recipes/*` operations. It reads public `/api/v1/fleet` and
  `/api/v1/endpoints/{alias}` evidence to prove inventory and route gates.
- Persists a local evidence JSON file without bearer tokens or secret values.

- [ ] **Step 1: Write failing fixture-policy tests**

Assert a digest-pinned multi-architecture base, non-root `USER`, no package manager/network downloads, bounded source archive, canonical recipe hash, deterministic health endpoint, OpenAI-shaped response, and schema validation.

- [ ] **Step 2: Write failing runner state-machine tests**

With a fake HTTP server, require ordered states:

```text
inventory-ready -> recipe-resolved -> source-verified -> image-built ->
image-distributed -> installed -> running -> route-published -> inference-ok ->
stopped -> route-withdrawn -> uninstalled
```

Test safe resume from every completed state and refusal to skip a failed gate.

- [ ] **Step 3: Run tests and establish failures**

```bash
uv run --project control pytest control/tests/test_development_recipe_fixture.py -q
uv run pytest scripts/tests/test_run_development_slices.py -q
```

- [ ] **Step 4: Add minimal fixture and runner**

The runner accepts `--api-base`, `--admin-token-file`,
`--inference-token-file`, `--phase`, `--builder-node`, `--target-node`,
`--evidence-file`, and optional real-recipe input. It reads each token once from
a private regular non-symlink file, never includes either token in subprocess
arguments, and redacts authorization headers in errors. Control administration
and LiteLLM inference remain separate authorities.

- [ ] **Step 5: Run fixture, API, and operation tests**

```bash
uv run --project control pytest control/tests/test_development_recipe_fixture.py control/tests/test_recipe_api.py control/tests/test_recipe_operations.py control/tests/test_recipe_routes.py -q
uv run pytest scripts/tests/test_run_development_slices.py -q
```

- [ ] **Step 6: Commit**

```bash
git add control/tests/fixtures/recipes/dev-http-smoke control/tests/test_development_recipe_fixture.py scripts/run-development-slices scripts/tests/test_run_development_slices.py
git commit -m "test(recipes): add source-first development acceptance slice"
```

### Task 9: Add complete-stack local enrollment and route smoke tests

**Files:**
- Create: `deploy/compose/tests/test_dev_complete_stack.py`
- Modify: `scripts/dev-compose`
- Modify: `scripts/dev-compose-secrets.py`

**Interfaces:**
- Uses disposable generated secrets and local images.
- Uses a test client certificate issued by the disposable agent CA.
- Does not require GPU hardware for this task.

- [ ] **Step 1: Add failing complete-stack test**

Start the rendered graph and assert:

```python
assert enroll_without_client_cert.status_code in {202, 401, 403}
assert claim_without_client_cert.tls_error
assert spoofed_identity_headers_do_not_reach_api
assert litellm_bootstrap_health.status_code == 200
assert activation_ack_matches_exact_marker
```

Use unique project names and always tear down with `--volumes --remove-orphans` in a `finally` block.

- [ ] **Step 2: Prove failure against the pre-integration helper**

```bash
uv run pytest deploy/compose/tests/test_dev_complete_stack.py -q
```

- [ ] **Step 3: Extend local helper for all generated files**

Have `scripts/dev-compose-secrets.py` call the same Task 5 generator and return one protected secret directory. Keep local image overlays only for control images; third-party images remain digest-pinned.

- [ ] **Step 4: Run complete stack twice**

```bash
uv run pytest deploy/compose/tests/test_dev_complete_stack.py -q
uv run pytest deploy/compose/tests/test_dev_complete_stack.py -q
```

The second run proves idempotence and cleanup.

- [ ] **Step 5: Commit**

```bash
git add deploy/compose/tests/test_dev_complete_stack.py scripts/dev-compose scripts/dev-compose-secrets.py
git commit -m "test(compose): exercise development enrollment and routing stack"
```

### Task 10: Define real-model qualification and multi-node acceptance

**Files:**
- Create: `config/recipes/development/model-smoke.json`
- Create: `config/recipes/development/model-smoke-source.json`
- Create: `config/recipes/development/model-smoke-artifacts.json`
- Create: `config/recipes/development/model-smoke-multinode.json`
- Create: `scripts/qualify-development-model`
- Create: `scripts/tests/test_qualify_development_model.py`
- Modify: `scripts/run-development-slices`
- Modify: `scripts/tests/test_run_development_slices.py`
- Create: `docs/audits/development-model-smoke.md`

**Interfaces:**
- Produces immutable recipe, source, image, and model-artifact identities selected from repository evidence.
- Refuses unsupported architecture, missing `sm_121`, floating image/model refs, insufficient disk/memory, absent license acknowledgement, or missing direct-fabric facts.
- Extends runner phases `model-single` and `model-multinode`.

- [ ] **Step 1: Inventory candidate evidence without mutation**

Run read-only host checks for architecture, driver, CUDA, Podman, free disk/memory, GPU identity, existing immutable artifacts, and direct-fabric addresses. Compare existing audited candidates and select the smallest one that passes every gate. Record commands, output digests, source revisions, image manifest digest, artifact hashes, and license in the audit document.

- [ ] **Step 2: Write failing qualification tests**

Feed recorded fixtures into the qualifier and assert exact rejection reasons for wrong architecture, missing arm64 manifest, mutable refs, model hash mismatch, insufficient capacity, overlapping management/fabric address, and inconsistent two-node identities.

- [ ] **Step 3: Run and observe failures**

```bash
uv run pytest scripts/tests/test_qualify_development_model.py scripts/tests/test_run_development_slices.py -q
```

- [ ] **Step 4: Implement qualification and exact recipes**

Emit canonical JSON only after all gates pass. The single-node and multi-node documents share exact image/model identities. The multi-node recipe declares two ranks and one entrypoint; it never embeds site IPs, credentials, shell, mutable tags, or host paths.

- [ ] **Step 5: Add runner failure/recovery assertions**

Require exact image equality, all-rank health before route publication, route withdrawal after one-rank failure, recovery/republication, NAS and agent restart persistence, and normal stop/uninstall evidence.

- [ ] **Step 6: Run tests and schema checks**

```bash
uv run pytest scripts/tests/test_qualify_development_model.py scripts/tests/test_run_development_slices.py -q
uv run --project control pytest control/tests/test_recipe_contract.py control/tests/test_cluster_mappings.py control/tests/test_run_admission.py -q
```

- [ ] **Step 7: Commit**

```bash
git add config/recipes/development scripts/qualify-development-model scripts/run-development-slices scripts/tests docs/audits/development-model-smoke.md
git commit -m "feat(recipes): qualify reproducible real model validation"
```

### Task 11: Publish the complete slice runbook and clean-room procedure

**Files:**
- Create: `docs/runbooks/development-agent-workloads.md`
- Modify: `docs/runbooks/development-nas-installation.md`
- Modify: `docs/runbooks/node-onboarding.md`
- Modify: `docs/runbooks/model-switching.md`
- Modify: `docs/model-capacity-overview.md`
- Modify: `tests/test_docs_contract.py`

**Interfaces:**
- Documents exact generic commands for all phases and exact site substitutions for the current acceptance run.
- Documents evidence locations, restart/failure tests, normal cleanup, and temporary sudo removal.

- [ ] **Step 1: Extend failing docs contracts**

Require headings and commands for prerequisites, PKI, hosts, firewall, pairing, inventory, synthetic build/distribution/run, real single-node, real multi-node, rank failure/recovery, restart, stop/uninstall, rollback, secret rotation, and sudo cleanup.

- [ ] **Step 2: Run docs tests**

```bash
uv run pytest tests/test_docs_contract.py -q
```

- [ ] **Step 3: Write the runbook from executable commands**

Every command uses placeholders defined in a table, avoids printing secrets, states expected output/state, and includes a read-only precheck before mutation. Clearly label physical-site commands as acceptance examples.

- [ ] **Step 4: Clean-room documentation audit**

Follow the guide into a disposable directory using fresh generated secrets through Compose rendering and synthetic fixture validation. Record only public fingerprints and test-state IDs.

- [ ] **Step 5: Verify links and stale terms**

```bash
uv run pytest tests/test_docs_contract.py -q
rg -n 'UGREEN|current/|controller_url = ' README.md docs deploy packaging
```

Any `UGREEN` reference must be a clearly labeled UI example; every active agent TOML block must also contain `enrollment_url`.

- [ ] **Step 6: Commit**

```bash
git add docs tests/test_docs_contract.py
git commit -m "docs: add complete development workload validation runbook"
```

### Task 12: Run repository-wide verification and independent review

**Files:**
- Modify only files required by failures proven in this task.

**Interfaces:**
- Produces a clean branch with no secret material and all relevant suites green.

- [ ] **Step 1: Run formatting, static analysis, and unit suites in parallel**

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
uv run --project control pytest control/tests -q -n auto
uv run --project agent --frozen pytest agent/tests -q -n auto
uv run pytest scripts/tests deploy/compose/tests tests -q -n auto
```

- [ ] **Step 2: Run build/package/container checks**

```bash
scripts/build-agent-deb
scripts/verify-agent-deb
scripts/verify-agent-systemd
scripts/verify-dev-image-secrets
scripts/dev-image-acceptance
```

- [ ] **Step 3: Audit every requirement against the design**

Create a checklist mapping each design bullet to a test, runbook section, or physical acceptance step. Fix uncovered gaps test-first; do not mark deferred items complete.

- [ ] **Step 4: Request independent code review**

Use `superpowers:requesting-code-review` against `origin/main...HEAD`. Address every verified P1/P2 and any correctness/security issue using `superpowers:receiving-code-review` and TDD.

- [ ] **Step 5: Repeat affected and full verification**

Rerun the commands from Steps 1 and 2 after review fixes, then verify:

```bash
git diff --check origin/main...HEAD
git status --short
git grep -n -- 'BEGIN .*PRIVATE KEY' ':!control/tests' ':!agent/tests' ':!rust/**/tests'
```

- [ ] **Step 6: Commit verified review fixes**

```bash
git add -A
git commit -m "fix: close development workload review findings"
```

Skip this commit only when the tree is already clean.

### Task 13: Publish through GitHub, pass CI, and merge

**Files:**
- No planned source changes; CI/review fixes remain test-first and get focused commits.

- [ ] **Step 1: Use the publishing skill**

Invoke `github:yeet`, confirm only this branch's commits are in scope, push `feature/dev-agent-workload-slices`, and open a draft PR with design, security boundary, tests, and physical post-merge acceptance clearly listed.

- [ ] **Step 2: Monitor all checks**

Use `github:gh-fix-ci` for any failure. Confirm development image and APT publication jobs cannot run with PR credentials and publication remains accepted-main-only.

- [ ] **Step 3: Resolve review threads**

Use `github:gh-address-comments`; verify each requested change technically before implementation and resolve only after pushed evidence.

- [ ] **Step 4: Merge only green reviewed code**

Mark ready, obtain required approval/checks, merge through GitHub, and record the accepted main SHA. Do not create a release or local publication.

- [ ] **Step 5: Wait for post-merge development publications**

Verify the accepted SHA produced the `:dev` API/worker cohort, mutable Compose artifact, pinned Compose artifact, and monotonic development APT package. Verify public pullability and cohort equality before deployment.

### Task 14: Deploy and execute all three physical slices

**Files:**
- Runtime state only; retain redacted evidence under the runbook-defined local evidence path and commit no secrets.

**Interfaces:**
- NAS: accepted mutable Compose plus secret files.
- Nodes: `dgx-spark-1` and `dgx-spark-2`, Ubuntu 24.04 aarch64.
- Completes only after sudo removal.

- [ ] **Step 1: Read-only backup and preflight**

Record accepted digests, NAS Compose checksum, volume list, service state, Spark package versions, resources, driver/runtime, existing artifacts, and current firewall/hosts entries. Back up NAS volumes and source secrets according to the runbook without printing contents.

- [ ] **Step 2: Prepare and copy the accepted project**

Generate/validate missing PKI locally, back it up in 1Password, run `scripts/dev-runtime-project` to the mounted NAS share, and independently verify destination hashes and exact two-item layout.

- [ ] **Step 3: Redeploy NAS and verify boundaries**

Pull/redeploy from the NAS UI, then verify PostgreSQL, migration, API, worker, Caddy, and LiteLLM health; loopback human API; enrollment TLS; unauthenticated mTLS rejection; CIDR firewall; and no secret bytes in logs or inspect output.

- [ ] **Step 4: Configure and pair both Sparks**

Idempotently install `/etc/hosts`, controller CA, exact fingerprint, `enrollment_url`, `controller_url`, node IDs, and fabric facts. Generate one-use grants, pair/approve one node at a time, enable/start systemd units, and verify inventory plus certificate identity.

- [ ] **Step 5: Prove Slice 1 restart persistence**

Restart both agent services and the NAS stack. Verify both identities persist, presence becomes fresh, and inventory returns ready without new grants.

- [ ] **Step 6: Execute Slice 2 twice**

Run `scripts/run-development-slices --phase synthetic` through build, upload, exact distribution, install, start, route, deterministic request, stop, withdrawal, and uninstall. Repeat after service restarts and verify no unnecessary rebuild/redownload.

- [ ] **Step 7: Execute real single-node validation**

Run qualification, obtain any operator-required model credential through a root-owned local Spark file, install exact artifacts, run the model, invoke a smoke inference through LiteLLM, stop/restart, and verify route withdrawal/republication.

- [ ] **Step 8: Execute two-node gang and failure recovery**

Materialize the two-rank mapping, confirm exact image/model equality, start both ranks, infer through the sole entrypoint, stop one rank, prove route withdrawal, recover/reconcile, infer again, and restart NAS/agents without rebuilding.

- [ ] **Step 9: Clean workload state safely**

Stop and uninstall the validation deployments through normal APIs. Preserve reference-counted immutable caches and node identity; do not broadly delete volumes or artifact directories.

- [ ] **Step 10: Remove temporary unattended sudo**

On NAS and both Sparks:

```bash
sudo rm -f /etc/sudoers.d/vonktemp /etc/sudoers.d/99-vonk-codex-temporary
sudo -k
if sudo -n true 2>/dev/null; then exit 1; else echo PASSWORD_REQUIRED; fi
```

Disable NAS SSH in the UI after the final remote check if that is the site's normal posture.

- [ ] **Step 11: Final requirement audit**

Map every completion criterion to fresh command output/evidence, confirm no active failures or unresolved review threads, confirm temporary sudo is absent, and only then mark the goal complete.
