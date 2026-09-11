# Agent guide

This repository is the Vonk Forge platform and control plane. The reviewed
recipe definitions live in the sibling `/opt/vonk-forge-recipes` checkout.
Keep repository, CI/publication, Controller deployment, and physical Spark
qualification as separate evidence boundaries.

## Engineering stance

Vonk Forge optimizes for simplicity, stability, and security while keeping every
frontier recipe runnable on local Spark capacity. Read
[docs/engineering-principles.md](docs/engineering-principles.md) before making a
structural choice. The short form:

- **Simplicity:** one Compose application, one PostgreSQL authority, one current
  execution path per operation. Kubernetes, a service mesh, an event bus, custom
  microservices, and a mandatory Vault are explicit non-goals. GPU nodes run
  workloads, never ingress, databases, monitoring, or the admin UI.
- **Stability:** state before controls, live-versus-desired before mutation, and
  one Spark at a time for consequential fleet-wide change. Remove retired paths
  together with their callers instead of carrying compatibility shims.
- **Security:** fail closed. Never soften a `PermissionDenied`, never clamp an
  invalid request into a valid one, keep the update-signing key separate from
  administrative authorization, and never let candidate tooling install itself.
  SSH is diagnostic and bootstrap-only.
- **Every frontier recipe:** the curated library exists to make any model
  reproducible, not to restrict what can run. Missing assets are actionable
  cache blockers and never a problem deferred to a Spark.

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
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes

# Fast tier: hermetic and parallel. No Docker, PostgreSQL, cargo or host tool.
# Approximately 80s for control/tests and 36s for tests on 8 cores.
UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache \
  uv run --project control --frozen --with-editable . pytest -q \
    control/tests -m "not lane" -n auto --dist loadfile
UV_CACHE_DIR=/private/tmp/vonk-forge-acceptance-cache \
  uv run --python 3.12 --frozen --with pytest==9.1.1 \
    --with pytest-xdist==3.8.0 \
    --with-editable "$VONK_RECIPE_LIBRARY_ROOT/contracts" \
    pytest -q tests -m "not lane" -n auto

# Lane tier: the same trees without the marker filter. Run it in OrbStack or
# the designated CI lane; it needs Docker, PostgreSQL, cargo, dpkg and a
# Linux/ARM64 host.
UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache \
  uv run --project control --frozen --with-editable . pytest -q control/tests
UV_CACHE_DIR=/private/tmp/vonk-forge-acceptance-cache \
  uv run --python 3.12 --frozen --with pytest==9.1.1 \
    --with-editable "$VONK_RECIPE_LIBRARY_ROOT/contracts" pytest -q tests

# Compose lane.
UV_CACHE_DIR=/private/tmp/vonk-forge-compose-cache \
  uv run --frozen pytest -q deploy/compose/tests
