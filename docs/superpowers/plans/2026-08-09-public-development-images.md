# Public Development Images Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish tested public API/worker development images from `main` and generate one digest-pinned Compose file that runs on the NAS with only operator-owned runtime secret files.

**Architecture:** GitHub Actions builds API and worker images once into OCI archives, scans and tests those exact archives through a complete image-only Compose lifecycle, then pushes the same manifests to GHCR. The API image supplies a one-shot development initializer that maintains a NAS-local Git checkout and disjoint API/worker secret volumes. Signed `vX.Y.Z` releases promote the already-tested development digests instead of rebuilding them.

**Tech Stack:** Python 3.12, Bash, Docker Buildx, Docker Compose, OCI archives, Skopeo, GHCR, GitHub Actions, pytest.

## Global Constraints

- `main` is the sole development publication branch.
- Manual publication must select the current `origin/main` tip.
- Production starts only from signed `vX.Y.Z` tags and promotes existing development digests.
- No image build receives GitHub tokens, runtime credentials, build secrets, or secret-valued build arguments.
- The NAS project contains only `docker-compose.yml` and `secrets/{postgres-password,database-url,git-signing-key}`.
- API and worker never mount the SMB project directory as `/repository`.
- API and worker receive disjoint runtime-secret named volumes.
- Existing PostgreSQL and repository volumes survive ordinary redeployment.
- No Compose file is copied to `Z:` until the exact image-only stack passes locally.
- A local acceptance-only repository override may use a temporary local origin
  when `VONK_DEV_LOCAL_ACCEPTANCE=1`; the published template and rendered NAS
  Compose must never contain that switch or a non-public repository URL.

---

### Task 1: Move development initialization into the API package

**Files:**
- Create: `control/src/vonk_control/dev_init.py`
- Create: `control/tests/test_dev_init.py`
- Delete: `scripts/dev-compose-init.py`

**Interfaces:**
- Produces: `python -m vonk_control.dev_init`.
- Produces: `initialize_repository(root: Path, repository_url: str, expected_commit: str) -> None`.
- Produces: `stage_runtime_secrets(source: Path, api_root: Path, worker_root: Path) -> None`.
- Consumes: `VONK_DEV_EXPECTED_COMMIT`, `VONK_DEV_REPOSITORY_URL`, `VONK_REPOSITORY_PATH`, `VONK_DEV_SECRET_SOURCE_ROOT`, `VONK_DEV_API_SECRET_ROOT`, and `VONK_DEV_WORKER_SECRET_ROOT`.
- Consumes: `VONK_DEV_LOCAL_ACCEPTANCE=1` only for the unpublished local
  acceptance harness; ordinary runtime initialization rejects local origins.

- [ ] **Step 1: Write failing fresh-clone and branch tests**

Create a temporary public-style bare origin with `main`, call
`initialize_repository`, and assert the destination is clean, checked out on
`main`, and resolves the literal expected commit. Assert a malformed SHA,
symlink root, changed origin URL, and non-repository non-empty root fail.

- [ ] **Step 2: Write failing persisted-update tests**

Advance the temporary origin, add an unrelated local branch, rerun
initialization with the new expected commit, and assert local `main`
fast-forwards while the unrelated ref remains. Add literal cases proving dirty
worktrees, non-fast-forward transitions, and rollback commits fail.

- [ ] **Step 3: Write failing secret-isolation tests**

Call `stage_runtime_secrets` with literal source files. Assert the API output is
exactly `database-url`, `git-signing-key`, and `admin-grant-private-key`; worker
output is exactly `database-url` and `worker-api-token`; neither directory
contains the other service's authority.

- [ ] **Step 4: Run tests and verify missing-module failures**

Run:

```bash
uv run --project control --frozen pytest -q control/tests/test_dev_init.py
```

Expected: collection failure because `vonk_control.dev_init` does not exist.

- [ ] **Step 5: Implement repository initialization**

