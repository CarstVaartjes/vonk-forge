# Agent-Derived GPU node Endpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make authenticated outbound-agent presence and current management addresses drive GPU node availability and LiteLLM upstream publication without treating IP addresses as node identity.

**Architecture:** Caddy supplies the direct LAN peer address in a proxy-authenticated header on mTLS agent requests. A new presence service validates that address against configured management CIDRs, updates `AgentNode.last_seen_at`, and stores a bounded observation. Route publication consumes certificate-bound node IDs, fresh observations, and workload ports through a fail-closed endpoint policy; address changes enter maintenance before a replacement is published.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, PostgreSQL/SQLite tests, `ipaddress`, Caddy, pytest, LiteLLM JSON generation.

## Global Constraints

- Preserve the existing outbound mTLS agent protocol and manual one-node onboarding flow.
- Do not add subnet scans, SSH discovery, mDNS trust, or per-node IP configuration.
- A management address is an observation; the immutable node identity remains the accepted `spk_` ID and certificate.
- Accept only canonical IP literals inside `VONK_MANAGEMENT_CIDRS`; reject loopback, link-local, multicast, unspecified, reserved, and direct-fabric CIDRs.
- Only repository-declared workload ports may become upstreams.
- Withdraw an old route before validating a replacement address.
- Keep Git authoritative for fleet membership and never publish an unaccepted node.
- Preserve unrelated worktree changes in agent supervisor and lifecycle files.

---

### Task 1: Validate and persist authenticated agent presence

**Files:**
- Create: `control/src/vonk_control/presence.py`
- Modify: `control/src/vonk_control/settings.py`
- Modify: `control/src/vonk_control/agent_api.py`
- Modify: `deploy/compose/compose.yaml`
- Test: `control/tests/test_presence.py`
- Test: `control/tests/test_settings.py`
- Test: `control/tests/test_agent_api.py`

**Interfaces:**
- Produces: `ManagementAddressPolicy.parse(value: str, *, forbidden_cidrs: str = "") -> ManagementAddressPolicy`.
- Produces: `AgentPresenceService.observe(node_id: str, address: str, observed_at: datetime) -> ManagementAddressObservation`.
- Produces: `AgentPresenceService.latest(node_id: str, *, maximum_age_seconds: int, now: datetime) -> ManagementAddressObservation`.
- Consumes: proxy-authenticated request header `X-Vonk-Agent-Source` set only by Caddy.

- [ ] **Step 1: Write failing CIDR and observation tests**

