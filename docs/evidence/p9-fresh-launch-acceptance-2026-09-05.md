# P9 fresh-launch acceptance evidence

This evidence belongs to the isolated `codex/p9-launch-acceptance` checkout.
The base is `codex/interface-integration` at
`49124cc33261877b02fdc8d8f2f243eb37b9e04a`. The test owner only added
`control/tests/test_p9_fresh_launch_acceptance.py` and this evidence file.

The acceptance consumes the existing immutable evidence and cache paths:

- `/private/tmp/vonk-production-reader-corpus-evidence.json`
- `/private/tmp/vonk-canonical-import-corpus-evidence.json`
- the production reader `cache_root` named by the first file

The index is schema 2 for `CarstVaartjes/vonk-forge-recipes`, publication
commit `2001c6502bfdc66141dd7224bfde5d77734e9959`, and currently contains 92
Models and 84 Recipes. The package archives contain 81 distinct Model
snapshots, leaving 11 valid Models without a matching Recipe. The test derives
the catalog and package counts from the index so additions do not require
rewriting the assertions.

## Checks run

Using the existing control virtual environment and a task-specific cache:

```text
/opt/vonk-forge/control/.venv/bin/pytest -q \
  control/tests/test_p9_fresh_launch_acceptance.py -k 'corpus or snapshot'
3 passed

/opt/vonk-forge/control/.venv/bin/pytest -q \
  control/tests/test_p9_fresh_launch_acceptance.py
3 passed, 1 skipped

PYTHONPATH=/private/tmp/vonk-forge-recipes-publication-acceptance/contracts/src:\
/private/tmp/vonk-forge-p9-launch-acceptance/control/src:\
/private/tmp/vonk-forge-p9-launch-acceptance/src \
  /opt/vonk-forge/control/.venv/bin/pytest -q \
  control/tests/test_p9_fresh_launch_acceptance.py
3 passed, 1 skipped

The same explicit candidate-contract path allows the pre-existing connected
catalog acceptance to collect:

```text
PYTHONPATH=/private/tmp/vonk-forge-recipes-publication-acceptance/contracts/src:\
/private/tmp/vonk-forge-p9-launch-acceptance/control/src:\
/private/tmp/vonk-forge-p9-launch-acceptance/src \
  /opt/vonk-forge/control/.venv/bin/pytest -q \
  control/tests/test_controller_catalog_postgres_acceptance.py
2 passed, 1 skipped
```
```

The passing checks validate canonical index closure, the 11 unlinked Model
invariant, every package archive’s expected byte count and SHA-256, and the
promoted durable `snapshot.json` state. The connected PostgreSQL/API test is
the skipped test.

The requested OrbStack lane is currently unavailable:

```text
docker context show
orbstack
docker info
failed to connect to the docker API at
unix:///Users/carstvaartjes/.orbstack/run/docker.sock
```

Before the integrated profile change, the pre-existing connected acceptance
was attempted with only the checked-out platform source on `PYTHONPATH`:

```text
PYTHONPATH=/private/tmp/vonk-forge-p9-launch-acceptance/control/src:/private/tmp/vonk-forge-p9-launch-acceptance/src \
  /opt/vonk-forge/control/.venv/bin/pytest -q \
  control/tests/test_controller_catalog_postgres_acceptance.py
```

That stale environment collected with the following blocker:

```text
ImportError: cannot import name 'validate_model_references'
from 'vonk_forge_contracts.resolver'
```

The first `uv run --project control --frozen --with-editable .` attempt is
also network-blocked while trying to fetch the pinned contract commit from
GitHub. No synthetic PostgreSQL/API success is claimed.

## Rerun after integration

The integrated profile change removes that import mismatch. With the
published contracts candidate source available and OrbStack healthy, run:

```text
UV_CACHE_DIR=/private/tmp/vonk-p9-uv-cache \
  uv run --project control --frozen --with-editable . pytest -q \
  control/tests/test_p9_fresh_launch_acceptance.py \
  control/tests/test_controller_catalog_postgres_acceptance.py
```

If the frozen environment still resolves the old Git dependency, prepend the
candidate contract source explicitly as in the passing `PYTHONPATH` command
above, or install that candidate into the task environment before rerunning.

That connected lane will create a disposable PostgreSQL database, migrate a
fresh schema, import the complete Model/Recipe corpus, verify public list and
local detail model-to-recipe identities, and exercise the package snapshot
after publication becomes offline. It does not qualify runtime inference,
NVIDIA hardware, Spark behavior, or physical acceptance.