Validate the literal public origin URL and 40-hex commit. Permit a temporary
local origin only when the explicit local-acceptance switch is set; test that
the same URL is rejected without the switch. For a fresh volume,
clone/fetch `main`, check out the expected commit as local `main`, and chown the
tree to 10001:10001 when EUID is zero. For an existing checkout, require a clean
worktree and unchanged origin, fetch `origin/main`, prove the expected commit is
reachable, require `merge-base --is-ancestor <current-main> <expected>`, update
`refs/heads/main` with old-value compare-and-swap, reset the checked-out clean
worktree, and preserve all other refs.

- [ ] **Step 6: Implement disjoint secrets and existing identity initialization**

Move the synthetic generation logic from `scripts/dev-compose-init.py` into the
module. Copy database URL separately to both projections, copy the signing key
only to API, generate the admin key only in API, and generate the worker token
only in worker. Refuse symlink inputs and apply restrictive ownership/modes.

- [ ] **Step 7: Run focused tests**

```bash
uv run --project control --frozen pytest -q control/tests/test_dev_init.py
python3 -m py_compile control/src/vonk_control/dev_init.py
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add control/src/vonk_control/dev_init.py control/tests/test_dev_init.py scripts/dev-compose-init.py
git commit -S -m "feat: initialize image-based development runtime"
```

---

### Task 2: Define and render the Compose-only NAS project

**Files:**
- Create: `deploy/compose/compose.dev.images.yaml`
- Create: `scripts/render-dev-compose`
- Create: `scripts/tests/test_render_dev_compose.py`
- Modify: `deploy/compose/tests/test_dev_compose.py`
- Delete: `deploy/compose/compose.dev.bundle.yaml`

**Interfaces:**
- Produces: `render(template: Path, output: Path, api_image: str, worker_image: str, commit: str) -> None`.
- Produces: one standalone `docker-compose.yml` containing no unresolved variables except optional host port configuration.
- Consumes: image references in `repository:dev-sha-<commit>@sha256:<64-hex>` form.

- [ ] **Step 1: Write failing renderer validation tests**

Assert literal valid image references and a 40-hex commit render exactly once,
while mutable tags, missing digests, production tags, mismatched development
tags, non-GHCR repositories, unresolved template tokens, and output paths equal
to source fail without replacing an existing output.

- [ ] **Step 2: Write failing rendered-Compose contract tests**

Render the template and assert there are no `build` keys or project-root source
binds. Assert API/worker image refs are digest-pinned, `dev-init` and `migrate`
reuse API, `VONK_DEPLOYMENT_BRANCH=main`, and `dev-init` receives the exact
commit. Assert `dev-repository` is API read-write and worker read-only.

Assert these secret mounts exactly:

```text
postgres       -> postgres-password
dev-init       -> database-url, git-signing-key
migrate        -> database-url
control-api    -> dev-api-secrets volume only
control-worker -> dev-worker-secrets volume only
```

- [ ] **Step 3: Run tests and verify failures**

```bash
./.venv/bin/python -m pytest -q scripts/tests/test_render_dev_compose.py deploy/compose/tests/test_dev_compose.py
```

Expected: failures for absent renderer/template and current shared/bind mounts.

- [ ] **Step 4: Implement the image-only template**

Use literal renderer tokens for API image, worker image, and expected commit.
Define `postgres`, `dev-init`, `migrate`, `control-worker`, and `control-api`;
relative `./secrets/*` Compose secrets; `dev-api-secrets`,
`dev-worker-secrets`, `dev-repository`, PostgreSQL, identity, state, route, and
supervisor named volumes; and existing health/dependency ordering.

- [ ] **Step 5: Implement atomic rendering**

Validate all inputs before replacing tokens, reject remaining `__VONK_*__`
markers, render to a sibling temporary file, use `docker compose config -q` with
a temporary synthetic secret directory, then atomically replace the output.

- [ ] **Step 6: Run renderer and Compose tests**

