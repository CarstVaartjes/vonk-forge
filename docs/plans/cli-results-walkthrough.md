# U7 results walkthrough setup

This opt-in facilitator setup covers the U7 result-discovery boundary. It is
not a human-participant result and does not close W19.

The setup builds and runs an independently installed `vonkctl` wheel against
the registered Controller API over loopback HTTPS and two uniquely named,
disposable PostgreSQL databases. One database holds a current route published
by `RecipeRouteService` and a Profile owner bound to that exact run. The other
holds a running recipe and three artifact jobs created through the installed
CLI: a finalized draft with no result, a succeeded job with a verified file,
and a succeeded job with an empty manifest.

The file and empty result use a deterministic test executor. It obtains the
real queued agent claim, stores output bytes through `ArtifactJobService`, and
submits a canonical result through `AgentJobService.record_result` and the
registered result-consumption callback. This exercises artifact validation,
durable completion, blob publication, the authenticated result APIs, and the
installed CLI's download verification. It does not execute a real agent or
recipe on a Spark and makes no model-quality, publication, deployment, or
physical-acceptance claim.

Run the noninteractive setup smoke from the repository root:

```bash
VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes \
UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache \
VONK_RESULTS_WALKTHROUGH_MODE=smoke \
uv run --project control --frozen --with-editable . \
  pytest -q -s control/tests/test_cli_results_walkthrough.py
```

Replace `smoke` with `interactive` to open the disposable shell. It prints the
Profile number and artifact run ID; give the participant only the shipped
runbook and the U7 outcome card. The smoke checks the exact Profile-owned
published endpoint, verifies the ready draft remains unavailable, distinguishes
that from the succeeded empty result, and downloads the succeeded file to a
private temporary directory while checking its SHA-256.

The interactive shell uses a private token file with mode `0600`, a temporary
home and working directory, and a loopback TLS certificate trusted only for
this run. Its ordinary host networking is not an OS network sandbox. The setup
starts no worker, uses no production credential, performs no external Fleet
mutation, and contacts no Spark. Type `exit` or press Ctrl-D to close the shell.
The harness removes its temporary wheel, local files, storage, and both
per-run PostgreSQL databases during normal teardown. Do not force-kill the test
process; that can bypass cleanup.

The following U7 dimensions remain unsupported by this setup: real agent or
Spark execution; a route backed by a physically running model; service
availability outside the local API peer; production authorization and
publication; and human participant outcomes. Retain those as separate
acceptance work. A smoke or shell launch is not evidence that an operator
completed the task unassisted.
