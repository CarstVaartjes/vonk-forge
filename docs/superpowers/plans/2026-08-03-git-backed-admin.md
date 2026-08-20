# Git-Backed CLI and Web Administration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let administrators inspect and maintain repository-backed nodes, models, profiles, and desired deployments through equivalent CLI and web workflows.

**Architecture:** A repository service reads typed documents at an immutable base commit, creates canonical proposals in isolated Git workspaces, and enforces either pre-release commit mode or post-release PR mode. CLI and web use clients generated from the same OpenAPI document; the browser contains no independent Git or cluster logic. LiteLLM and Grafana retain their existing gateway and dashboard administration surfaces.

**Tech Stack:** Python 3.12, FastAPI, Git CLI with safe argv, GitHub connector/API abstraction, React + TypeScript + Vite, OpenAPI-generated client, pytest, Vitest, Playwright.

## Global Constraints

- Git is the only authority for models, profiles, topology, policy, and desired state.
- PostgreSQL stores jobs/audit/reconciliation only; drafts are base-commit proposals, not deployable database records.
- UI editing is limited to typed allowlisted repository documents.
- Canonical CLI and web proposals for the same input are byte-identical.
- Pre-release mode may commit through an explicit admin action; first real release permanently enables protected deployment branch and PR-only proposals.
- Only merged, CI-passing, eligible commits can reconcile.
- Never execute hooks or repository-provided binaries while parsing or proposing documents.

---

### Task 1: Implement safe immutable repository reads

**Files:**
- Create: `control/src/vonk_control/repository.py`
- Create: `control/tests/test_repository.py`

**Interfaces:**
- `RepositoryService.inspect(commit: str) -> RepositorySnapshot`.
- `read_document(commit, DocumentPath) -> TypedDocument`; allowed roots are `inventory/`, `config/cluster-profiles/`, `config/workloads/`, `locks/`, `manifests/`, and `docs/audits/` with per-type writers.

- [ ] **Step 1: Write failing traversal, hook, and immutable-read tests**

```python
def test_repository_rejects_unallowlisted_path(repo_service):
    with pytest.raises(RepositoryPolicyError):
        repo_service.read_document(COMMIT, "../../.git/config")


def test_inspect_does_not_execute_repository_hooks(malicious_repository, repo_service):
    repo_service.inspect(malicious_repository.commit)
    assert not malicious_repository.hook_marker.exists()
```

- [ ] **Step 2: Run and observe missing service**

Run: `uv run --project control pytest control/tests/test_repository.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement object-level reads with commit validation**

Resolve full 40-hex commits, use `git show <commit>:<allowlisted-path>` with hooks disabled and bounded output, reject symlinks/submodules for managed documents, parse through repository schemas, and return content hashes and typed dependency graphs.

- [ ] **Step 4: Run repository tests**

Run: `uv run --project control pytest control/tests/test_repository.py -v`
Expected: PASS.

- [ ] **Step 5: Commit repository reads**

```bash
git add control/src/vonk_control/repository.py control/tests/test_repository.py
git commit -m "feat: inspect repository-backed cluster state"
```

### Task 2: Build canonical typed proposals

**Files:**
- Create: `control/src/vonk_control/proposals.py`
- Create: `control/src/vonk_control/serializers.py`
- Create: `control/tests/test_proposals.py`

**Interfaces:**
- `ProposalService.preview(actor, authority_revision, changes) -> ProposalPreview`.
- Preview contains canonical patch, affected documents/profiles/nodes, validation results, and digest.

- [ ] **Step 1: Write failing determinism, stale-base, and path-policy tests**

```python
def test_equivalent_changes_produce_identical_patch(proposals):
    assert proposals.preview(ADMIN, COMMIT, CHANGE_A).patch == proposals.preview(ADMIN, COMMIT, CHANGE_A_REORDERED).patch


def test_stale_base_is_rejected_after_head_moves(proposals, repository):
    preview = proposals.preview(ADMIN, repository.head, CHANGE_A)
    repository.advance_head()
    with pytest.raises(StaleBaseCommit):
        proposals.apply(preview.digest)
```

- [ ] **Step 2: Run and observe missing proposal service**

Run: `uv run --project control pytest control/tests/test_proposals.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement typed serializers and isolated temporary worktrees**

Sort fields according to schema-defined canonical order, normalize UTF-8/LF/final newline, create an exact-base temporary worktree, write only allowlisted targets, run in-process schemas/policies, compute `git diff --binary --no-ext-diff`, and discard the worktree after producing immutable proposal content.

