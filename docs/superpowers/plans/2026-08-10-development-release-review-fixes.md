# Development Release Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four accepted review findings and the date-sensitive agent test while keeping all official development and production release authority inside GitHub Actions.

**Architecture:** The development repository keeps accepted GitHub state on local `main` and mutable NAS-local state on checked-out `deploy`. A networked, secret-free repository initializer is separate from the network-disabled runtime initializer, which projects service-specific authority. The dedicated development release workflow supplies its monotonic GitHub run number to package metadata, while the reusable apt publisher uses Acquire-By-Hash for race-free index switching.

**Tech Stack:** Python 3.12, pytest, Git, Docker Compose YAML, GitHub Actions YAML, Bash, aptly, Debian version semantics.

## Global Constraints

- Official development and production versions are selected and published only by GitHub Actions; local helpers may validate an explicit fixture value but may not derive or publish an official release sequence.
- Development package versions are exactly `X.Y.Z~dev.<workflow-run-number>+g<sha12>`; production package versions remain exactly `X.Y.Z`.
- The dedicated `.github/workflows/agent-release.yml` identity must remain stable because its GitHub `run_number` is the monotonic development ordering authority and is stable across reruns.
- `main` is the last accepted GitHub baseline; `deploy` is the API's mutable NAS-local development branch.
- NAS source secret files remain file-backed inputs only to the network-disabled
  runtime `dev-init`; migration, API, worker, Caddy, and LiteLLM receive separate
  least-privilege named-volume projections.
- Migration receives only `database-url`; it must not receive the Git signing key, generated admin private key, or worker token.
- Initial apt publication must enable Acquire-By-Hash, and generated publication state must prove the release advertises it and the corresponding SHA256 index paths exist before upload.
- Production deadline behavior is unchanged; the agent CI repair removes test dependence on scheduler and filesystem timing.
- All behavior changes are test-driven: add or adjust a focused test, observe the expected failure, implement the smallest fix, and rerun the focused suite before committing.

---

### Task 1: Make terminal replay deadline tests deterministic

**Files:**
- Modify: `agent/tests/test_operations.py`
- Test: `agent/tests/test_operations.py`

**Interfaces:**
- Consumes: `MonotonicDeadline.bind(datetime)` and `DeadlineBindingError` from `vonk_agent.deadlines`.
- Produces: deterministic replay tests that simulate deadline rejection without changing production code or sleeping against a 30 ms wall-clock window.

- [ ] **Step 1: Replace the two wall-clock replay fixtures with an explicit expired-binding fixture**

Import `DeadlineBindingError` alongside `MonotonicDeadline`, add this test helper near the existing operation test helpers, and give both affected tests a `monkeypatch: pytest.MonkeyPatch` argument:

```python
def _reject_deadline_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(
        _cls: type[MonotonicDeadline],
        _value: datetime | MonotonicDeadline,
    ) -> MonotonicDeadline:
        raise DeadlineBindingError("deadline has elapsed")

    monkeypatch.setattr(MonotonicDeadline, "bind", classmethod(reject))
```

In `test_terminal_result_replays_after_deadline_without_local_execution` and `test_acknowledged_terminal_result_inspects_and_replays_after_restart_and_expiry`, use the normal one-minute `claim()` deadline, complete the first execution, then call `_reject_deadline_binding(monkeypatch)` before replay. Remove the 30 ms deadlines and `time.sleep(0.04)` calls. Preserve all existing replay, canonical-result, probe-count, restart, inspection, and conflict assertions.

- [ ] **Step 2: Run the focused tests repeatedly to prove they no longer depend on timing**

Run:

```bash
for run in 1 2 3 4 5; do
  uv run --project agent --frozen pytest \
    agent/tests/test_operations.py::test_terminal_result_replays_after_deadline_without_local_execution \
    agent/tests/test_operations.py::test_acknowledged_terminal_result_inspects_and_replays_after_restart_and_expiry \
    -q || exit 1
done
```

Expected: both tests pass in all five runs with no sleeps and no production-code diff.

- [ ] **Step 3: Run the complete operation-registry test module**

Run: `uv run --project agent --frozen pytest agent/tests/test_operations.py -q`

Expected: all tests pass.

- [ ] **Step 4: Commit the deterministic CI repair**

