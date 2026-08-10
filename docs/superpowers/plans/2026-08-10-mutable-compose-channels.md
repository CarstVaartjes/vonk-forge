# Mutable Compose Channels Implementation Plan

> **For Codex:** Execute this plan with `superpowers:subagent-driven-development`, use `superpowers:test-driven-development` for every behavior change, and run `superpowers:verification-before-completion` before committing or publishing.

**Goal:** Publish one unchanged NAS development Compose file that safely follows the accepted `:dev` image cohort, while keeping production deployment behind the signed `stable` host-updater boundary and treating `:latest` as an evaluation alias only.

**Architecture:** Each development API and worker image contains canonical, public build identity. Networkless one-shot Compose services reset a transient cohort volume, report each image identity, and verify both roles match before repository initialization or migration. The verifier writes one selected-cohort document; development startup derives its synthetic generation identity from that document, while production startup continues to require exact TUF-selected image digests and the root-owned active projection. GitHub Actions renders both mutable and pinned development artifacts, accepts immutable images first, and moves `:dev` only after all evidence and artifacts pass.

**Tech Stack:** Python 3.12, pytest, Docker/BuildKit, Docker Compose, GitHub Actions YAML, canonical JSON, GHCR/Skopeo.

---

## Task 1: Define canonical development image and cohort identity

**Files:**

- Create: `control/src/vonk_control/dev_cohort.py`
- Create: `control/tests/test_dev_cohort.py`
- Modify: `control/src/vonk_control/__init__.py` only if shared version export is needed

**Step 1: Write failing parser and canonicalization tests**

Add tests for `DevelopmentImageIdentity.from_bytes(raw, expected_role=...)` covering the exact schema: `schema_version`, `source_repository`, `source_commit`, `channel`, `platform_version`, `build_digest`, `database_revision`, `protocol_minimum`, `protocol_maximum`, and `image_role`. Require canonical ASCII JSON with one trailing newline, unique fields, no unknown fields, a public Vonk Forge repository, lowercase full commit, `channel == "development"`, semantic platform version, digest syntax, bounded protocol range, and role `api` or `worker`. Reject malformed, duplicate-field, non-canonical, oversized, wrong-repository, wrong-channel, wrong-role, and symlinked input.

Run: `uv run --project control --frozen pytest control/tests/test_dev_cohort.py -q`

Expected: FAIL because `vonk_control.dev_cohort` does not exist.

**Step 2: Implement the immutable identity model and safe reader**

Implement:

- `DevelopmentCohortError`
- `DevelopmentImageIdentity`
- `canonical_json(value) -> bytes`
- `read_identity(path, *, expected_role) -> DevelopmentImageIdentity`
- `build_identity(*, role, source_commit) -> DevelopmentImageIdentity`

Derive `build_digest` from the canonical role-independent identity fields so API and worker from one source commit have the same cohort digest. Keep platform version `0.1.0`, database revision `0020_recipe_catalog_bridge`, protocol range `1..2`, source repository `https://github.com/CarstVaartjes/vonk-forge`, and the fixed embedded path `/usr/local/share/vonk-forge/development-image-identity.json` in this module so workflow, runtime, and initializer do not duplicate values.

**Step 3: Write failing cohort comparison and selected-document tests**

Test `verify_cohort(api, worker)` rejects mismatched commit, build digest, version, database revision, protocol range, repository, channel, and duplicate/missing roles. Test that a matching pair produces one canonical `SelectedDevelopmentCohort` document containing the common fields plus role-specific public identity digests and deterministic synthetic API/worker process references under `development.invalid`, a deterministic generation ID, release/build digests, and start nonce. Test round-trip parsing and tamper rejection.

**Step 4: Implement cohort verification and derivation**

Implement:

- `DevelopmentCohort`
- `SelectedDevelopmentCohort`
- `verify_cohort(identities) -> SelectedDevelopmentCohort`
- `SelectedDevelopmentCohort.from_bytes(raw)`
- deterministic synthetic development generation fields derived only from canonical accepted cohort metadata

The synthetic process references must remain digest-shaped for the existing generation validator but use `development.invalid` so they cannot be mistaken for pullable GHCR manifest identities. Do not weaken `HostOperationPlan`, `GenerationReceipt`, production settings, or production image-reference validation.

**Step 5: Run focused tests**

Run: `uv run --project control --frozen pytest control/tests/test_dev_cohort.py -q`

Expected: PASS.

## Task 2: Embed and independently verify role identity in both images

**Files:**