- [ ] **Step 4: Run proposal and repository tests**

Run: `uv run --project control pytest control/tests/test_repository.py control/tests/test_proposals.py -v`
Expected: PASS.

- [ ] **Step 5: Commit proposals**

```bash
git add control/src/vonk_control/proposals.py control/src/vonk_control/serializers.py control/tests/test_proposals.py
git commit -m "feat: preview canonical repository proposals"
```

### Task 3: Add commit/PR policy and CI eligibility

**Files:**
- Create: `control/src/vonk_control/git_policy.py`
- Create: `control/src/vonk_control/code_host.py`
- Create: `control/tests/test_git_policy.py`
- Create: `control/tests/test_code_host.py`

**Interfaces:**
- Modes: `development-direct` and irreversible `release-pr-only`.
- `submit(preview) -> SubmittedChange` creates a signed/audited commit or branch + PR through `CodeHost`.
- `eligible(commit) -> Eligibility` requires reachability from protected deployment branch and passing required checks.

- [ ] **Step 1: Write failing release transition and eligibility tests**

```python
def test_release_mode_cannot_return_to_direct(policy_store):
    policy_store.enable_release_pr_only(actor=ADMIN)
    with pytest.raises(IrreversiblePolicyError):
        policy_store.enable_development_direct(actor=ADMIN)


def test_unmerged_or_failing_commit_is_ineligible(git_policy, code_host):
    change = code_host.open_pr(checks="failing", merged=False)
    assert not git_policy.eligible(change.commit).ok
```

- [ ] **Step 2: Run and observe missing policy**

Run: `uv run --project control pytest control/tests/test_git_policy.py control/tests/test_code_host.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement policy state, branch naming, PR abstraction, and check verification**

Use branches `vonk-control/<proposal-id>`, commit trailers for actor/request/proposal digest, no force-push, GitHub App credentials through secret references, idempotent submission, and exact required-check names from trusted service configuration.

- [ ] **Step 4: Run policy tests**

Run: `uv run --project control pytest control/tests/test_git_policy.py control/tests/test_code_host.py -v`
Expected: PASS.

- [ ] **Step 5: Commit Git workflow**

```bash
git add control/src/vonk_control/git_policy.py control/src/vonk_control/code_host.py control/tests/test_git_policy.py control/tests/test_code_host.py
git commit -m "feat: enforce repository review workflow"
```

### Task 4: Expose repository, proposal, and reconciliation APIs and CLI

**Files:**
- Modify: `control/src/vonk_control/api.py`
- Create: `control/src/vonk_control/reconcile.py`
- Create: `src/cluster_profiles/control_client.py`
- Modify: `src/cluster_profiles/cli.py`
- Test: `control/tests/test_admin_api.py`
- Test: `tests/cluster_profiles/test_control_client.py`

**Interfaces:**
- API: `/api/v1/repository`, `/documents`, `/proposals`, `/changes`, `/reconciliations`.
- CLI: `vonkctl admin fleet|models|profiles|proposal|deploy|jobs|audit` using the API.

- [ ] **Step 1: Write failing API/CLI equivalence test**

```python
def test_cli_and_api_create_same_proposal(api_client, run_cli, change):
    api = api_client.post("/api/v1/proposals", json=change).json()
    cli = json.loads(run_cli("admin", "proposal", "--file", change.path, "--json").stdout)
    assert (api["digest"], api["patch"]) == (cli["digest"], cli["patch"])
```

- [ ] **Step 2: Run and observe missing endpoints/commands**

Run: `uv run --project control pytest control/tests/test_admin_api.py -v && uv run pytest tests/cluster_profiles/test_control_client.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement thin API and CLI adapters over shared services**

Require base commit on every proposal, enqueue reconciliation as a durable job, expose read-only planning before apply, use generated OpenAPI types where possible, and make offline CLI reject these normal operations.

- [ ] **Step 4: Run API/CLI tests**

Run: `uv run --project control pytest control/tests/test_admin_api.py -v && uv run pytest tests/cluster_profiles/test_control_client.py -v`
Expected: PASS.

- [ ] **Step 5: Commit interfaces**

```bash
git add control/src/vonk_control/api.py control/src/vonk_control/reconcile.py src/cluster_profiles/control_client.py src/cluster_profiles/cli.py control/tests/test_admin_api.py tests/cluster_profiles/test_control_client.py
git commit -m "feat: administer repository state through API and CLI"
```

### Task 5: Build web fleet, profile, model, job, and audit experience

