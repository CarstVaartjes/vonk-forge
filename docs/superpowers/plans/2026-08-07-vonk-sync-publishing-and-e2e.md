# Retired: Vonk Sync, Publishing, and End-to-End Implementation Plan

> Historical plan retained for provenance. Local authoring and WorkloadRun
> import scenarios described here are no longer active.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the local Vonk Forge product to the public catalog for explicit download and publishing, preserve offline local authority, expose administration/inference only through Tailscale, and prove the entire single- and multi-node lifecycle in staging.

**Architecture:** The local controller uses a bounded anonymous HTTPS client pinned to a released global schema bundle. Explicit imports need no account and always create an independent immutable local revision; no remote deletion or outage can erase it. Publishing is browser-mediated: the NAS exports only recipe JSON plus publisher-submitted test evidence, and OAuth/session credentials exist only on vonkforge.ai. Tailscale terminates the supported remote-access boundary before static Caddy, which proxies UI/API and `/v1/*` to LiteLLM.

**Tech Stack:** FastAPI, PostgreSQL, httpx, browser OAuth, JSON Schema/OpenAPI, Docker Compose, Tailscale, Caddy, LiteLLM, Playwright, pytest.

## Implemented status and superseding decision

- [x] Pin and verify the global recipe/problem/test-report schema bundle.
- [x] Fetch immutable `vonk://...@sha256:` revisions through a fixed HTTPS origin with no redirects, ambient credentials, or unbounded bodies.
- [x] Review and explicitly import global revisions into authoritative local PostgreSQL, preserving offline operation and provenance.
- [x] Attach hash/image/topology-bound publisher test evidence and export a target-namespace-normalized metadata-only envelope.
- [x] Keep OAuth entirely in the vonkforge.ai browser workspace; store no global access or refresh token on the NAS.
- [x] Enforce Tailscale ingress and static-Caddy/dynamic-LiteLLM responsibility boundaries.
- [ ] Complete hosted Railway OAuth acceptance and physical single-/multi-GPU node soak evidence; these require external provider credentials and hardware.

Tasks 4 and the token-bearing parts of Task 5 below are retained as historical design notes and are superseded by the browser-mediated export implemented above. They must not be implemented.

---

## Task 1: Release and pin the cross-repository contract

**Files (`vonk-forge-web`):**
- Create: `scripts/export-contract`
- Create: `tests/test_contract_release.py`
- Create: `.github/workflows/contract-release.yml`
- Modify: `openapi/openapi.json`
- Modify: `schemas/recipe/v1.schema.json`

**Files (`vonk-forge`):**
- Modify: `schemas/global/recipe-v1.schema.json`
- Modify: `schemas/global/problem-v1.schema.json`
- Modify: `schemas/global/test-report-v1.schema.json`
- Modify: `schemas/global/contract.lock.json`
- Modify: `scripts/update-global-contracts`
- Create: `control/tests/test_global_contract_lock.py`
- Modify: `.github/workflows/ci.yml`

- [ ] In `vonk-forge-web`, write the contract-release test first. Assert exported canonical files, SHA-256 manifest, semantic contract version, source commit, and generated OpenAPI are stable and contain no server secrets/internal routes.
- [ ] Run it and confirm failure because export tooling is absent.
- [ ] Implement deterministic export and a signed GitHub Release artifact `vonk-contract-v1.<minor>.<patch>.tar.gz` with checksum/provenance.
- [ ] In `vonk-forge`, write the lock test first. Assert every vendored byte hash equals `contract.lock.json` and local positive/negative fixtures validate identically.
- [ ] Extend `scripts/update-global-contracts <release-tag>` to download only a signed release, verify provenance/checksum, stage files, run compatibility tests, and update the exact source commit/hashes. Runtime code must never follow global `main`.
- [ ] Add CI compatibility checks in Python and TypeScript; Rust joins after its protocol/schema consumer exists.
- [ ] Commit separately: `build: release public catalog contract` and `build: pin global catalog contract`.