- Modify: `control/Dockerfile`
- Modify: `.github/workflows/dev-images.yml`
- Modify: `scripts/verify-dev-image-secrets`
- Modify: `scripts/tests/test_verify_dev_image_secrets.py`
- Modify: `scripts/tests/test_dev_image_workflow.py`

**Step 1: Write failing Dockerfile and workflow contract tests**

Require both `api` and `worker` targets to receive the exact accepted source commit and create the fixed identity file in a role-specific intermediate stage. Require the workflow to pass `GITHUB_SHA` as a build argument to both immutable OCI builds and to verify each loaded image's embedded identity before acceptance. Require no credential or runtime-secret build argument.

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest scripts/tests/test_dev_image_workflow.py scripts/tests/test_verify_dev_image_secrets.py -q`

Expected: FAIL on missing build identity contracts.

**Step 2: Add role-specific runtime stages**

Add a validated `VONK_DEV_SOURCE_COMMIT` build argument with a harmless all-zero local-build default. Create `worker-root` and extend `api-root`; in each stage call the `dev_cohort` build command to atomically write the canonical identity with mode `0444`. Copy those roots into the existing scratch targets. Do not add secrets, registry credentials, Docker socket access, or extra network clients to the worker image.

**Step 3: Extend image inspection**

Extend `scripts/verify-dev-image-secrets` (or a narrowly scoped companion invoked by it) to execute the installed module inside each loaded image, verify the expected role and workflow commit, and compare the embedded source commit to the OCI revision label. Keep the existing filesystem/private-key scan intact.

**Step 4: Wire exact build metadata in Actions**

Pass `--build-arg VONK_DEV_SOURCE_COMMIT="$GITHUB_SHA"` to both builds. Make the pre-publication acceptance command verify role, commit, repository, and canonical identity. Keep immutable publication, SBOM, provenance, attestations, Compose artifacts, and alias promotion in their current authority order.

**Step 5: Run focused tests**

Run the tests from Step 1 and `scripts/verify-public-image-inputs`.

Expected: PASS.

## Task 3: Add the fail-closed runtime cohort gate

**Files:**

- Modify: `control/src/vonk_control/dev_cohort.py`
- Modify: `control/tests/test_dev_cohort.py`
- Modify: `deploy/compose/compose.dev.images.yaml`
- Modify: `deploy/compose/tests/test_dev_compose.py`

**Step 1: Write failing CLI filesystem-safety tests**

Test commands/functions for:

- `reset_cohort_root(path)`: root-only, normalized absolute path, no symlink traversal, clears only regular report/selection files from the dedicated cohort volume, then prepares a UID/GID 10001 writable directory;
- `report_identity(root, role)`: reads the fixed embedded identity, verifies its role, writes exactly `<role>.json` atomically, and refuses an existing/symlinked/unsafe destination;
- `select_cohort(root)`: requires exactly `api.json` and `worker.json`, verifies both, and atomically writes `selected.json` read-only;
- `require_selected_cohort(path, role)`: compares the selected document with the current image's embedded identity;
- CLI subcommands `build-identity`, `reset`, `report`, `verify`, and `run-selected`.

For `run-selected`, test that mismatch prevents `os.execvp` and a match replaces the process only after verification.

**Step 2: Implement safe cohort-volume lifecycle and CLI**

Use descriptor-relative `O_NOFOLLOW` reads/writes, 64 KiB limits, stable identity checks before/after reads, atomic replace plus fsync, and strict file modes. Reporters and verifier must operate without network or secrets. `run-selected --role api -- <command>` verifies and then execs the migration command.

**Step 3: Write failing Compose graph tests**

Require these one-shot services in order:

1. `dev-cohort-reset` from API `:dev`, root, `network_mode: none`;
2. `dev-api-cohort` from API `:dev`, UID 10001, no secrets, `network_mode: none`;
3. `dev-worker-cohort` from worker `:dev`, UID 10001, no secrets, `network_mode: none`;
4. `dev-cohort-verify` from API `:dev`, UID 10001, no secrets, `network_mode: none`;
5. `dev-init` and `migrate` depend on successful verification.

Require one dedicated `dev-image-cohort` named volume, mounted writable only by the one-shot gate and read-only by initializer, migration, API, and worker. Require `pull_policy: always` on every service that uses a mutable first-party reference. Assert no gate service receives host secrets, runtime-secret projections, repository, control state, or Docker socket.

**Step 4: Implement the Compose gate**

Add the services and dependency conditions. Remove the rendered expected-commit literal from the mutable template; set `VONK_DEV_SELECTED_COHORT_FILE=/cohort/selected.json` for `dev-init`, API, worker, and migration. Wrap migration with `dev_cohort run-selected`. Ensure PostgreSQL remains independently digest pinned and secret-backed.

**Step 5: Run focused tests**

Run: `uv run --project control --frozen pytest control/tests/test_dev_cohort.py -q && uv run --python 3.12 --frozen --with pytest==9.1.1 pytest deploy/compose/tests/test_dev_compose.py -q`

Expected: PASS.

## Task 4: Derive development startup identity from the verified cohort

**Files:**

- Modify: `control/src/vonk_control/dev_init.py`
- Modify: `control/src/vonk_control/settings.py`
- Modify: `control/tests/test_dev_init.py`
- Modify: `control/tests/test_settings.py`
- Modify: `control/tests/test_generation_readiness.py`
- Modify: `deploy/compose/compose.dev.images.yaml`

**Step 1: Write failing initializer tests**

Test that `dev_init.main()` supports exactly two development identity inputs:

- local source mode: existing `VONK_DEV_EXPECTED_COMMIT`, `VONK_DEV_API_IMAGE`, and `VONK_DEV_WORKER_IMAGE` digest-shaped values;
- image-only mutable and pinned modes: `VONK_DEV_SELECTED_COHORT_FILE`, with no rendered commit or image value in the initializer environment.

Mutable mode must parse and verify `selected.json` against the initializer image before repository fetch, secret staging, or state writes. It must use the selected source commit to advance `refs/heads/main` while preserving locally advanced `deploy`, and write an active projection from selected cohort fields. Reject mixed input modes, malformed/stale cohort files, role mismatch, and missing input.

**Step 2: Refactor development projection creation**

Change `_active_projection` and `_write_active_projection` to accept one explicit development generation identity object. Preserve the current local-source adapter. For image-only mutable and pinned modes, use selected cohort version, database revision, build/release digests, generation ID, and `development.invalid` API/worker references. Keep the production host-state schema and validators unchanged.

**Step 3: Write failing startup-settings tests**

Test `GenerationStartupSettings.from_env_and_secrets()` behavior:

- production and test continue to require all explicit immutable generation variables exactly as before;
- development with no cohort file continues to support the local source Compose variables;
- development with `VONK_DEV_SELECTED_COHORT_FILE` derives generation ID, release/build digest, platform version, role-specific process identity, database revision, and start nonce from the verified selected cohort;
- role is required through `VONK_CONTROL_PROCESS_ROLE=api|worker` and must match the embedded image;
- explicit generation variables may not conflict with cohort-derived mode.

**Step 4: Implement cohort-derived generation settings**

Load the selected document with the hardened reader, compare it with the fixed embedded identity, and map it into `GenerationStartupSettings`. Keep the direct path structurally unchanged for production. Update API/worker Compose environments to specify only role plus selected-cohort path for dynamic identity fields.

**Step 5: Verify readiness remains fail closed**

Add readiness tests proving the derived API and worker identities match the active projection and a stale/tampered cohort fails before application construction or worker startup.

Run: `uv run --project control --frozen pytest control/tests/test_dev_init.py control/tests/test_settings.py control/tests/test_generation_readiness.py -q`

Expected: PASS.

## Task 5: Render a genuinely mutable development artifact and retain pinned recovery

**Files:**

- Modify: `scripts/render-dev-compose`
- Modify: `scripts/tests/test_render_dev_compose.py`
- Modify: `.github/workflows/dev-images.yml`
- Modify: `scripts/tests/test_dev_image_workflow.py`
- Modify: `scripts/dev-image-acceptance`
- Modify: `scripts/tests/test_dev_image_acceptance.py`

**Step 1: Write failing renderer tests**

Split renderer contracts by channel:

- `channel="dev"` accepts only exact bare public refs `ghcr.io/carstvaartjes/vonk-forge-api:dev` and `...worker:dev`, takes no commit, emits `pull_policy: always`, no first-party `@sha256:`, no `VONK_DEV_EXPECTED_COMMIT`, and no unresolved token;
- `channel="pinned"` still requires matching `dev-sha-<commit>@sha256:<digest>` refs and a lowercase full commit;
- both rendered modes retain `VONK_DEV_SELECTED_COHORT_FILE` for initializer, migration, API, and worker so one verified cohort is the only runtime identity source;
- neither mode accepts `:latest`, another owner/repository, another tag, interpolation, or caller-controlled output paths.

**Step 2: Split template substitutions without weakening validation**

Use channel-specific token sets or two explicit template branches. Keep atomic output replacement, synthetic-secret Compose validation, fixed environment, and cleanup guarantees. Do not make commit optional in pinned mode; make it forbidden/unneeded in mutable mode.

**Step 3: Update workflow artifact rendering**

Render:

- `dist/docker-compose.pinned.yml` from immutable tag+digest and exact commit;
- `dist/docker-compose.dev.yml` from the two exact bare `:dev` references.

Render and validate both before alias promotion. Rename the step to `Render development Compose artifacts`. Preserve the rule that `Advance accepted development aliases` is the final registry mutation and occurs only after artifact upload and authority recheck.

**Step 4: Extend lifecycle acceptance**

Teach `scripts/dev-image-acceptance` to exercise the cohort gate with loaded immutable images before publication. Add a lifecycle fixture that aliases cohort A, starts it, aliases cohort B, pulls/redeploys the unchanged Compose file, and observes B; add a mixed A/B fixture that proves `dev-init` and migration never run. Keep all destructive Docker activity inside the randomly named temporary Compose project.

**Step 5: Run focused tests and local image acceptance**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest -q \
  scripts/tests/test_render_dev_compose.py \
  scripts/tests/test_dev_image_workflow.py \
  scripts/tests/test_dev_image_acceptance.py \
  deploy/compose/tests/test_dev_compose.py
```