**Files:**
- Create: `control/web/package.json`
- Create: `control/web/src/app.tsx`
- Create: `control/web/src/api/`
- Create: `control/web/src/pages/fleet.tsx`
- Create: `control/web/src/pages/profiles.tsx`
- Create: `control/web/src/pages/models.tsx`
- Create: `control/web/src/pages/jobs.tsx`
- Create: `control/web/src/pages/audit.tsx`
- Create: `control/web/src/components/proposal-diff.tsx`
- Create: `control/web/src/**/*.test.tsx`
- Create: `control/web/e2e/admin.spec.ts`

**Interfaces:**
- UI uses only `/api/v1`; no shell, Git, SSH, or direct database access.
- Editors require base commit, typed fields, validation, diff preview, and explicit submit.
- UI links to Caddy-protected LiteLLM administration for keys/teams/spend and Grafana for dashboards; it does not duplicate them or grant LiteLLM model authority.

- [ ] **Step 1: Write failing component and browser workflow tests**

```typescript
it("shows validation and canonical diff before submit", async () => {
  render(<ProfileEditor api={fakeApi} />)
  await userEvent.click(screen.getByRole("button", {name: "Preview change"}))
  expect(await screen.findByText("Base commit")).toBeVisible()
  expect(screen.getByTestId("canonical-diff")).toBeVisible()
  expect(screen.getByRole("button", {name: "Submit change"})).toBeEnabled()
})
```

- [ ] **Step 2: Run and verify web project is absent**

Run: `npm --prefix control/web test -- --run`
Expected: FAIL because `package.json` is absent.

- [ ] **Step 3: Implement accessible routed application and generated API client**

Provide fleet health/topology, onboarding job progress, typed model/profile
editors, affected-target diff, development commit versus PR status,
reconciliation plan, job logs, audit filtering, and contextual links to native
LiteLLM/Grafana pages. Generate TypeScript types with `openapi-typescript` and
call through `openapi-fetch`; check generated output drift in CI. Use semantic
HTML, keyboard navigation, explicit destructive confirmations, and no hidden
direct mutation.

- [ ] **Step 4: Run unit and Playwright tests against disposable API**

Run: `npm --prefix control/web test -- --run && npm --prefix control/web run build && npm --prefix control/web run test:e2e`
Expected: PASS.

- [ ] **Step 5: Commit web administration**

```bash
git add control/web control/Dockerfile deploy/compose/compose.yaml
git commit -m "feat: add web cluster administration"
```

### Task 6: Verify reconciliation is merged-commit-only and fail-closed

**Files:**
- Modify: `control/src/vonk_control/reconcile.py`
- Create: `control/tests/test_reconcile.py`
- Create: `docs/runbooks/repository-administration.md`

**Interfaces:**
- `Reconciler.plan(commit) -> ReconciliationPlan`; `enqueue(plan_digest, actor) -> Job`.
- Plan pins eligible commit, exact placements, routes, releases, and input digests.

- [ ] **Step 1: Write failing race and route-withdrawal tests**

```python
def test_reconcile_rechecks_commit_eligibility_before_mutation(reconciler, code_host):
    plan = reconciler.plan(code_host.merged_passing_commit)
    code_host.revoke_required_check(plan.commit)
    with pytest.raises(IneligibleCommit):
        reconciler.execute(plan)
    assert reconciler.node_calls == []


def test_failed_reconcile_leaves_affected_routes_withdrawn(reconciler):
    result = reconciler.execute(reconciler.plan_with_start_failure())
    assert result.status == "failed"
    assert reconciler.routes.for_targets(result.targets) == "maintenance"
```

- [ ] **Step 2: Run and observe missing gates**

Run: `uv run --project control pytest control/tests/test_reconcile.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement eligibility recheck, leases, dry plan, and fail-closed lifecycle**

Verify commit and proposal digests immediately before mutation, withdraw only affected routes, acquire node leases in sorted ID order, call generic controller operations, require health/acceptance, publish atomically, and record terminal reconciliation plus audit event.

- [ ] **Step 4: Run Phase 4 integration**

Run: `uv run --project control pytest -v && uv run pytest tests/cluster_profiles -v && npm --prefix control/web test -- --run && git diff --check`
Expected: PASS.

- [ ] **Step 5: Commit reconciliation**

```bash
git add control/src/vonk_control/reconcile.py control/tests/test_reconcile.py docs/runbooks/repository-administration.md
git commit -m "feat: reconcile eligible repository state"
```
