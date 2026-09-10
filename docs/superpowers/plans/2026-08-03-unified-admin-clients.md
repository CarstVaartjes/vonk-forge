# Unified CLI and Web Administration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every routine CLI and web operation through the same control API, durable jobs, and outbound agents.

**Architecture:** FastAPI's committed OpenAPI document generates Python and TypeScript clients; thin wrappers own polling, request-ID reuse, and domain error normalization. CLI and React pages remain views over identical plan/job resources; bootstrap and recovery tools are explicitly separate. LiteLLM and Grafana retain their native gateway/observability administration surfaces.

**Tech Stack:** Python 3.12, FastAPI OpenAPI, `openapi-python-client`, React 19, TypeScript, `openapi-typescript`, `openapi-fetch`, Vitest, Playwright, pytest

## Global Constraints

- Routine CLI commands never instantiate `SshBackend`, OpenSSH transports, local `ProfileSwitcher`, or direct agent clients.
- Control-plane unavailability is an explicit error; there is no SSH or local-controller fallback.
- CLI and web produce the same plan digests, jobs, authorization decisions, audit records, and terminal results.
- JSON output remains bounded, stable, and free of secrets/certificate material.
- `node-install`, `vonk-agent-repair`, and `vonk-control-offline` remain separately named bootstrap/recovery interfaces.

---

### Task 1: Typed API resources for routine operations

**Files:**
- Modify: `control/src/vonk_control/api.py`
- Create: `control/src/vonk_control/operation_api.py`
- Create: `control/openapi.json`
- Create: `scripts/generate-control-clients`
- Create: `tests/control/test_openapi_clients.py`
- Test: `control/tests/test_operation_api.py`

**Interfaces:**
- Produces `/api/nodes/status`, `/api/profiles/{id}/plan`, `/api/reconciliations`, `/api/endpoints/{alias}`, `/api/agents`, and job status/log resources.
- Plan responses include commit and digest; apply requests require exact digest.

- [ ] **Step 1: Write failing plan/apply/status tests**

```python
def test_apply_requires_exact_server_plan_digest(client, operator) -> None:
    plan = client.post("/api/profiles/agent/plan", headers=operator).json()
    stale = client.post("/api/reconciliations", headers=operator, json={"plan_digest": "0" * 64})
    assert stale.status_code == 409
    accepted = client.post("/api/reconciliations", headers=operator, json={"plan_digest": plan["digest"]})
    assert accepted.status_code == 202
```

- [ ] **Step 2: Run and observe missing routes**

Run: `uv run --project control pytest control/tests/test_operation_api.py -v`
Expected: FAIL with 404 responses.

- [ ] **Step 3: Implement thin resources over desired-state/orchestrator services**

Use strict Pydantic models with `extra="forbid"`, full commit/digest patterns,
RBAC, request IDs, CSRF for browser mutations, and audit events. Nodes status
uses persisted recent agent observations and reports staleness; it does not
trigger SSH. Endpoint resolution requires currently published route state.
Export a deterministic OpenAPI document, generate both language clients with
pinned generators, and make CI fail when regeneration changes tracked output.

- [ ] **Step 4: Run API and security suites**

Run: `uv run --project control pytest control/tests/test_operation_api.py control/tests/test_api.py control/tests/security/test_authorization_matrix.py -v`
Expected: PASS.

- [ ] **Step 5: Commit API resources**

```bash
git add control/src/vonk_control/api.py control/src/vonk_control/operation_api.py control/tests/test_operation_api.py
git commit -m "feat: expose agent-backed operation APIs"
```

### Task 2: Control client polling and typed failures

**Files:**
- Create: `src/cluster_profiles/generated_control/`
- Modify: `src/cluster_profiles/control_client.py`
- Test: `tests/cluster_profiles/test_control_client.py`

**Interfaces:**
- Produces `nodes()`, `plan_profile(profile)`, `apply_plan(digest)`, `job(id)`, `wait_job(id, timeout, interval)`, `endpoint(alias)`, `agents()`.

- [ ] **Step 1: Write failing retry/wait tests**

Test 401/403/409/503 mapping, malformed JSON, oversized response, terminal
success/failure/operator-wait, timeout, and `Retry-After` bounded to 1-30
seconds. Mutations are never automatically replayed after ambiguous transport
failure; callers reuse request IDs explicitly.

- [ ] **Step 2: Run and observe missing methods**

Run: `uv run pytest tests/cluster_profiles/test_control_client.py -v`
Expected: FAIL with missing attributes.

- [ ] **Step 3: Implement typed methods over existing request boundary**

Wrap the generated Python client rather than hand-maintaining request/response
models. Keep bearer token from environment/secret file, HTTPS validation, response
limit, and no token logging. `wait_job` polls only GET, returns structured
terminal result, and raises typed `JobFailed`, `JobWaitingForOperator`, or
`ControlTimeout`.

- [ ] **Step 4: Run client tests**

Run: `uv run pytest tests/cluster_profiles/test_control_client.py -v`
Expected: PASS.

- [ ] **Step 5: Commit client**

```bash
git add src/cluster_profiles/control_client.py tests/cluster_profiles/test_control_client.py
git commit -m "feat: control agent jobs through typed client"
```

### Task 3: Cut routine `vonkctl` commands over to API

**Files:**
- Modify: `src/cluster_profiles/cli.py`
- Create: `src/cluster_profiles/legacy_cli.py`
- Test: `tests/cluster_profiles/test_agent_cli.py`
- Modify: `tests/cluster_profiles/test_cli.py`