Then build both local targets with a real 40-hex commit build arg and run `scripts/dev-image-acceptance` against them.

Expected: PASS, with the unchanged mutable Compose graph selecting the newer matching cohort and mixed roles stopping before mutation.

## Task 6: Document the operator-safe development, production, and recovery paths

**Files:**

- Modify: `docs/runbooks/development-nas-installation.md`
- Modify: `deploy/compose/README.md`
- Modify: `tests/runbooks/test_development_nas_installation.py`
- Modify: `docs/superpowers/specs/2026-08-10-mutable-compose-channels-design.md` only for implementation-discovered precision

**Step 1: Write failing runbook contracts**

Require the development guide to lead with exactly `docker-compose.yml` plus `secrets/`, identify `docker-compose.dev.yml` as bare mutable `:dev`, say pull/redeploy rather than restart, and explain that the cohort gate exits before migration on a mixed pull. Require the production guide to say `stable` is selected through the trusted host updater and `:latest` is evaluation/discovery only. Require pinned rollback wording to state repository-volume reset for compatible schemas and matching full-state restore for incompatible migrations.

**Step 2: Rewrite operator instructions**

Provide UGREEN/generic Docker UI steps for creating the project, copying three NAS secret files, pulling/redeploying unchanged `docker-compose.yml`, checking one-shot job order, and diagnosing mixed cohorts without deleting state. Keep shell commands only in advanced guarded recovery sections and preserve the exact volume-label/identity checks before repository-volume deletion.

