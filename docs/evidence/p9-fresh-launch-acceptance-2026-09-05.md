# P9 fresh-launch acceptance evidence

This evidence belongs to the isolated `codex/p9-launch-acceptance` checkout.
The integrated base is `codex/interface-integration` at
`8930f9fbef2bd3bb2889450bf0dfb46c0edbcd4e`. The P9 commits only add this
evidence and `control/tests/test_p9_fresh_launch_acceptance.py`.

The acceptance consumes the supplied verified publication receipt and cache:

- `/private/tmp/vonk-production-reader-corpus-evidence.json`
- `/private/tmp/vonk-canonical-import-corpus-evidence.json`
- the production reader `cache_root` named by the first receipt

The receipt currently describes a schema-2 index for
`CarstVaartjes/vonk-forge-recipes` with 92 Models and 84 Recipes. Package
archives currently carry 81 distinct Model snapshots, leaving 11 valid Models
without a matching Recipe. Those values are evidence of this receipt, not
test constants: the test validates the supplied publication SHA-1, derives all
catalog/package/selector sets from the index, and compares every database/API
count to those derived sets. A newer verified receipt and snapshot can be
supplied through `VONK_CATALOG_CORPUS_EVIDENCE` without editing the test.

## OrbStack lane

The lightweight disposable lane ran against the restored engine:

```text
docker context show
orbstack
docker info --format 'server={{.ServerVersion}} os={{.OperatingSystem}} arch={{.Architecture}}'
server=29.4.0 os=OrbStack arch=aarch64
docker ps
<empty>
```

No Rust or OCI builds were run. The PostgreSQL fixture created and removed its
own temporary container and database. The package acceptance uses symlinked
verified cache prefixes in its private pytest directory, so the production
cache bytes and durable snapshot are not modified. The production cache was
checked after the run and had no `snapshot.candidate.json` left behind.

## Checks run

The pinned control environment was run with the explicit editable contracts
candidate because the frozen Git dependency is not reachable in this host:

```text
PYTHONPATH=/private/tmp/vonk-forge-recipes-publication-acceptance/contracts/src:\
/private/tmp/vonk-forge-p9-launch-acceptance/control/src:\
/private/tmp/vonk-forge-p9-launch-acceptance/src \
  /opt/vonk-forge/control/.venv/bin/pytest -q -rs \
  control/tests/test_p9_fresh_launch_acceptance.py
4 passed
```

This covers fresh PostgreSQL migration/import, exact Model/Recipe closure,
package hashes and snapshot state, no legacy catalog entity routes, public
recipe projection, canonical Library model-to-recipe list pairs, canonical
recipe detail pairs, and the offline snapshot boundary.

The corroborating pre-existing connected acceptance also passed:

```text
PYTHONPATH=/private/tmp/vonk-forge-recipes-publication-acceptance/contracts/src:\
/private/tmp/vonk-forge-p9-launch-acceptance/control/src:\
/private/tmp/vonk-forge-p9-launch-acceptance/src \
  /opt/vonk-forge/control/.venv/bin/pytest -q -rs \
  control/tests/test_controller_catalog_postgres_acceptance.py
3 passed
```

That lane independently verifies the raw publication reader, fresh
PostgreSQL import of the complete corpus, and offline restart continuation.
Ruff and `git diff --check` also pass.

The earlier stale environment had failed collection because its installed
contracts package lacked `validate_model_references`. Supplying the integrated
candidate contracts source resolves that mismatch. A normal frozen `uv run`
still attempts the pinned GitHub dependency and is network-blocked here; no
synthetic success was used.

No runtime inference, NVIDIA hardware, Spark behavior, or physical acceptance
is claimed.
