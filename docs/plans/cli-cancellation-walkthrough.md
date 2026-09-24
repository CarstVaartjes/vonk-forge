# U6 disposable cancellation walkthrough

This facilitator exercises the original U6 card in the
[W19 protocol](cli-operator-walkthrough.md). It does not complete the independent
human scorecard or close the remaining admission and removal gates.

Give participants only the original outcome card, shipped
[`vonkctl` runbook](../runbooks/vonkctl.md), installed executable and private
connection details. Keep this setup guide, source and expected outcomes private
to the facilitator. Record documentation lookups and requests for help.

## Start the disposable session

Run from the repository root with OrbStack available for disposable PostgreSQL:

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes
export UV_CACHE_DIR=/private/tmp/vonk-forge-cli-implementation-cache

VONK_CANCELLATION_WALKTHROUGH_MODE=smoke \
  uv run --project control --frozen --with-editable . pytest -q -s \
  control/tests/test_cli_cancellation_walkthrough.py::test_disposable_cli_cancellation_walkthrough_smoke

VONK_CANCELLATION_WALKTHROUGH_MODE=interactive \
  uv run --project control --frozen --with-editable . pytest -q -s \
  control/tests/test_cli_cancellation_walkthrough.py::test_disposable_cli_cancellation_walkthrough_interactive
```

The launcher builds a private installed wheel and starts a loopback HTTPS API.
Its shell uses that exact executable, an isolated HOME and temporary directory,
a private token file, and disabled history writing. The host shell is not an
OS network sandbox. Exit the shell normally to tear down its temporary wheel,
TLS material, credentials and managed storage; pytest drops its own database.

Keep the shell open after requesting cancellation. The facilitator detects the
durable request, runs a worker that exits before reconciliation completes, then
restarts recovery and supplies the exact outstanding agent cancellation receipt.
It announces settlement while the shell remains available for the participant
to inspect progress. Exiting before settlement does not establish that the
participant observed recovery.

## Evidence and limits

The registered API and PostgreSQL Profile, Run/Switch, agent-job and recipe
preparation services own the operations. Setup creates a completed stop effect,
an issued start effect and a later unissued effect. The smoke checks the real
advertised preparation action, cancellation receipt and Activity projection,
process-death recovery, and a fresh installed-CLI terminal progress read pinned
to the exact application and request. It verifies that managed model bytes
remain available for reuse after cancellation.

Execution prerequisites and agent results use deterministic test fixtures;
model bytes are served by an in-process fixture transport. Build and artifact
inspection fixtures do not prove that a runtime image or model runs on a Spark.
The preparation request is queued without an image-build worker. This is
connected control-plane and managed-storage evidence, not publication,
deployment, physical execution, model quality or independent human acceptance.
Cache-removal races remain dependent on W09/W17.