**Step 3: Run documentation tests**

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest tests/runbooks/test_development_nas_installation.py -q`

Expected: PASS.

## Task 7: Full verification, review, and publication

**Files:**

- Verify all modified files
- Update: implementation plan checkboxes/status only if the repository convention requires it

**Step 1: Run format, lint, and static contracts**

Run the repository's documented Python formatting/linting/type checks for `control`, all workflow contract tests, `scripts/verify-public-image-inputs`, and `docker compose config` for local, mutable, and pinned development files.

**Step 2: Run complete relevant suites**

Run at minimum:

```bash
uv run --project control --frozen pytest control/tests -q
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest -q \
  deploy/compose/tests \
  scripts/tests \
  tests/runbooks/test_development_nas_installation.py
```

Run the repository-wide CI-equivalent commands affected by the patch. Record any pre-existing unrelated failure separately; do not claim success over it.

**Step 3: Run image lifecycle acceptance**

Build the actual API and worker targets with the current commit metadata, run secret scanning and `dev-image-acceptance`, and inspect the generated mutable Compose to confirm it contains only bare first-party `:dev` refs. Confirm no image layer or Compose artifact contains runtime secret values.

**Step 4: Request code review and address findings**

Use `superpowers:requesting-code-review` against the complete diff. Apply actionable findings with `superpowers:receiving-code-review` and rerun affected verification.

**Step 5: Commit and publish intentionally**

Inspect `git status` and `git diff --check`, commit the implementation on `feature/mutable-compose-channels`, push it, and open or update the draft PR. Do not create a release or advance any production channel locally; only GitHub Actions may publish images, release tags, APT repositories, or signed channel metadata.