```bash
./.venv/bin/python -m pytest -q scripts/tests/test_render_dev_compose.py deploy/compose/tests/test_dev_compose.py
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add deploy/compose/compose.dev.images.yaml deploy/compose/compose.dev.bundle.yaml deploy/compose/tests/test_dev_compose.py scripts/render-dev-compose scripts/tests/test_render_dev_compose.py
git commit -S -m "feat: render Compose-only development projects"
```

---

### Task 3: Harden and inspect development images

**Files:**
- Modify: `control/Dockerfile`
- Modify: `.dockerignore`
- Create: `scripts/verify-dev-image-secrets`
- Create: `scripts/tests/test_verify_dev_image_secrets.py`

**Interfaces:**
- Produces: `scripts/verify-dev-image-secrets <api-image> <worker-image>`.
- Consumes: locally loaded image references.
- Guarantees: no forbidden source-secret paths, credential filenames, private-key markers, `.env`, `.dev`, or Git metadata in final filesystems or image config.

- [ ] **Step 1: Write failing scanner tests with synthetic images**

Build tiny images containing one forbidden item at a time (`.dev`, `.env`,
`id_ed25519`, `credentials.json`, and a private-key marker) and assert the
scanner rejects each. Build one clean image and assert acceptance. Assert image
config secrets/build args and missing non-root user metadata fail.

- [ ] **Step 2: Run scanner tests and verify the command is absent**

```bash
./.venv/bin/python -m pytest -q scripts/tests/test_verify_dev_image_secrets.py
```

Expected: failure because the scanner does not exist.

- [ ] **Step 3: Implement filesystem and metadata inspection**

Use `docker image inspect`, `docker history --no-trunc`, and a temporary
`docker create`/`docker export` stream. Inspect filenames and regular-file
contents without printing matched secret material. Reject environment names
containing credential/secret/token/password/key authorities except documented
file-path variables.

- [ ] **Step 4: Preserve migration normalization and make required Git tooling explicit**

Keep migration `a+rX` normalization. Ensure both runtime targets contain only
the Git tooling their actual repository reads require and continue running as
UID/GID 10001. Do not copy dev initializer data, secret fixtures, or `.git`.

- [ ] **Step 5: Build and scan real images**

```bash
docker build -f control/Dockerfile --target api -t vonk-forge-api:dev-local .
docker build -f control/Dockerfile --target worker -t vonk-forge-worker:dev-local .
scripts/verify-dev-image-secrets vonk-forge-api:dev-local vonk-forge-worker:dev-local
```

Expected: both images pass.

- [ ] **Step 6: Commit**

```bash
git add .dockerignore control/Dockerfile scripts/verify-dev-image-secrets scripts/tests/test_verify_dev_image_secrets.py
git commit -S -m "test: enforce development image secret boundary"
```

---

### Task 4: Prove the full image-only lifecycle locally

**Files:**
- Create: `scripts/dev-image-acceptance`
- Create: `scripts/tests/test_dev_image_acceptance.py`
- Modify: `scripts/dev-compose`

**Interfaces:**
- Produces: `scripts/dev-image-acceptance --api-image REF --worker-image REF --commit SHA`.
- Produces: automatic diagnostic logs and teardown limited to its unique temporary Compose project.

- [ ] **Step 1: Write failing harness safety tests**

Test argument validation, unique project naming, temporary path validation, trap
installation before Docker mutations, and refusal to use production tags or a
non-main commit fixture. Mock only the Docker process boundary; assert generated
commands include `down --volumes --remove-orphans` for the unique project.

- [ ] **Step 2: Implement the acceptance harness**

Generate temporary synthetic runtime secret files after image build, render a
Compose file, create a temporary bare origin containing the exact local commit,
and set the local-acceptance repository override only in that temporary
Compose file. Assert the ordinary rendered NAS Compose has neither the override
nor its switch. Select a free loopback port, start with fresh volumes, and on
failure print `compose ps -a` and bounded service logs before teardown.

- [ ] **Step 3: Exercise real runtime behavior**

