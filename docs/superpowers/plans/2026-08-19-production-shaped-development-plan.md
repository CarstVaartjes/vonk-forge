# Production-Shaped Development Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make development consume the production-shaped Compose graph and GitHub Actions-published images, with the selected image/version as the only runtime difference from production.

**Architecture:** Promote the production Compose graph to the sole runtime source of truth. Development and production are mutually exclusive release channels; the deployment selects a published image/version while retaining the same credentials, PKI, persistent data, endpoints, volumes, and service boundaries.

**Tech Stack:** Docker Compose, GitHub Actions immutable image references, Python development bootstrap tooling, Step CA, Caddy, pytest, YAML contract tests.

**Spec:** `docs/superpowers/specs/2026-08-19-production-shaped-development-design.md`

## Global Constraints

- Development must not build local images, use `dev-local` tags, inject a source-origin repository, or bypass GitHub Actions image publication.
- Development and production must share one production-shaped Compose service graph.
- Development and production are not concurrent environments on one network.
- Development and production share hostnames, management CIDRs, published ports,
  service names, network mappings, secret projection contracts, credentials, PKI,
  persistent data, and volumes.
- Existing production security boundaries must remain intact.
- Tests must prove topology parity and published-image consumption.

---

### Task 1: Inventory the production/development graph and define shared inputs

**Files:**
- Inspect: `deploy/compose/compose.yaml`
- Inspect: `deploy/compose/compose.dev.yaml`
- Inspect: `deploy/compose/compose.dev.images.yaml`
- Inspect: `.github/workflows/dev-images.yml`
- Inspect: `scripts/dev-compose`
- Inspect: `scripts/dev-image-acceptance`
- Test: `deploy/compose/tests/test_dev_compose.py`

**Interfaces:**
- Produces the exact service/image/secret/volume mapping used by Tasks 2–4.

- [ ] **Step 1: Add a failing topology-parity test** asserting that the development configuration includes the production runtime services and does not use `dev-local`, `build:`, or source-origin injection.
- [ ] **Step 2: Run the focused test** with `uv run --frozen --group dev python -m pytest -q deploy/compose/tests/test_dev_compose.py -k parity`; verify it fails against the current split graph.
- [ ] **Step 3: Record the mapping** in the test fixtures: production runtime services, required networks, hostnames, management CIDRs, published ports, secret projections, and persistent volume names.
- [ ] **Step 4: Run the focused test again** to verify the test still fails for the intended topology mismatch.
- [ ] **Step 5: Commit** the failing contract test with `git add deploy/compose/tests/test_dev_compose.py && git commit -m "test: define development production topology parity"`.

### Task 2: Make production Compose the shared runtime graph

**Files:**
- Modify: `deploy/compose/compose.yaml`
- Create or modify: `deploy/compose/compose.dev.yaml`
- Remove or reduce: `deploy/compose/compose.dev.images.yaml`
- Test: `deploy/compose/tests/test_dev_compose.py`

**Interfaces:**
- Consumes the service mapping from Task 1.
- Produces a production-shaped Compose graph with mode-specific image,
  image/version input only.

- [ ] **Step 1: Extend the failing parity test** to require production service names and shared security-sensitive configuration, while allowing only documented mode-specific inputs.
- [ ] **Step 2: Run the test** and confirm the current development graph fails.
- [ ] **Step 3: Refactor Compose sources** so the production graph is the sole runtime graph and development supplies only the published image/version references.
- [ ] **Step 4: Remove the development image-template path** that substitutes `vonk-forge-api:dev-local`, `vonk-forge-worker:dev-local`, and a source-origin repository.
- [ ] **Step 5: Run `docker compose config` contract tests** for both production and development inputs and verify the parity test passes.
- [ ] **Step 6: Commit** with `git add deploy/compose && git commit -m "refactor: share production compose topology"`.

### Task 3: Use GitHub Actions-published development images

**Files:**
- Modify: `scripts/dev-compose`
- Modify: `.github/workflows/dev-images.yml` if image output metadata is not consumable locally
- Test: `scripts/tests/test_dev_image_acceptance.py`
- Test: `deploy/compose/tests/test_dev_compose.py`

**Interfaces:**
- Consumes the published development image references from GitHub Actions.
- Produces a wrapper that pulls those immutable references and never builds or
  clones runtime source.