```

The `lane` marker is applied automatically at collection time from what a test
actually needs: a PostgreSQL fixture, a `*_wire_bridge.py` Rust probe module, a
Docker build, or a Linux host tool such as `dpkg`. New host-dependent tests are
classified without further edits, so `-m "not lane"` stays honest. A fast-tier
failure is a real defect; a lane-tier failure on macOS is usually a missing
Linux dependency, not a regression.

Run the two trees in separate pytest invocations. Both contain modules with the
same basename, so a single invocation over `tests control/tests` mis-collects
them.

Run the root `tests/` suite in the standalone environment CI uses: `pytest`
plus an editable install of the recipe contracts package. The root project is
the `vonk-cluster-profiles` package and its lint tooling; it deliberately has
no dependency on `pydantic`, `vonk_control`, or the contracts package, so a
bare `uv run pytest` from the root environment cannot import what the
acceptance and contract tests need. The control environment is a superset and
can also run that tree for a quick check, but CI parity is the standalone form.

`VONK_RECIPE_LIBRARY_ROOT` is a path to the sibling recipe-library checkout, not
a secret. Catalog, canonical-consumer, and acceptance-recipe tests read the real
library through it and fail at collection when it is unset, so export it before
running the root or control suites.

The native Rust agent and its wire contract build only for Linux. The
`control/tests/*_wire_bridge.py` suites consume probes produced by
`scripts/tests/run_agent_wire_contracts.py`; on macOS `cargo build` fails on
platform-gated code such as `rustix::fs::openat2`, so run those suites in the
Linux/OrbStack or designated CI lane instead of reading the failure as a
regression.

### Lint, format and type checks

Run all three after a change. Each is pinned and deterministic.

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes

# Python lint; ruff is the repository's formatting authority too.
UV_CACHE_DIR=/private/tmp/vonk-forge-uv-cache uv run --frozen ruff check .

# Python types. Pyright reads [tool.pyright] and resolves imports from the
# control virtualenv, so sync that project once first.
UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache \
  uv sync --project control --frozen
UV_CACHE_DIR=/private/tmp/vonk-forge-uv-cache scripts/check-python-types

# TypeScript types; the build runs tsc --noEmit before bundling.
npm ci --prefix control/web
npm run build --prefix control/web
```

`control/.venv` cannot satisfy the ruff pin because `openapi-python-client`
requires `ruff<0.14`; always lint through the root project. CI runs the same
version via `uvx --from ruff==0.16.1 ruff check .`.

The repository does not type-check cleanly yet. `scripts/check-python-types`
enforces a per-file ratchet against `tools/pyright-baseline.json`: a file may
never exceed its recorded error count, so the known-error set only shrinks as
entries are lowered. Improvements are reported but do not fail the check,
because platform-conditional code can legitimately differ between a developer
host and CI; run `--update` to record a reduction. `pyright` runs over
`control/src`, `src`, `tests` and `control/tests` in basic mode; generated
clients and virtualenvs are excluded.

There is no separate ESLint or Prettier configuration. TypeScript formatting
follows the surrounding files, and `npm run build` is the type gate.

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

## Current contracts only: no legacy compatibility

This is a first internal release, not a migration. Keep one current definition
and one current execution path for each document and operation. Remove retired
parsers, DTOs, database models/tables, endpoints, CLI compatibility aliases,
fixtures, packaged assets, and callers together. Do not retain deprecated
constructors, old field names, dual readers/writers, or fallback shapes merely
to keep old tests or old worktrees working. In particular, `fleet node-profile`
is the current command; remove the obsolete `fleet profile` alias.

Published Model and Recipe structures are defined by the canonical Pydantic
package in vonk-forge-recipes. Controller APIs and Controller/Spark messages
use their authoritative nested Pydantic models. Validate persisted contract
JSON on reads and writes; malformed data must not become empty/default state.
Generate OpenAPI and Python/TypeScript clients from the current API. Generate
Rust wire structures with typify from the Pydantic-derived JSON Schemas; do not
maintain parallel handwritten payload fields. Keep handwritten semantic and
security validation, and pass connected producer/consumer tests that preserve
required-field presence, nullability and strict structure.

Every application API route must appear in the full OpenAPI schema. Do not use
`include_in_schema=False` in production routes. Declare byte uploads, downloads,
and streams explicitly too. Derive narrower client schemas from the complete
schema; never hide the underlying route to limit a client audience. Keep the
discovered-route completeness gate in CI, including its deliberate hidden-route
rejection test.

Consume wire and persisted JSON with the canonical model's JSON validation
semantics, including JSON already decoded by the database driver. Do not treat
JSON arrays as malformed Python tuples or disable strict validation to conceal
that mode mismatch. Test the actual serialize/store/load/consume path.

Optional fields with a declared `None` default accept omission and explicit
`null` as the same value. Omit those unused fields when sending documents.
Normalize through the authoritative model before hashing or signing, and use
the identical canonical representation when verifying in Python and Rust.
Do not make an optional field required merely to repair a serialization
mismatch. Required nullable fields remain required and retain their `null`.
Preserve meaningful `false`, `0`, empty values, and engine-owned JSON values;
never recursively strip every `null` from arbitrary dictionaries. Apply only
declared defaults, never defaults that hide malformed required data. Preserve
formatted string values as strings; a date-time format is not permission to
rewrite a timestamp's spelling. Test actual producer-to-consumer round trips,
including signatures/digests and both missing/null forms, not just acceptance
by two parsers. See `docs/api-contracts.md` for the serialization policy.

Allow engine-owned content through its declared extension fields; do not
introduce an exhaustive engine-argument allowlist.

Test fixtures and health probes are consumers too: use the same typed contract
as the producer, rather than raw dictionary equality or duplicated key lists.
For external protocols, model their documented required/optional fields and
extension behavior; keep deterministic test-content assertions separate from
structural validation. An omitted optional default is not a security failure.
Exercise normal streaming and non-streaming paths where the protocol supports
both.

Update producers, consumers, documentation and meaningful tests together.
A current document's version number is not a reason to introduce another
version reader. Historical audit documents may remain clearly marked as
history; they must not be imported, packaged as active configuration, or used
as instructions to restore old behavior.


This is a greenfield deployment. Active installer, release-publication,
catalog, recovery, and authority paths use schema 2. Do not add schema-1
fallbacks, dual readers/writers, migration shims, or stale schema-1 fixtures to
those paths. Keep schema 1 only where the code explicitly defines it as the
current wire/build/job/evidence contract or as an inert historical migration;
the `/api` URL prefix is API versioning, not permission to restore an old
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
SSH as the rollout path. `vonkctl` exposes one authorized upgrade command; there
is no separate candidate/preview/apply or plan-digest subcommand:

```bash
vonkctl fleet upgrade Atlas --strategy one-at-a-time --json
vonkctl fleet upgrade --all --strategy one-at-a-time --json
```

For a mounted controller project, consume the signed NAS installer from the
directory containing the existing bundle, preserve `.env`, `secrets/`, and
named volumes, then redeploy through Docker Compose/UI. Inspect the published
manifest first: it must be schema 2 and bind the intended current source and
artifacts. Do not delete PostgreSQL volumes or run `docker compose down -v` for
a normal upgrade. SSH is diagnostic/bootstrap-only, never a hidden fallback
for an authorized Controller operation.

## Profile cache contract

The local NAS/Controller cache is the authoritative availability surface for
Fleet profile authoring and apply admission. Profile choices must resolve from
that cache to an exact model and recipe-image identity; a public catalog entry
or a Spark-local copy is not sufficient. Persisted profiles still belong to
the current PostgreSQL authority and schema-2 contracts, but their choices and
readiness are cache-backed.

Missing model or recipe-image assets are actionable blockers. Preview and
authoring must name the missing asset and expose the current prepare-cache
action; do not silently admit a profile and defer the problem to a Spark.
The local NAS cache is trusted under its managed-storage contract, so do not
promise costly repeated full hashing as part of ordinary profile reads or
admission.

Applying a ready profile binds the exact profile and plan digests, fans out
exact model and recipe-image preparation to the target Sparks in parallel, and
skips assets already local on each target. It must stop and replace workloads
safely, retain durable per-target progress, and report that progress before a
route is published. NAS reconciliation removes unused local model-cache
entries while preserving profile and active-workload references. Spark-local
copies are execution caches only: they may be reused or rehydrated, but they
never become profile authority.

Keep these readiness and admission facts separate from repository, CI and
publication evidence, Controller deployment evidence, and physical Spark or
model-quality acceptance. A passing profile preview or cache operation is not
physical Spark qualification, and SSH must not become an undocumented
alternative rollout path.

## Parallel work

Use an isolated branch/worktree for each independent agent, based on the latest
`origin/main`. Do not edit another agent’s worktree or the shared checkout’s
uncommitted files. Before committing, inspect `git status`, run relevant tests,
and run `git diff --check`. Keep commits scoped and coordinate overlapping
files before merging.