Require `docker compose up --wait`, zero exits for `dev-init` and `migrate`,
running PostgreSQL/worker/API, successful `/api/v1/readyz`, local repository
branch `main` at the exact commit, API inability to see worker secrets, worker
inability to see API secrets, and no project-root `/repository` mount.

Restart API and worker, run `up --wait` again, repeat readiness, and verify
PostgreSQL and repository identities persisted. Always remove the temporary
project and its volumes.

- [ ] **Step 4: Run the complete local stack**

```bash
scripts/dev-image-acceptance \
  --api-image vonk-forge-api:dev-local \
  --worker-image vonk-forge-worker:dev-local \
  --commit "$(git rev-parse main)"
```

Expected: one success result after build-independent image execution, migration,
health, isolation, restart, and teardown.

- [ ] **Step 5: Make the local wrapper use image-only Compose**

Change `scripts/dev-compose` to render and run the image-only graph from explicit
local image refs. Remove source bundle publication and Windows execution-policy
workarounds from the local runtime path.

- [ ] **Step 6: Commit**

```bash
git add scripts/dev-image-acceptance scripts/tests/test_dev_image_acceptance.py scripts/dev-compose
git commit -S -m "test: exercise image-only development stack"
```

---

### Task 5: Publish exact tested images and Compose from main

**Files:**
- Create: `.github/workflows/dev-images.yml`
- Create: `scripts/dev-image-metadata`
- Create: `scripts/promote-image-aliases`
- Create: `scripts/tests/test_dev_image_workflow.py`

**Interfaces:**
- Produces: immutable `dev-sha-<40-hex>` API/worker tags, moving `dev` aliases,
  and digest outputs.
- Produces: artifact `vonk-forge-dev-compose-<sha>` containing
  `docker-compose.dev.yml` and `docker-compose.pinned.yml`.
- Produces on signed releases: `docker-compose.production.yml` from the full
  production graph.
- Consumes: only the exact current `origin/main` tip.

- [ ] **Step 1: Write failing metadata and workflow contract tests**

Test exact-main ref validation, immutable development tag generation, the exact
`dev` alias, repository names, digest format, and rejection of
branches/tags/abbreviated SHAs. Parse the workflow and assert no `environment`,
build args, build secrets, `latest`, or pre-acceptance registry login/push
exists. Require the `dev` alias update to occur only after immutable publication,
digest verification, provenance, and Compose rendering succeed. Exercise
initial publication, partial-state roll-forward, established-pair rollback,
rollback failure, idempotent reruns, and failed-job repair using fake registry
state.

- [ ] **Step 2: Implement deterministic metadata**

`scripts/dev-image-metadata` accepts event ref, selected SHA, and fetched
origin-main SHA; all must identify `refs/heads/main` at one full commit. It emits
API/worker repository names, immutable `dev-sha-<sha>` tags, and the exact `dev`
convenience alias. The renderer consumes only digest-locked refs; its pinned
mode additionally requires the immutable development tag.

- [ ] **Step 3: Build exact OCI archives before registry login**

Use Buildx linux/amd64 OCI outputs with SBOM and provenance attestations. Install
Skopeo, copy each OCI archive to the local Docker daemon, scan the loaded images,
and run `scripts/dev-image-acceptance` against them. Do not expose
`GITHUB_TOKEN` to build/test commands.

- [ ] **Step 4: Push the same tested manifests**

Only after acceptance, log into GHCR and use Skopeo to copy the tested OCI
archives to their development tags. Resolve immutable registry digests, render
the two development Compose files with tag-plus-digest references, validate
them, sign the published subjects with GitHub artifact attestations, and upload
only those Compose files. As the final registry
mutation, advance each `dev` alias to its already-verified immutable manifest
without rebuilding it. Serialize and retry the cross-repository reconciliation,
verify both resulting digests, roll back an established pair on failure, and
retain the accepted OCI artifact so a failed-job rerun repairs first-publication
or cancellation drift. Never publish or change `latest` in this workflow.