```bash
git add agent/tests/test_operations.py
git commit -S -m "test(agent): make expired replay checks deterministic"
```

---

### Task 2: Give migration a database-only staged secret projection

**Files:**
- Modify: `control/src/vonk_control/dev_init.py`
- Modify: `control/tests/test_dev_init.py`
- Modify: `deploy/compose/compose.dev.yaml`
- Modify: `deploy/compose/compose.dev.images.yaml`
- Modify: `deploy/compose/tests/test_dev_compose.py`
- Test: `control/tests/test_dev_init.py`
- Test: `deploy/compose/tests/test_dev_compose.py`

**Interfaces:**
- Consumes: source files `database-url` and `git-signing-key` mounted only into `dev-init` under `/host-secrets`.
- Produces: `stage_runtime_secrets(source, api_root, migrate_root, worker_root) -> None` and required environment variable `VONK_DEV_MIGRATE_SECRET_ROOT`.
- Produces: named volume `dev-migrate-secrets`, mounted by `dev-init` at `/migrate-secrets` and by `migrate` at `/run/secrets:ro`.

- [ ] **Step 1: Add failing projection-isolation tests**

Update every `stage_runtime_secrets` call to pass a dedicated `migrate_root`. Extend `test_stage_runtime_secrets_creates_disjoint_service_projections` with:

```python
assert {path.name for path in migrate_root.iterdir()} == {"database-url"}
assert (migrate_root / "database-url").read_bytes() == (
    b"postgresql://vonk:secret@postgres/vonk\n"
)
assert not (migrate_root / "git-signing-key").exists()
assert not (migrate_root / "admin-grant-private-key").exists()
assert not (migrate_root / "worker-api-token").exists()
```

Extend alias and symlink tests so every pair among API, migration, and worker roots must be logically and physically distinct. Extend the refresh test to prove the migration database copy updates while generated API and worker credentials remain stable. Extend `test_main_initializes_repository_synthetic_state_and_runtime_secrets` and the environment-preflight test with `VONK_DEV_MIGRATE_SECRET_ROOT`.

- [ ] **Step 2: Run the focused secret tests and observe failure**

Run: `uv run --project control --frozen --with-editable . pytest control/tests/test_dev_init.py -q`

Expected: FAIL because the initializer currently accepts only two projections and does not create the migration projection.

- [ ] **Step 3: Implement the three-way secret projection**

Change the public function to:

```python
def stage_runtime_secrets(
    source: Path,
    api_root: Path,
    migrate_root: Path,
    worker_root: Path,
) -> None:
```

Validate all three absolute roots before mutation, open all three with the existing descriptor-safe helpers, reject duplicate `(st_dev, st_ino)` identities, prepare and clear all projections, and write exactly:

```text
api:     database-url, git-signing-key, admin-grant-private-key
migrate: database-url
worker:  database-url, worker-api-token
```

Seal all three projections and close every opened descriptor in `finally`. In `main()`, require `VONK_DEV_MIGRATE_SECRET_ROOT` before initializing the repository and pass it to `stage_runtime_secrets`.

- [ ] **Step 4: Add failing Compose boundary assertions**

For both the local rendered Compose and the image template, assert:

```python
assert initializer["environment"]["VONK_DEV_MIGRATE_SECRET_ROOT"] == "/migrate-secrets"
assert init_volumes["/migrate-secrets"]["source"].endswith("dev-migrate-secrets")
assert migrate_volumes["/run/secrets"]["source"].endswith("dev-migrate-secrets")
assert migrate_volumes["/run/secrets"]["read_only"] is True
assert services["migrate"].get("secrets", []) == []
```

Also assert the migration volume source differs from both API and worker projection sources.

- [ ] **Step 5: Wire the dedicated migration volume into both development Compose files**

In both Compose files:

- add `VONK_DEV_MIGRATE_SECRET_ROOT: /migrate-secrets` to `dev-init`;
- mount `dev-migrate-secrets:/migrate-secrets` into `dev-init`;
- replace `migrate`'s file-backed `secrets: [database-url]` with `dev-migrate-secrets:/run/secrets:ro`;
- declare `dev-migrate-secrets: {}` under top-level volumes;
- retain the top-level `database-url` secret because `dev-init` still consumes it.