**Interfaces:**
- Routine commands: `nodes status`, `validate`, `prepare`, `switch`, `restore-default`, and `endpoint` call `ControlClient`.
- Explicit compatibility launcher: `bin/vonkctl-legacy` or `vonkctl legacy ...`, rejected in production documentation/settings.

- [ ] **Step 1: Write failing no-SSH CLI equivalence tests**

Inject a real HTTP test server and patch `SshBackend`, subprocess SSH, and local
dependency construction to raise. For each command assert the expected API
request and hand-derived JSON output. Test unavailable API returns
`error_type=control_api` without invoking fallback.

- [ ] **Step 2: Run and confirm current local controller path fails**

Run: `uv run pytest tests/cluster_profiles/test_agent_cli.py -v`
Expected: FAIL because commands build local SSH dependencies.

- [ ] **Step 3: Implement API-first routine command dispatch**

Parse commands as today, but construct `ControlClient` before any local
controller dependency. `validate` requests plan-only validation; `prepare` and
`switch` display plan by default and require existing explicit apply semantics;
mutations wait or return job ID according to `--wait/--no-wait`. Move old local
behavior without modification into explicit legacy module/launcher. Never
select it based on an exception.

- [ ] **Step 4: Run CLI, client, and transport-denial tests**

Run: `uv run pytest tests/cluster_profiles/test_agent_cli.py tests/cluster_profiles/test_control_client.py tests/cluster_profiles/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit CLI cutover**

```bash
git add src/cluster_profiles/cli.py src/cluster_profiles/legacy_cli.py bin/vonkctl-legacy tests/cluster_profiles/test_agent_cli.py tests/cluster_profiles/test_cli.py
git commit -m "refactor: route vonkctl through control API"
```

### Task 4: Agent enrollment and fleet pages

**Files:**
- Create: `control/web/src/api/generated.d.ts`
- Modify: `control/web/src/api/client.ts`
- Modify: `control/web/src/pages/fleet.tsx`
- Create: `control/web/src/pages/agents.tsx`
- Create: `control/web/src/components/enrollment-review.tsx`
- Test: `control/web/src/pages/agents.test.tsx`

**Interfaces:**
- Web lists agent state/version/last-seen/certificate expiry and pending enrollment evidence.
- Administrator may create grant, approve/reject enrollment, revoke node certificate, and inspect bounded audit evidence.

- [ ] **Step 1: Write failing accessible workflow tests**

Test semantic table/headings, keyboard navigation, explicit evidence comparison,
grant token displayed exactly once, approval confirmation, revocation warning,
and no private key/certificate body in page/API types.

- [ ] **Step 2: Run and observe missing page**

Run: `npm --prefix control/web test -- --run src/pages/agents.test.tsx`
Expected: FAIL importing page.

- [ ] **Step 3: Implement typed page and enrollment review**

Use `openapi-typescript` plus `openapi-fetch`; do not maintain parallel manual
API DTOs. Use existing session/CSRF behavior. Render immutable node ID, host-key
and hardware fingerprints, agent digest, timestamps, compatibility, and audit
status. Require administrator typed confirmation for rejection/revocation.
Provide clearly labelled links to the Caddy-protected LiteLLM Admin UI for
keys/teams/spend and Grafana for dashboards. Do not reproduce either UI and do
not allow LiteLLM dynamic model records to override Git-backed definitions.

- [ ] **Step 4: Run component tests and build**

Run: `npm --prefix control/web test -- --run && npm --prefix control/web run build`
Expected: PASS.

- [ ] **Step 5: Commit agent UX**

```bash
git add control/web/src/api control/web/src/pages/fleet.tsx control/web/src/pages/agents.tsx control/web/src/components/enrollment-review.tsx control/web/src/pages/agents.test.tsx
git commit -m "feat: administer GPU node agents in web UX"
```

### Task 5: Unified plan and job experience

**Files:**
- Modify: `control/web/src/pages/profiles.tsx`
- Modify: `control/web/src/pages/jobs.tsx`
- Create: `control/web/src/components/reconciliation-plan.tsx`
- Test: `control/web/src/components/reconciliation-plan.test.tsx`
- Test: `control/web/e2e/admin.spec.ts`

**Interfaces:**
- Profile actions preview exact placement/operations/routes/releases and submit exact digest.
- Job page displays parent job and node operations with bounded progress and operator-wait actions.

- [ ] **Step 1: Write failing CLI/web digest equivalence integration**

Against one disposable API, request the same profile plan from CLI and web
client and assert commit/digest/targets/operations are identical. Test stale
digest conflict and fail-closed unavailable node display.

- [ ] **Step 2: Run and observe incomplete web workflow**

Run: `npm --prefix control/web test -- --run && uv run pytest tests/e2e/test_admin_equivalence.py -v`
Expected: FAIL missing plan component/integration test.

- [ ] **Step 3: Implement plan diff and operation progress**

Show affected nodes, stop/start dependencies, immutable release hashes, route
maintenance, agent compatibility, and acceptance gates. Apply uses exact digest
and requires explicit confirmation. Job progress never renders raw untrusted
HTML or unbounded result data.

- [ ] **Step 4: Run Phase 5 verification**

Run: `uv run pytest tests/cluster_profiles/test_agent_cli.py tests/e2e/test_admin_equivalence.py -q && npm --prefix control/web test -- --run && npm --prefix control/web run build && git diff --check`
Expected: all pass.

- [ ] **Step 5: Commit unified experience**

```bash
git add control/web/src/pages control/web/src/components control/web/e2e/admin.spec.ts tests/e2e/test_admin_equivalence.py
git commit -m "feat: unify CLI and web agent workflows"
```