- [ ] **Step 5: Run workflow contract tests and local workflow commands**

```bash
./.venv/bin/python -m pytest -q scripts/tests/test_dev_image_workflow.py
scripts/dev-image-metadata refs/heads/main "$(git rev-parse main)" "$(git rev-parse main)"
```

Expected: all tests and metadata validation pass.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/dev-images.yml scripts/dev-image-metadata scripts/promote-image-aliases scripts/tests/test_dev_image_workflow.py
git commit -S -m "ci: publish accepted main development images"
```

---

### Task 6: Promote development digests for production releases

**Files:**
- Modify: `.github/workflows/ci.yml`
- Create: `.github/release-allowed-signers`
- Modify: `scripts/container-release-metadata`
- Modify: `tests/scripts/test_container_release_metadata.py`
- Modify: `tests/test_container_release_workflow.py`

**Interfaces:**
- Consumes: signed `vX.Y.Z` tag commit and existing `dev-sha-<commit>` manifests.
- Produces: stable API/worker tags and moving `latest` aliases pointing to
  exactly those manifests, plus the release's Hermes `latest` alias.
- Preserves: existing Hermes build and platform release evidence pipeline while
  moving Hermes `latest` only in the final reconciled alias set.

- [ ] **Step 1: Write failing release-promotion tests**

Assert release metadata includes development source references for the exact tag
commit. Assert the workflow no longer builds API/worker, refuses absent or
mismatched development manifests, never emits or changes `dev`, promotes by
digest, and feeds promoted digest outputs into existing
SBOM/provenance/platform evidence. Require `latest` to advance only after every
signed-release gate, stable-tag digest check, and release-evidence step passes.
The tag verifier must use a trusted signer allowlist read from fetched `main`,
require an annotated SSH-signed tag, and prove the tagged commit is reachable
from `origin/main` before any release build or publication job can run. Require
the final globally serialized reconciler to select the highest completed stable
release and use only checksum-verified `vonk-forge-images.env` release evidence
as digest authority.

- [ ] **Step 2: Run release tests and verify current rebuild behavior fails**

```bash
./.venv/bin/python -m pytest -q \
  tests/scripts/test_container_release_metadata.py \
  tests/test_container_release_workflow.py
```

Expected: failure because CI currently rebuilds API/worker and publishes
`latest`.

- [ ] **Step 3: Replace API/worker builds with verified digest promotion**

Inspect each `dev-sha-<tag-commit>` manifest, verify its source revision and
attestation identity, then use `docker buildx imagetools create --tag
<repository>:<version> <repository>@<digest>`. Reinspect the stable tag and
require digest equality. After all release evidence succeeds, update
`<repository>:latest` from that same immutable digest and verify equality again.
Promote API, worker, and Hermes convenience aliases as one reconciled set. Keep
the existing output names consumed by platform evidence. Never mutate
`<repository>:dev`.

- [ ] **Step 4: Run release and supply-chain tests**

```bash
./.venv/bin/python -m pytest -q \
  tests/scripts/test_container_release_metadata.py \
  tests/test_container_release_workflow.py
scripts/verify-supply-chain --json
```

Expected: tests pass and release evidence still consumes exact immutable
digests.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml .github/release-allowed-signers scripts/container-release-metadata tests/scripts/test_container_release_metadata.py tests/test_container_release_workflow.py
git commit -S -m "ci: promote accepted images for production"
```

---

### Task 7: Remove obsolete SMB build publication and document handoff

**Files:**
- Delete: `scripts/dev-compose-sync`
- Delete: `scripts/dev-compose-sync.ps1`
- Delete: `scripts/dev_compose_sync.py`
- Delete: `scripts/dev-compose-secrets`
- Delete: `scripts/tests/test_dev_compose_sync.py`
- Delete: `docs/superpowers/plans/2026-08-08-smb-dev-compose-sync.md`
- Delete: `docs/superpowers/specs/2026-08-08-smb-dev-compose-sync-design.md`
- Modify: `.gitignore`
- Modify: `README.md`
- Modify: `deploy/compose/README.md`
- Create: `docs/runbooks/development-nas-installation.md`

