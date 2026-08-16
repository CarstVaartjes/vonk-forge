# Task 9 report: Fresh reset, physical acceptance, and operator documentation

Date: 2026-08-16

Status: DONE_WITH_CONCERNS

Implementation commit: `aa641a3868d667e5e077271e81fd38e0e614e717`

## What was implemented

- Added `scripts/reset-development-recipe-domain`, a development-only reset that requires the exact destructive confirmation, validates the complete Compose service and named-volume boundary, drains workloads through digest-bound public APIs, removes only the validated project volumes, recreates PostgreSQL, verifies Alembic head `0027_execution_harness_catalog`, starts the stack, and verifies an empty Fleet plus the exact eight resolved built-in harnesses.
- Added explicit `--docker-mode sudo|direct` authority. NAS use defaults to non-interactive `sudo -n docker`; the reset itself remains unprivileged so its administrator token stays operator-owned and mode `0600`.
- Added `scripts/accept-recipe`, which rejects wrong node counts before credential or network access, resolves exact v1 recipe/catalog identity, verifies Fleet and agent bindings, performs read-only Spark host/resource/fabric/image/artifact preflight, delegates lifecycle execution to `scripts/run-development-slices`, and advances canonical private evidence only through proven phase prefixes.
- Updated `scripts/run-development-slices` to consume the current `FleetSnapshot` schema and derive canonical fleet evidence rather than expecting prototype flat node fields.
- Added operator guides for the harness/runtime-distribution/patch contract, catalog identity layers, one versus many Sparks, replicas, custom recipes, license responsibility, reset, install/invoke/stop/update/rollback, and physical acceptance evidence.
- Updated the NAS and fresh-install runbooks and architecture HTML. The permanent NAS project remains exactly `docker-compose.yaml` plus `secrets/`; the reset script and short-lived token are staged outside the project and removed afterward.

## TDD evidence

The new reset and acceptance tests were authored before their scripts. They initially failed because both commands were absent. During controller inspection, a further RED run proved the NAS sudo authority was missing:

```text
uv run --frozen python -m pytest scripts/tests/test_reset_development_recipe_domain.py -q
8 failed, 1 passed in 4.56s
```

All eight failures were the expected unrecognized `--docker-mode` contract before implementation. After adding `sudo -n docker` and direct modes:

```text
uv run --frozen python -m pytest scripts/tests/test_reset_development_recipe_domain.py -q
9 passed in 4.84s
```

## Local GREEN evidence

```text
git diff --check && \
uv run --frozen python -m pytest \
  scripts/tests/test_reset_development_recipe_domain.py \
  scripts/tests/test_accept_recipe.py \
  tests/test_docs_contract.py \
  scripts/tests/test_run_development_slices.py -q
120 passed in 49.29s
```

```text
uvx --from ruff==0.16.1 ruff check \
  scripts/accept-recipe \
  scripts/reset-development-recipe-domain \
  scripts/run-development-slices \
  scripts/tests/test_accept_recipe.py \
  scripts/tests/test_reset_development_recipe_domain.py \
  scripts/tests/test_run_development_slices.py \
  tests/test_docs_contract.py
All checks passed!
```

Both command help paths also execute successfully. No SSH, Compose reset, NAS mutation, Spark mutation, deployment, or physical acceptance was performed by the implementer.

## Remaining concerns and external gates

- Task 9 steps 6–8 remain intentionally incomplete: actual Spark preflight, destructive development reset, fresh administrator/browser session, fresh Spark re-enrollment, and physical one-Spark DS4 plus two-Spark Mia acceptance.
- The local patch must pass independent review before any destructive command is used.
- The new control/API and worker `:dev` images and agent package must be published from the accepted branch, the NAS Compose artifact copied and redeployed, and both Sparks updated before reset/acceptance.
- Physical acceptance must preserve canonical evidence through all explicit restart and distributed rank-loss/recovery checkpoints. No `spark-accepted` claim exists yet.

## Files changed

- `scripts/reset-development-recipe-domain`
- `scripts/accept-recipe`
- `scripts/run-development-slices`
- `scripts/tests/test_reset_development_recipe_domain.py`
- `scripts/tests/test_accept_recipe.py`
- `scripts/tests/test_run_development_slices.py`
- `tests/test_docs_contract.py`
- `docs/operators/execution-harnesses.md`
- `docs/operators/model-catalog.md`
- `docs/runbooks/development-nas-installation.md`
- `docs/runbooks/fresh-development-install.md`
- `docs/vonk-forge-architecture.html`
- `docs/README.md`