Add tests that construct `ManagementAddressPolicy.parse("10.0.0.0/24,10.1.0.0/16", forbidden_cidrs="10.0.0.240/28")`, accept `10.0.0.42`, canonicalize it, and reject `127.0.0.1`, `169.254.1.1`, `224.0.0.1`, `10.0.0.241`, and `10.0.1.1`. Add a database test proving `observe()` updates `AgentNode.last_seen_at`, stores exactly one `Observation(kind="management-address", payload={"address": "10.0.0.42"})`, and `latest()` rejects stale observations.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run --project control pytest control/tests/test_presence.py -v`

Expected: FAIL because `vonk_control.presence` does not exist.

- [ ] **Step 3: Implement the bounded presence service**

Implement immutable `ManagementAddressObservation(node_id: str, address: str, observed_at: datetime)`. Parse comma-separated CIDRs with `ipaddress.ip_network(..., strict=True)`, reject empty/duplicate networks, and permit a source only when it belongs to an allowed network, belongs to no forbidden network, and has none of the prohibited address classes. Store the canonical compressed address and an aware UTC timestamp. Query only `Observation.kind == "management-address"`, newest first.

- [ ] **Step 4: Add strict settings for management networks**

Add `management_cidrs: str` and `direct_fabric_cidrs: str` to `Settings`. In production require non-empty `VONK_MANAGEMENT_CIDRS`; parse it at startup through `ManagementAddressPolicy`. Default `VONK_DIRECT_FABRIC_CIDRS` to an empty string. Add settings tests for missing production CIDRs, invalid CIDRs, overlaps where an allowed network is wholly forbidden, and a valid comma-separated value.

- [ ] **Step 5: Record presence on every authenticated claim**

Add `presence: AgentPresenceService` to `AgentApiServices`. After `_authenticated_identity()` succeeds in `/agent/claim`, require exactly one `X-Vonk-Agent-Source` value, pass it to `presence.observe(identity.node_id, source, services.clock())`, and reject invalid/missing values with HTTP 422 without claiming work. Extend test fixtures and assert that forged agent identity headers still fail before any observation is written.

- [ ] **Step 6: Wire production configuration**

Construct `AgentPresenceService` where `AgentApiServices` is assembled, using `Settings.management_cidrs` and `Settings.direct_fabric_cidrs`. Add `VONK_MANAGEMENT_CIDRS` and `VONK_DIRECT_FABRIC_CIDRS` to the shared control environment in `deploy/compose/compose.yaml` and safe examples to `.env.example` in the networking plan.

- [ ] **Step 7: Run focused and regression tests**

Run: `uv run --project control pytest control/tests/test_presence.py control/tests/test_settings.py control/tests/test_agent_api.py -q`

Expected: PASS.

- [ ] **Step 8: Commit authenticated presence**

```bash
git add control/src/vonk_control/presence.py control/src/vonk_control/settings.py control/src/vonk_control/agent_api.py control/tests/test_presence.py control/tests/test_settings.py control/tests/test_agent_api.py deploy/compose/compose.yaml
git commit -m "feat: record authenticated GPU node presence"
```

### Task 2: Replace exact upstream allowlists with endpoint policy

**Files:**
- Modify: `control/src/vonk_control/routes.py`
- Modify: `control/src/vonk_control/litellm.py`
- Test: `control/tests/test_routes.py`
- Test: `control/tests/test_litellm.py`

**Interfaces:**
- Consumes: `ManagementAddressObservation` from Task 1.
- Produces: `RouteEndpoint(node_id: str, address: str, port: int, scheme: str, observed_at: datetime)`.
- Produces: `RouteEndpointPolicy(management: ManagementAddressPolicy, allowed_ports: frozenset[int], maximum_age_seconds: int, clock: Callable[[], datetime])`.
- Produces: `RoutePublisher.transition(candidate: RouteCandidate) -> RouteState`, which enters maintenance before replacement validation.
- Preserves: persisted `RouteState.aliases: Mapping[str, str]` and LiteLLM's string `api_base` input.

- [ ] **Step 1: Rewrite route tests around structured endpoints**

Change `_candidate()` to map aliases to `RouteEndpoint(NODE_ID, "10.0.0.42", 8888, "http", NOW)`. Test acceptance inside `10.0.0.0/24`, and rejection of a different node ID, stale observation, HTTPS when only HTTP is allowed, undeclared port 9999, userinfo, hostname, direct-fabric address, and address outside the CIDR. Assert the stored alias renders as `http://10.0.0.42:8888/v1`.

- [ ] **Step 2: Add the address-change fail-closed test**

Publish a healthy first generation at `.42`, then call `transition()` with `.43` while the validator rejects the new generation. Assert the final snapshot is `maintenance`, has no visible aliases, and never re-exposes `.42`.

- [ ] **Step 3: Run route tests and verify failure**

Run: `uv run --project control pytest control/tests/test_routes.py -v`

Expected: FAIL because `RouteEndpoint`, `RouteEndpointPolicy`, and `transition()` do not exist.

- [ ] **Step 4: Implement structured endpoint validation and rendering**

Validate canonical node ID, canonical IP literal, membership in the candidate's `node_ids`, allowed scheme from `{"http", "https"}`, membership in `allowed_ports`, and freshness not in the future and not older than `maximum_age_seconds`. Render IPv6 addresses with brackets and append `/v1`. `publish()` validates without mutating existing state; `transition()` first publishes maintenance for the candidate nodes and then calls `publish()`, leaving maintenance on any error.

