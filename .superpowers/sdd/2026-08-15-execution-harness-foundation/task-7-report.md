# Task 7 report: fresh pre-production v1 schema

## Outcome

Implemented the fresh pre-production catalog cutover at Alembic head
`0027_execution_harness_catalog`, based directly on
`0026_telemetry_maintenance_state`.  The revision builds the v1 catalog on an
empty database and renames the empty-schema mapping column to `topology_name`.
It deliberately does not convert, preserve, alias, or otherwise support
prototype rows.

## Changed files

- Added `control/migrations/versions/0027_execution_harness_catalog.py`.
- Added `control/tests/test_execution_harness_catalog_migration.py`.
- Removed `control/tests/test_recipe_catalog_migration.py`.
- Replaced prototype package-family seeding with resolved built-in execution
  harness catalog seeding in `control/src/vonk_control/catalog_seeds.py` and
  wired it in `control/src/vonk_control/api.py`.
- Updated catalog-seed, migration-chain, historic migration-boundary,
  development-fixture, authority-fixture, and OpenAPI contract tests under
  `control/tests/` and `tests/control/test_openapi_clients.py`.
- Removed prototype global recipe/test-report inputs from
  `schemas/global/contract.lock.json` and from `scripts/update-global-contracts`.

## RED evidence

The fresh-schema test was written before the migration existed:

```text
$ uv run --project control --frozen python -m pytest control/tests/test_execution_harness_catalog_migration.py -q
FAILED test_fresh_database_reaches_the_v1_catalog_head
assert table_exists(connection, "catalog_entities")
1 failed in 0.85s
```

The catalog seed test was also red before the new v1 seeder was implemented:

```text
ImportError: cannot import name seed_builtin_harnesses
```

During implementation, SQLite metadata parity initially exposed batch-recreate
column ordering; the test was kept and the explicit batch ordering was fixed.
Disposable PostgreSQL then exposed password rendering and a foreign-key-safe
rename requirement; the final migration uses a direct PostgreSQL rename and a
SQLite batch rebuild.

## GREEN evidence

```text
$ uv run --project control --frozen python -m pytest control/tests/test_execution_harness_catalog_migration.py -q
3 passed in 3.37s
```

This covers a fresh SQLite chain to head, reflection parity for
`CatalogEntity`, `CatalogEntityRevision`, and `ClusterMapping`, and a
disposable PostgreSQL database run.  PostgreSQL reached head, contains the two
catalog tables and `cluster_mappings.topology_name`, has no `profile_name`, and
reported `compare_metadata(...) == []` against `Base.metadata`.

```text
$ uv run --project control --frozen python -m pytest \
  control/tests/test_execution_harness_catalog_migration.py \
  control/tests/test_admission_migration.py \
  control/tests/test_wheel_runtime_assets.py \
  control/tests/test_catalog_seeds.py \
  control/tests/test_agent_migrations.py \
  control/tests/test_workload_package_migration.py \
  control/tests/test_development_recipe_fixture.py \
  control/tests/test_recipe_deployment_authority.py \
  control/tests/test_enrollment.py::test_postgres_issued_revocation_evidence_migration_chain -q
41 passed, 1 skipped in 15.81s
```

```text
$ scripts/update-global-contracts
$ uvx ruff@0.16.1 check <all changed Python/script targets>
All checks passed!
$ git diff --check
```

The contract refresher completed with the checked-in source commit and now
locks only the container-runtime-policy and problem schemas.

## Full control suite

The first final-suite attempt used:

```text
$ uv run --project control --frozen python -m pytest control/tests -q \
  --junitxml=/tmp/task7-control-suite-final.xml
```

JUnit recorded `2398` tests, `1` skipped, `1` failure, and `0` errors in
`279.676s`.  The sole failure was outside Task 7:

```text
tests.test_library_projection::test_grouped_reservations_do_not_hide_later_candidate_port_conflict
AttributeError: 'code' object has no attribute '_remove'
```

An isolated rerun of that test crashed the interpreter natively in SQLAlchemy
`Session.add_all` with `Fatal Python error: Segmentation fault`.  A clean
repeat of the full-suite command also terminated before creating its requested
JUnit file, consistent with that unrelated native crash.  No deterministic
Task 7 migration, fixture, catalog, lock, or metadata-parity failure remains;
the scoped suite above is green.

## Prototype scan

```text
$ rg -n 'deployment_profiles|profile_name|mia_dsv4_flash|ds4_smoke' \
  control schemas config scripts deploy tests
```

The scan has no active prototype semantics.  Its five `profile_name` line
matches occur in three intentional historical-column contexts: the original
0015 historical migration, the two 0027 discarded-column operations, and the
two new test assertions that the discarded column is absent.  There are no
`deployment_profiles`, `mia_dsv4_flash`, or `ds4_smoke` matches.

## Downgrade and support boundary

`0027_execution_harness_catalog.downgrade()` intentionally raises a clear
`RuntimeError`: this is a fresh pre-production cutover, not an in-place
upgrade path.  Historic reversible-migration tests are bounded to their
applicable revisions at or before `0026`; fresh-database coverage is the only
path that crosses 0027.  Existing pre-production users, agents, catalog
content, recipes, installations, runs, routes, and acceptance data are not
supported and must be discarded/recreated as required.

## Self-review and concerns

Reviewed the migration's exact revision/down-revision, SQLite ordering,
PostgreSQL rename, catalog constraints/indexes, model metadata parity, the
linear-chain assertion, seed idempotence, lock generator defaults, and the
prototype scan.  No Task 8 recipes or Task 9 reset/physical-acceptance scripts
were added.

Concern: the complete control suite is blocked by the pre-existing/unrelated
native crash in `test_library_projection`; this task leaves Fleet/library/UI
semantics unchanged and does not modify that subsystem.