- [ ] **Step 6: Run focused control and Compose tests**

Run:

```bash
uv run --project control --frozen --with-editable . pytest control/tests/test_dev_init.py -q
uv run --frozen pytest deploy/compose/tests/test_dev_compose.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the migration secret isolation fix**

```bash
git add control/src/vonk_control/dev_init.py control/tests/test_dev_init.py \
  deploy/compose/compose.dev.yaml deploy/compose/compose.dev.images.yaml \
  deploy/compose/tests/test_dev_compose.py
git commit -S -m "fix(compose): isolate migration database secret"
```

---

### Task 3: Separate accepted `main` from mutable `deploy`

**Files:**
- Modify: `control/src/vonk_control/dev_init.py`
- Modify: `control/tests/test_dev_init.py`
- Modify: `deploy/compose/compose.dev.yaml`
- Modify: `deploy/compose/compose.dev.images.yaml`
- Modify: `deploy/compose/tests/test_dev_compose.py`
- Test: `control/tests/test_dev_init.py`
- Test: `deploy/compose/tests/test_dev_compose.py`

**Interfaces:**
- Consumes: exact accepted commit `VONK_DEV_EXPECTED_COMMIT`, fetched `refs/remotes/origin/main`, and prior local accepted `refs/heads/main`.
- Produces: checked-out mutable `refs/heads/deploy`; local `refs/heads/main` remains the last accepted GitHub baseline.
- Produces: `VONK_DEPLOYMENT_BRANCH=deploy` for API and worker.

- [ ] **Step 1: Rewrite repository tests around the two-branch invariant**

Change fresh-clone assertions to require `HEAD == deploy` and both `main` and `deploy` equal the expected commit. Change the no-local-change update test to require both refs fast-forward to the next accepted commit while unrelated refs remain unchanged.

Add a regression test that:

1. initializes at accepted commit A;
2. creates a clean local commit L on checked-out `deploy`;
3. reruns initialization for A and proves `deploy == L`;
4. pushes accepted commit B to origin and initializes for B;
5. proves `main == B`, `deploy == L`, `HEAD == deploy`, the local file remains present, and the worktree is clean.

Update divergence coverage to force local `main` away from the prior accepted baseline and require failure, while ordinary commits on `deploy` remain valid. Preserve changed-origin, dirty-worktree, missing-commit, rollback, and unrelated-ref coverage.

- [ ] **Step 2: Run repository tests and observe the current `main`-branch behavior fail**

Run: `uv run --project control --frozen --with-editable . pytest control/tests/test_dev_init.py -q`

Expected: FAIL because fresh initialization checks out `main` and existing initialization rejects local direct commits.

- [ ] **Step 3: Implement fresh and existing repository transitions**

Fresh initialization must fetch and verify expected `origin/main`, set local `main` to the expected commit, and check out `deploy` at that commit.

Existing initialization must:

```text
require clean worktree and HEAD == deploy
accepted := refs/heads/main
deployed := refs/heads/deploy
fetch origin/main and verify expected is reachable
require accepted is an ancestor of expected
require accepted is an ancestor of deployed
compare-and-swap refs/heads/main from accepted to expected
if deployed == accepted:
    compare-and-swap refs/heads/deploy from deployed to expected
    reset the clean checked-out worktree to expected
else:
    preserve deploy and its clean worktree unchanged
```

Final validation must require a clean worktree, `HEAD == deploy`, and local `main == expected`. Error messages must distinguish a divergent accepted baseline from a deployment branch that no longer descends from that baseline.

- [ ] **Step 4: Change both development Compose graphs to select `deploy`**

Set the shared `VONK_DEPLOYMENT_BRANCH` value to `deploy` in both `deploy/compose/compose.dev.yaml` and `deploy/compose/compose.dev.images.yaml`. Update Compose tests to require `deploy` for API and worker.

- [ ] **Step 5: Run focused repository and Compose tests**

Run:

```bash
uv run --project control --frozen --with-editable . pytest control/tests/test_dev_init.py -q
uv run --frozen pytest deploy/compose/tests/test_dev_compose.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit the repository transition fix**

```bash
git add control/src/vonk_control/dev_init.py control/tests/test_dev_init.py \
  deploy/compose/compose.dev.yaml deploy/compose/compose.dev.images.yaml \
  deploy/compose/tests/test_dev_compose.py
git commit -S -m "fix(dev): preserve local deployment history"
```