## Task 2: Generate a bounded local global-catalog client

**Files (`vonk-forge`):**
- Create: `control/src/vonk_control/global_catalog_client.py`
- Create: `control/src/vonk_control/global_catalog_models.py`
- Create: `control/tests/test_global_catalog_client.py`
- Modify: `control/src/vonk_control/settings.py`
- Modify: `control/pyproject.toml`

- [ ] Write failing tests for list/detail/revision fetch, ETag/304, pagination, problem bodies, timeouts, Retry-After, body limits, TLS verification, redirects, offline operation, and base URL allowlisting.
- [ ] Run the scoped test; confirm missing client.
- [ ] Generate or implement typed models from the pinned OpenAPI artifact, then wrap transport with strict deadlines, response caps, no ambient proxy credentials, HTTPS production requirement, and structured stable errors.
- [ ] Cache only public metadata/ETags in PostgreSQL. Never make controller startup/readiness depend on global availability.
- [ ] Emit metrics for request outcome/latency and redact authorization headers, cookies, draft JSON, and tokens.
- [ ] Run client and controller offline tests.
- [ ] Commit: `feat(catalog): add bounded global catalog client`

## Task 3: Import global revisions into the authoritative local catalog

**Files (`vonk-forge`):**
- Create: `control/src/vonk_control/global_import.py`
- Create: `control/src/vonk_control/global_catalog_api.py`
- Create: `control/tests/test_global_import.py`
- Modify: `control/web/src/pages/catalog.tsx`
- Modify: `control/web/src/pages/catalog.test.tsx`
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/web/src/api/client.ts`
- Modify: `control/web/src/app.tsx`

- [ ] Write backend/UI tests first for anonymous browse, revision preview, explicit import, identical re-import, changed public revision as a separate local revision, schema incompatibility, hidden remote revision, remote outage, and local recipe remaining usable after outage/deletion.
- [ ] Run scoped tests and confirm missing behavior.
- [ ] Add read-through browse endpoints that clearly identify live global versus local cached state. Import fetches one immutable revision, validates canonical hash/schema, and creates a local recipe source/revision transactionally.
- [ ] Keep remote publisher, recipe, revision, content hash, validation/evidence summaries, import timestamp, and contract version. The local copy is never a proxy row and is never cascade-deleted from remote state.
- [ ] Present an import review with image/weight sources, disk/RAM/topology, evidence provenance, trust/publisher, required capabilities, and any local resolution needed before acceptance.
- [ ] Run with the global test server disabled after import and prove install/run planning still works.
- [ ] Commit: `feat(catalog): import immutable global recipes locally`

## Task 4: SUPERSEDED — do not add OAuth device authorization locally

**Files (`vonk-forge-web`):**
- Create: `api/src/vonk_catalog/device_authorization.py`
- Create: `api/src/vonk_catalog/device_api.py`
- Create: `api/tests/test_device_authorization.py`
- Create: `web/src/pages/device-approval.tsx`
- Create: `web/src/pages/device-approval.test.tsx`

**Files (`vonk-forge`):**
- Create: `control/src/vonk_control/global_auth.py`
- Create: `control/src/vonk_control/secrets.py`
- Create: `control/tests/test_global_auth.py`
- Modify: `control/src/vonk_control/settings.py`

- [ ] Write global tests first for device code issuance, user code entropy, OAuth browser approval, publisher/scope selection, interval enforcement, expiry, denial, one-time exchange, refresh rotation/reuse detection, revocation, and no client secret for local installs.
- [ ] Implement RFC 8628-style endpoints with scopes `draft:read`, `draft:write`, `publish`, and publisher namespace binding. Access tokens are short-lived and opaque; refresh tokens are hashed server-side.
- [ ] Write local tests for start/login status/logout, encrypted-at-rest refresh token, locked-down file/database key permissions, token refresh, revoked token, controller restart, and logs/backup excluding plaintext credentials.
- [ ] Implement the local login flow that shows verification URL/code and polls within the server-provided interval. Store only the authorized publisher/scopes and encrypted refresh credential.
- [ ] Require a separate explicit local action to publish; login itself causes no upload or external mutation.
- [ ] Commit separately: `feat(auth): authorize local publishing devices` and `feat(catalog): add global publisher login`.

## Task 5: SUPERSEDED — browser upload replaces token-bearing local publishing

**Files (`vonk-forge`):**
- Create: `control/src/vonk_control/global_publishing.py`
- Create: `control/src/vonk_control/test_evidence.py`
- Create: `control/tests/test_global_publishing.py`
- Create: `control/web/src/pages/publish-recipe.tsx`
- Create: `control/web/src/pages/publish-recipe.test.tsx`
- Modify: `control/src/vonk_control/global_catalog_api.py`
- Modify: `control/web/src/api/client.ts`

- [ ] Write failing tests for untested recipe, mutable image tag, private/unreachable image, wrong publisher, draft create/update, ETag conflict, idempotent retry, validation poll, registry failure, explicit publish, immutable result, token loss, and global outage.
- [ ] Run scoped tests; confirm missing service/UI.
- [ ] Generate a canonical test report only from recorded local physical/simulator runs matching recipe hash, image digest, weight digest, node count, runtime, inference checks, timestamps, and Vonk version. Sign the evidence with the local installation identity for provenance, not certification.
- [ ] Upload only recipe JSON and evidence JSON to a private draft. Never upload image layers, weights, registry credentials, node inventory, model prompts/responses, tailnet details, local hostnames, or unrelated audit data.
- [ ] Make validation a resumable local job. Show every global check and stable repair guidance; losing connectivity preserves the local recipe and resumes polling later.
- [ ] Require final confirmation of publisher/slug/hash/digest/public visibility, then call publish with an idempotency key and persist returned global revision/hash/source link locally.
- [ ] Run tests including restart at each job phase.
- [ ] Commit: `feat(catalog): publish tested local recipes globally`

## Task 6: Enforce the Tailscale-only product boundary

**Files (`vonk-forge`):**
- Create: `control/tests/security/test_tailscale_only_ingress.py`
- Create: `tests/e2e/test_tailscale_ingress.py`
- Create: `docs/runbooks/tailscale-access.md`
- Modify: `compose.yaml`
- Modify: `deploy/compose/caddy/Caddyfile`
- Modify: `deploy/compose/docker-compose.tailscale.yml`
- Modify: `.env.example`

- [ ] Write failing configuration and network tests proving the host publishes no controller, web, PostgreSQL, LiteLLM, or Caddy port on LAN/public interfaces; only the Tailscale sidecar/network namespace exposes HTTPS.
- [ ] Run the security test; confirm current Compose violates or lacks the asserted final contract.
- [ ] Make Tailscale the documented/default Compose profile, use auth-key or OAuth-client secret files, keep state in a dedicated volume, advertise no subnet routes by default, and fail readiness when the expected tailnet identity/certificate is absent.
- [ ] Configure static Caddy routes for admin UI/API and `/v1/*` to LiteLLM, security headers, request limits, WebSocket/SSE support where needed, and trusted-proxy rules limited to the sidecar.
- [ ] Verify GPU nodes remain management-LAN-only and do not need Tailscale; LiteLLM's egress network alone can reach validated GPU node service endpoints.
- [ ] Document onboarding, ACL examples, key rotation, logout/re-auth, NAS reboot, and recovery without opening a LAN port.
- [ ] Run network namespace scans from tailnet, LAN, container networks, and a GPU node simulator.
- [ ] Commit: `feat(access): make Tailscale the default ingress`

## Task 7: Prove static Caddy and dynamic LiteLLM responsibilities

**Files (`vonk-forge`):**
- Create: `tests/e2e/test_route_publication.py`
- Create: `tests/fixtures/routes/single-node.json`
- Create: `tests/fixtures/routes/multi-node.json`
- Create: `docs/runbooks/routing.md`
- Modify: `control/tests/test_litellm.py`
- Modify: `control/tests/test_route_runtime.py`

- [ ] Write the E2E test first. Assert Caddy configuration is unchanged while recipes start/stop, the controller's complete LiteLLM config changes atomically, single-node and multi-node expose one entrypoint each, worker ranks are absent, and withdrawn runs stop receiving new inference before workload termination.
- [ ] Run the test and confirm expected failure until admission/routing implementation is present.
- [ ] Capture route generation lineage: recipe revision, run generation, node identity, entrypoint, health evidence timestamp, LiteLLM candidate hash, validation result, active hash, and withdrawal reason.
- [ ] Test malformed candidate, LiteLLM reload failure, controller restart, stale agent lease, split multi-node readiness, and Caddy restart. The last validated config remains active only while its run leases remain valid.
- [ ] Document the request path: Tailscale -> Caddy -> LiteLLM -> one GPU node entrypoint -> GPU node fabric workers.
- [ ] Commit: `test(routing): prove controller-managed LiteLLM routes`

## Task 8: Execute the complete staging acceptance matrix

**Files (`vonk-forge-web`):**
- Create: `tests/e2e/test_catalog_journey.py`
- Create: `docs/operations/staging-acceptance.md`

**Files (`vonk-forge`):**
- Create: `tests/e2e/test_vonk_forge_journey.py`
- Create: `docs/runbooks/end-to-end-acceptance.md`
- Create: `docs/audits/vonk-forge-release-evidence.md`
- Modify: `README.md`

- [ ] Write one traceable acceptance scenario covering: local authoring; WorkloadRun import disposition report; local image digest and recipe test; global device login; private draft; validation; immutable publication; anonymous discovery on a second install; local import; disk planning/install; memory/topology planning; multi-node start; route publication; inference through Tailscale; stop; route withdrawal; offline rerun from local state.
- [ ] Add negative scenarios for malicious WorkloadRun fields, insufficient disk, insufficient memory, mismatched nodes, missing ARM64 image, unreachable registry, revoked OAuth token, global outage, lost GPU node, stale readiness, and failed LiteLLM candidate.
- [ ] Use Railway staging and disposable local PostgreSQL databases first, then repeat workload-critical phases on physical Vonk Forge GPU nodes. Never point tests at production catalog data or production publisher tokens.
- [ ] Record exact repo commits, contract release/hash, migration heads, container digests, `.deb` digest/signature, GPU node firmware/driver/runtime, recipe revision, image/weight digests, node counts, route hashes, and backup/restore evidence.
- [ ] Map every architecture-spec requirement to an automated test or named manual/physical artifact. Any unmapped requirement blocks the release.
- [ ] Run full suites in both repositories, `git diff --check`, dependency/security scans, database restore drills, and 24-hour Rust-agent/multi-node soak.
- [ ] Commit separately: `test: add global catalog staging acceptance` and `test: complete Vonk Forge end-to-end acceptance`.

## Verification

From `vonk-forge-web`:

```bash
uv run --project api pytest api/tests tests/e2e/test_catalog_journey.py -q
uv run --project worker pytest worker/tests -q
npm --prefix web test -- --run
npm --prefix web run build
git diff --check
```

From `vonk-forge`:

```bash
uv run --project control pytest control/tests -q
uv run --project agent_protocol pytest agent_protocol/tests -q
cargo test --workspace
npm --prefix control/web test -- --run
npm --prefix control/web run build
uv run pytest tests/e2e/test_vonk_forge_journey.py tests/e2e/test_route_publication.py tests/e2e/test_tailscale_ingress.py -q
git diff --check
```

Completion requires passing staging plus physical evidence, a successful global database restore, and an offline rerun proving the local database remains authoritative after the global service is unavailable.
