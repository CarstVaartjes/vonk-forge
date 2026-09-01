# Agent guide

This repository is the Vonk Forge platform and control plane. The reviewed
recipe definitions live in the sibling `/opt/vonk-forge-recipes` checkout.
Keep repository, CI/publication, Controller deployment, and physical Spark
qualification as separate evidence boundaries.

## Local Linux and container testing

On macOS, use OrbStack for container-backed tests before treating a Linux-only
path as unavailable:

```bash
docker context show
docker info
```

The active context and `docker info` output must identify the intended OrbStack
engine. Switch explicitly with `docker context use orbstack` when needed. Run
Compose, Linux/systemd harnesses, and disposable NAS acceptance in OrbStack or
in the designated CI lane; do not declare them impossible merely because the
host is macOS. OrbStack can catch container, Compose, installer, systemd, and
readiness regressions. Real NVIDIA hardware, NCCL/fabric behavior, model
quality, and physical Spark acceptance still require the designated Linux/ARM64
or Spark lane.

Use a writable, task-specific uv cache. From `/opt/vonk-forge`:

```bash
UV_CACHE_DIR=/private/tmp/vonk-forge-uv-cache \
  uv run --frozen pytest -q
UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache \
  uv run --project control --frozen --with-editable . pytest -q control/tests
UV_CACHE_DIR=/private/tmp/vonk-forge-compose-cache \
  uv run --frozen pytest -q deploy/compose/tests
UV_CACHE_DIR=/private/tmp/vonk-forge-acceptance-cache \
  uv run --frozen pytest -q \
    tests/test_fresh_nas_acceptance.py \
    tests/test_spark_lifecycle_runner.py \
    tests/test_installer_publication_workflow.py
```

When acceptance inputs are available, run the actual harness through the same
OrbStack Docker context, not only its unit tests:

```bash
UV_CACHE_DIR=/private/tmp/vonk-forge-acceptance-cache \
  uv run python tests/acceptance/test_fresh_nas_install.py
UV_CACHE_DIR=/private/tmp/vonk-forge-acceptance-cache \
  uv run python tests/acceptance/test_spark_lifecycle.py run
```

Those commands require the candidate, compose, Controller, and acceptance
environment described by the acceptance workflow. Never substitute synthetic
success for missing environment inputs.

## Schema and compatibility policy

This is a greenfield deployment. Active installer, release-publication,
catalog, recovery, and authority paths use schema 2. Do not add schema-1
fallbacks, dual readers/writers, migration shims, or stale schema-1 fixtures to
those paths. Keep schema 1 only where the code explicitly defines it as the
current wire/build/job/evidence contract or as an inert historical migration;
the `/api/v1` URL prefix is API versioning, not permission to restore an old
document schema. “Dual-Spark” means two-node topology and remains supported; it
does not mean maintaining schema-1/schema-2 runtime paths.

## Recipe-library checks

Build and validate the sibling recipe checkout against the exact platform
checkout before calling a recipe installable:

```bash
cd /opt/vonk-forge-recipes
tools/build-catalog-index
tools/build-catalog-index --check
/opt/vonk-forge/control/.venv/bin/python \
  /opt/vonk-forge/scripts/validate-recipe-library \
  --library-root /opt/vonk-forge-recipes \
  --platform-root /opt/vonk-forge \
  --json
```

Structural qualification proves an executable contract, not physical model
acceptance. For a single recipe, use `scripts/qualify-recipe` with
`--level structural`; reserve native/container/Spark acceptance for the matching Linux,
OrbStack, CI, or physical hardware lane. Record the exact platform commit,
recipe-library commit, recipe digest, and resulting evidence.

## Deployment and fleet safety

Routine Spark package upgrades are Controller-authorized and signed; do not use
SSH as the rollout path. Preview and apply the returned digest one Spark at a
time:

```bash
vonkctl fleet upgrade candidate --json
vonkctl fleet upgrade preview --strategy one-at-a-time --json
vonkctl fleet upgrade apply --strategy one-at-a-time \
  --plan-digest PLAN_DIGEST --apply --json
```

For a mounted controller project, consume the signed NAS installer from the
directory containing the existing bundle, preserve `.env`, `secrets/`, and
named volumes, then redeploy through Docker Compose/UI. Inspect the published
manifest first: it must be schema 2 and bind the intended current source and
artifacts. Do not delete PostgreSQL volumes or run `docker compose down -v` for
a normal upgrade. SSH is diagnostic/bootstrap-only, never a hidden fallback
for an authorized Controller operation.

## Parallel work

Use an isolated branch/worktree for each independent agent, based on the latest
`origin/main`. Do not edit another agent’s worktree or the shared checkout’s
uncommitted files. Before committing, inspect `git status`, run relevant tests,
and run `git diff --check`. Keep commits scoped and coordinate overlapping
files before merging.