- [ ] **Step 1: Add failing wrapper assertions** for published immutable image references, absence of `--build`, absence of `dev-local`, and absence of source-origin injection.
- [ ] **Step 2: Run the focused wrapper tests** and verify they fail against the current script.
- [ ] **Step 3: Implement image-channel resolution** using the repository’s existing GitHub Actions publication metadata and pass the resolved digests into the shared Compose graph.
- [ ] **Step 4: Remove local port overrides, disposable volume naming, and development-only secret projections from the deployed development channel.**
- [ ] **Step 5: Run wrapper/configuration tests** and verify the published-image assertions pass.
- [ ] **Step 6: Commit** with `git add scripts/dev-compose scripts/tests/test_dev_image_acceptance.py deploy/compose/tests/test_dev_compose.py .github/workflows/dev-images.yml && git commit -m "feat: run development from published images"`.

### Task 4: Align development PKI with production Step CA

**Files:**
- Modify: `deploy/compose/compose.dev.yaml`
- Modify: `deploy/compose/compose.yaml` only for shared provider inputs if required
- Modify: `control/src/vonk_control/dev_bootstrap.py`
- Modify: `scripts/dev-runtime-secrets.py`
- Test: `deploy/compose/tests/test_dev_compose_secrets.py`
- Test: `scripts/tests/test_dev_runtime_secrets.py`
- Test: existing agent PKI/control integration tests

**Interfaces:**
- Consumes the deployment's configured Step CA state.
- Produces the same Step CA service and certificate renewal contract used by
  production for the selected image channel.

- [ ] **Step 1: Add a failing test** requiring the development graph to include the Step CA service, its isolated state, and the same Caddy/API trust projections as production.
- [ ] **Step 2: Run the focused PKI tests** and verify they fail against the built-in development CA path.
- [ ] **Step 3: Use the same Step CA inputs and persistent PKI paths as the production-shaped deployment; temporary test roots belong only to test harnesses.**
- [ ] **Step 4: Wire development service dependencies and secret projections** to the shared Step CA contract.
- [ ] **Step 5: Remove the development-only controller server certificate rotation path** once Step CA/Caddy owns the server certificate lifecycle; retain validation that reports provider expiry clearly.
- [ ] **Step 6: Run the PKI and enrollment/renewal tests** and verify the same protocol succeeds for the development image channel.
- [ ] **Step 7: Commit** with `git add deploy/compose control/src/vonk_control/dev_bootstrap.py scripts/dev-runtime-secrets.py deploy/compose/tests scripts/tests && git commit -m "feat: use the production PKI flow in development"`.

### Task 5: Align documentation and reset behavior

**Files:**
- Modify: `deploy/compose/README.md`
- Modify: `docs/runbooks/development-nas-installation.md`
- Modify: `docs/runbooks/control-plane-bootstrap.md`
- Modify: relevant documentation contract tests

**Interfaces:**
- Documents one production-shaped deployment model with release-channel image
  selection.

- [ ] **Step 1: Add failing documentation assertions** requiring published-image usage, shared topology, Step CA, and explicit isolation boundaries.
- [ ] **Step 2: Run the documentation tests** and verify they fail against the split development instructions.
- [ ] **Step 3: Rewrite the development instructions** to describe the shared graph, GitHub Actions image channel, same PKI/data contract, and channel-switch/reset commands.
- [ ] **Step 4: Run documentation and Compose contract tests** and verify they pass.
- [ ] **Step 5: Commit** with `git add deploy/compose/README.md docs/runbooks docs/superpowers && git commit -m "docs: describe production-shaped development"`.

### Task 6: Full verification and PR publication

**Files:**
- Test: all files changed by Tasks 1–5

- [ ] **Step 1: Run `git diff --check` and `bash -n scripts/dev-compose scripts/dev-image-acceptance`**.
- [ ] **Step 2: Run the focused Python/Compose tests** with `uv run --frozen --group dev python -m pytest -q scripts/tests deploy/compose/tests tests/runbooks`.
- [ ] **Step 3: Run the repository’s configured Ruff and CI-local checks.**
- [ ] **Step 4: Review the final diff for production credential or persistent-volume access from development.**
- [ ] **Step 5: Push `feat/production-shaped-development-deployment` and open a draft PR.**
- [ ] **Step 6: Convert the PR to ready after local verification and report the PR URL.**