- [ ] **Step 5: Keep LiteLLM generation string-only**

No network parsing belongs in `litellm.py`. Update tests to prove only already-rendered route-state strings reach `api_base`, and that a maintenance snapshot cannot render models.

- [ ] **Step 6: Run focused tests**

Run: `uv run --project control pytest control/tests/test_routes.py control/tests/test_litellm.py -q`

Expected: PASS.

- [ ] **Step 7: Commit endpoint policy**

```bash
git add control/src/vonk_control/routes.py control/src/vonk_control/litellm.py control/tests/test_routes.py control/tests/test_litellm.py
git commit -m "feat: derive routes from GPU node identity and presence"
```

### Task 3: Project agent availability without exposing addresses

**Files:**
- Modify: `control/src/vonk_control/dashboard.py`
- Test: `control/tests/test_dashboard.py`
- Modify: `docs/runbooks/node-onboarding.md`
- Modify: `docs/runbooks/platform-operations.md`

**Interfaces:**
- Consumes: `AgentNode.last_seen_at` from Task 1.
- Produces: dashboard fields `agent_state`, `agent_last_seen_at`, and `agent_online` without returning a management address.

- [ ] **Step 1: Add failing dashboard presence tests**

Create accepted fleet nodes with active, stale, revoked, and missing `AgentNode` rows. Assert `agent_online` is true only for an active node seen within the configured 150-second window, and assert serialized dashboard output contains no address field or observed IP literal.

- [ ] **Step 2: Run the dashboard test and verify failure**

Run: `uv run --project control pytest control/tests/test_dashboard.py -v`

Expected: FAIL because agent presence fields are absent.

- [ ] **Step 3: Implement the projection**

Load `AgentNode` rows in one query, map them by stable node ID, normalize SQLite timestamps to UTC, and compute online state from `state == "active"`, `revoked_at is None`, and age at most 150 seconds. Keep raw management observations private.

- [ ] **Step 4: Document the discovery contract**

Update onboarding and operations runbooks to state that bootstrap is manual, accepted agents announce presence through long polling, DHCP addresses are observations, reservations are recommended but not required, and no scan or automatic trust occurs.

- [ ] **Step 5: Run focused and full control tests**

Run: `uv run --project control pytest control/tests/test_dashboard.py -q && uv run --project control pytest -q`

Expected: PASS.

- [ ] **Step 6: Commit availability projection**

```bash
git add control/src/vonk_control/dashboard.py control/tests/test_dashboard.py docs/runbooks/node-onboarding.md docs/runbooks/platform-operations.md
git commit -m "feat: show agent-derived GPU node availability"
```

### Task 4: Verify the dynamic-address boundary

**Files:**
- Modify: `docs/security/threat-model.md`
- Modify: `deploy/compose/tests/test_agent_ingress.py`

**Interfaces:**
- Consumes: presence header contract and endpoint policy from Tasks 1-3.
- Produces: regression coverage tying Caddy's source header to the control boundary.

- [ ] **Step 1: Add a Caddy adaptation assertion**

Extend the agent-ingress test to assert that Caddy deletes any incoming `X-Vonk-Agent-*` headers and sets `X-Vonk-Agent-Source` from `{http.request.remote.host}` only on the verified mTLS agent route.

- [ ] **Step 2: Add threat-model entries**

Document spoofed source headers, DHCP churn, address reuse, stale observations, forbidden fabric CIDRs, and the maintenance-before-republish response.

- [ ] **Step 3: Run all affected suites**

Run: `uv run --project control pytest -q && uv run pytest deploy/compose/tests/test_agent_ingress.py -q && git diff --check`

Expected: PASS with no whitespace errors.

- [ ] **Step 4: Commit boundary verification**

```bash
git add deploy/compose/tests/test_agent_ingress.py docs/security/threat-model.md
git commit -m "test: verify dynamic GPU node address boundary"
```