---

### Task 4: Enable and verify apt Acquire-By-Hash

**Files:**
- Modify: `.github/workflows/agent-apt-publish.yml`
- Modify: `tests/test_agent_release_workflow.py`
- Test: `tests/test_agent_release_workflow.py`

**Interfaces:**
- Consumes: aptly initial `publish snapshot` and later `publish switch` paths in the reusable apt publisher.
- Produces: initial publications with `-acquire-by-hash`, `Acquire-By-Hash: yes`, and SHA256-addressed immutable package indexes.

- [ ] **Step 1: Add a failing workflow-contract test**

Add `test_reusable_apt_publisher_enables_and_verifies_by_hash_indexes`. Extract the `Generate missing aptly state or public tree` step and assert all of these are present:

```python
assert "-acquire-by-hash" in local
assert "Acquire-By-Hash: yes" in local
assert "by-hash/SHA256" in local
assert local.index("publish snapshot") < local.index("Acquire-By-Hash: yes")
```

Keep the existing manifest-last publication-order assertions.

- [ ] **Step 2: Run the focused test and observe failure**

Run: `uv run --frozen pytest tests/test_agent_release_workflow.py::test_reusable_apt_publisher_enables_and_verifies_by_hash_indexes -q`

Expected: FAIL because the initial aptly publication does not enable by-hash.

- [ ] **Step 3: Enable and fail-closed verify the generated by-hash tree**

Add `-acquire-by-hash` only to the initial `aptly publish snapshot` command; restored publication switches preserve the existing setting. After publication and before bundle/commit/upload:

- require a non-empty `Release` file;
- require an exact `Acquire-By-Hash: yes` field;
- parse the `SHA256:` section's `Packages`, `Packages.gz`, and other `Packages.*` entries;
- require each named index's digest-addressed sibling at `<index-directory>/by-hash/SHA256/<digest>` to exist and match the recorded byte size;
- require at least one package index to have been checked.

Use only fixed paths rooted below `$work/public/dists/$DISTRIBUTION`; reject malformed relative index names before path construction.

- [ ] **Step 4: Run the workflow contract suite**

Run: `uv run --frozen pytest tests/test_agent_release_workflow.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the apt publication fix**

```bash
git add .github/workflows/agent-apt-publish.yml tests/test_agent_release_workflow.py
git commit -S -m "fix(apt): publish immutable by-hash indexes"
```

---

### Task 5: Make GitHub Actions own monotonic development package versions

**Files:**
- Modify: `scripts/agent-package-metadata`
- Modify: `.github/workflows/agent-release.yml`
- Modify: `.github/workflows/agent-package-build.yml`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/scripts/test_agent_package_metadata.py`
- Modify: `tests/test_agent_release_workflow.py`
- Modify: `tests/test_container_release_workflow.py`
- Test: `tests/scripts/test_agent_package_metadata.py`
- Test: `tests/test_agent_release_workflow.py`
- Test: `tests/test_container_release_workflow.py`

**Interfaces:**
- Consumes: fifth `scripts/agent-package-metadata` argument as `publication_sequence` rather than commit epoch.
- Produces: development versions ordered by positive GitHub workflow run number; production calls use the exact sentinel sequence `0` because their stable version is tag-derived.
- Produces: required reusable-workflow string input `publication_sequence`.

- [ ] **Step 1: Add failing package metadata tests for Actions-owned ordering**

Change the canonical development fixture to sequence `417` and expect:

```text
version=0.1.0~dev.417+g0123456789ab
next_version=0.1.0~dev.418+g0123456789ab
package=vonk-forge-agent_0.1.0~dev.417+g0123456789ab_arm64.deb
snapshot=dev-0.1.0~dev.417+g0123456789ab
```

Production metadata must pass `0`. Add invalid development cases for `0`, negative, leading-zero, nonnumeric, and an overlong sequence. Add an invalid production case with a nonzero sequence. Add a Debian-ordering regression using a lexically high SHA at sequence 417 and a lexically low SHA at sequence 418; assert the latter version is greater with `dpkg --compare-versions`.

- [ ] **Step 2: Run metadata tests and observe failure**