**Interfaces:**
- Produces: documented operator layout containing only Compose and three secret files.
- Removes: source/Dockerfile synchronization and Windows-side project generation.

- [ ] **Step 1: Remove obsolete source-build publisher artifacts**

Delete only the files listed above and remove documentation that tells operators
to build from SMB. Preserve unrelated user files and production deployment docs.

- [ ] **Step 2: Document public image and secret flow**

Document main development publication (`dev-sha-*` plus `dev`), signed-tag
production promotion (`vX.Y.Z` plus `latest`), Action artifact download, generic
NAS Compose-project redeployment, relative NAS secret paths, repository
fast-forward behavior, intentional repository-volume rollback, and the fact
that no secret enters CI or an image.

Add a dedicated installation runbook with the exact three-file directory
layout, NAS-shell and PowerShell-safe generation/copy instructions, restrictive
permissions, database URL construction, private SSH signing-key handling,
Compose import and first-start checks, non-secret troubleshooting, rotation,
backup/restore boundaries, and an explicit list of generated credentials held
only in named volumes. Commands must avoid printing secret values or placing
them in command history.

- [ ] **Step 3: Run all focused verification**

```bash
uv run --project control --frozen pytest -q \
  control/tests/test_dev_init.py \
  control/tests/test_container_release.py
./.venv/bin/python -m pytest -q \
  scripts/tests/test_render_dev_compose.py \
  scripts/tests/test_verify_dev_image_secrets.py \
  scripts/tests/test_dev_image_acceptance.py \
  scripts/tests/test_dev_image_workflow.py \
  deploy/compose/tests/test_dev_compose.py
python3 -m py_compile control/src/vonk_control/dev_init.py scripts/render-dev-compose scripts/dev-image-metadata
bash -n scripts/dev-compose scripts/dev-image-acceptance scripts/verify-dev-image-secrets
git diff --check
```

- [ ] **Step 4: Run the exact real-image publication gate locally**

Build API/worker OCI archives through the workflow-equivalent commands, load
them, scan them, and run `scripts/dev-image-acceptance`. Expected: all services
healthy, branch/update and secret-isolation assertions pass, restart passes, and
teardown removes its temporary volumes.

- [ ] **Step 5: Commit**

```bash
git add -A -- .gitignore README.md deploy/compose/README.md scripts docs/superpowers/plans/2026-08-08-smb-dev-compose-sync.md docs/superpowers/specs/2026-08-08-smb-dev-compose-sync-design.md
git commit -S -m "docs: replace SMB builds with public dev images"
```

---

### Task 8: Publish the generated Compose artifact to the NAS share

**Files:**
- No source changes.

**Interfaces:**
- Consumes: a passing `main` workflow artifact or locally rendered digest-pinned equivalent.
- Produces: `Z:\vonk-forge\docker-compose.yml` only.

- [ ] **Step 1: Confirm the image publication workflow passed**

Verify the workflow commit equals current `origin/main`, both public GHCR
digests resolve, and the downloaded Compose references those exact digests.

- [ ] **Step 2: Validate the NAS secret prerequisites without reading values**

Verify these regular files exist and are non-empty:

```text
Z:\vonk-forge\secrets\postgres-password
Z:\vonk-forge\secrets\database-url
Z:\vonk-forge\secrets\git-signing-key
```

- [ ] **Step 3: Replace only the Compose file**

Copy the validated artifact to `Z:\vonk-forge\docker-compose.yml`. Do not delete,
move, overwrite, hash-print, or otherwise expose files under `secrets/`.

- [ ] **Step 4: Report the UGREEN action**

Tell the operator to redeploy/pull the project. Explicitly state that no image
build and no PostgreSQL/repository volume deletion are needed for a forward
update.