Run: `uv run --frozen pytest tests/scripts/test_agent_package_metadata.py -q`

Expected: FAIL because the helper still interprets the final argument as a commit epoch and accepts production epochs.

- [ ] **Step 3: Implement explicit publication-sequence validation**

Replace epoch parsing with channel-aware sequence parsing:

```python
def publication_sequence(release: str, value: str) -> int:
    if release == "production":
        if value != "0":
            raise ValueError("publication sequence is invalid")
        return 0
    if re.fullmatch(r"[1-9][0-9]{0,18}", value) is None:
        raise ValueError("publication sequence is invalid")
    sequence = int(value)
    if sequence >= 9_999_999_999_999_999_999:
        raise ValueError("publication sequence is invalid")
    return sequence
```

Use `sequence` and `sequence + 1` in development package versions. Keep SHA, branch/tag, workspace-version, artifact-name, channel, and snapshot validation unchanged.

- [ ] **Step 4: Add failing workflow authority tests**

Require the development metadata job to pass `${{ github.run_number }}` through an environment variable and explicitly compare it with `$GITHUB_RUN_NUMBER`. Require the reusable package workflow to declare `publication_sequence`, receive it from the development caller, and validate development metadata with it. Require the stable caller in `.github/workflows/ci.yml` to pass the exact string `'0'`.

The tests must reject any remaining use of `git show --format=%ct` as package-version metadata input while permitting it in package build steps as `SOURCE_DATE_EPOCH` for reproducible bytes.

- [ ] **Step 5: Wire the Actions-owned sequence through both release paths**

In `.github/workflows/agent-release.yml`:

```yaml
env:
  PUBLICATION_SEQUENCE: ${{ github.run_number }}
```

Require `PUBLICATION_SEQUENCE == GITHUB_RUN_NUMBER`, pass it as metadata's fifth argument, and pass `${{ github.run_number }}` as the reusable build's `publication_sequence` input.

In `.github/workflows/agent-package-build.yml`, declare required string input `publication_sequence`, expose it to metadata validation, require it equals `$GITHUB_RUN_NUMBER` for `dev`, require it equals `0` for `stable`, and pass it to `scripts/agent-package-metadata`. Keep commit epoch derivation only in reproducible package build commands.

In `.github/workflows/ci.yml`, pass `0` to production metadata and `publication_sequence: '0'` to the reusable stable build.

- [ ] **Step 6: Run focused metadata and workflow suites**

Run:

```bash
uv run --frozen pytest \
  tests/scripts/test_agent_package_metadata.py \
  tests/test_agent_release_workflow.py \
  tests/test_container_release_workflow.py \
  -q
```

Expected: all tests pass.

- [ ] **Step 7: Run repository-wide static checks for stale release semantics**

Run:

```bash
rg -n "commit-epoch|agent-package-metadata.*epoch|~dev\.<commit" \
  .github scripts tests docs || true
git diff --check
```

Expected: no stale commit-epoch development-version contract remains; any `git show --format=%ct` references are confined to reproducible build timestamps.

- [ ] **Step 8: Commit the Actions-only release sequencing fix**

```bash
git add scripts/agent-package-metadata .github/workflows/agent-release.yml \
  .github/workflows/agent-package-build.yml .github/workflows/ci.yml \
  tests/scripts/test_agent_package_metadata.py \
  tests/test_agent_release_workflow.py tests/test_container_release_workflow.py
git commit -S -m "fix(release): order development packages in Actions"
```

---

## Final integrated verification

After all five reviewed task commits:

```bash
uv run --project agent --frozen pytest agent/tests -q
uv run --project control --frozen --with-editable . pytest control/tests/test_dev_init.py -q
uv run --frozen pytest \
  deploy/compose/tests/test_dev_compose.py \
  tests/scripts/test_agent_package_metadata.py \
  tests/test_agent_release_workflow.py \
  tests/test_container_release_workflow.py \
  -q
git diff --check origin/main...HEAD
```

Render both development Compose graphs with repository test fixtures and verify that migration runs as UID 10001 with only its staged database projection, API and worker select `deploy`, and no Compose output contains secret values. Then run the repository's standard CI-equivalent checks relevant to changed files, request a whole-branch code review, push the reviewed commits to `codex/public-dev-images`, and recheck PR #48 Actions status.
