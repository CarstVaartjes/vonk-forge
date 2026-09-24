# U4 whole-Fleet load facilitator

**Facilitator-only:** do not give this guide or the expected outcomes below to
the participant. Give the participant the shipped `vonkctl` runbook, the
independently installed CLI, a private short-lived credential, and this task
card verbatim:

> Review a whole-Fleet load, identify affected and idle Sparks, and explain its
> effects before accepting. Handle a stale review without executing changed
> effects, then repeat the consent decision noninteractively.

This is a disposable control-plane exercise. The installed CLI talks through a
loopback HTTPS peer to the registered API routes and the real PostgreSQL-backed
Profile and Run/Switch owners. The fixture begins with a withdrawn running
endpoint on two affected Sparks and one idle Spark. No worker or Spark network
target is connected. The test setup records its prerequisite install and run
through the existing service test helpers, which call the lifecycle service's
result methods. Its deterministic artifact inspector is used only to make the
preview's prerequisite assets available; no artifact bytes are built or
published. The setup does not claim physical Spark, artifact publication, or
deployment acceptance.

The first load request is made stale at the real HTTP/API boundary. After the
CLI displays the complete review and the operator confirms, the HTTPS peer
updates the saved Profile through its registered `PUT /api/profile/{number}`
route, changing the assignment from running to installed-only. The original
`POST /load` then reaches the production owner with the old plan digest and is
refused as stale. The CLI displays the current review, whose effects no longer
include a start. The stale request creates no application, job, or Run/Switch
child. A fresh, explicitly consented request can be admitted with the exact
current digest; it remains queued because the fixture starts no worker.

## Launch

Run the disposable smoke from the repository root:

```bash
VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes \
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-implementation-cache \
VONK_U4_MODE=smoke \
PYTHONPATH=control/src:src:/opt/vonk-forge-recipes/contracts/src \
/opt/vonk-forge/control/.venv/bin/python -m pytest -q -s \
  control/tests/test_cli_fleet_load_walkthrough.py
```

Replace `smoke` with `interactive` to open the human session. The participant
gets a disposable shell and the task card above. Let the participant find the
workflow from the shipped runbook; do not provide the expected digest or show
this guide. They should inspect the refreshed review after the stale refusal
and make the second decision without an interactive prompt.

For facilitator diagnosis only, the second scripted request must bind the
digest from the refreshed review and pass explicit consent. Its shape is:

```bash
vonkctl --no-input --json --profile 1 profile load \
  --expected-plan <current-lowercase-plan-digest> \
  --yes --request-key <new-request-uuid> --detach
```

The old digest must fail. The accepted receipt must retain the new digest and
request UUID. `--yes` alone is not consent to an unknown or changed plan.

## Evidence and limits

`VONK_U4_MODE=smoke` drives the independently installed wheel through real
HTTPS/API and PostgreSQL owners. It checks that all Fleet members and both
affected Sparks are visible before the first consent input; that the idle
Spark is identified; and that the stale request leaves operation-owner rows
and the prior run unchanged. It then checks that the refreshed review changes
from one start plus one stop to one stop, and that noninteractive acceptance
uses the exact fresh digest with `--yes`. The accepted owner record stays
queued. The smoke is not a participant result.

`VONK_U4_MODE=interactive` runs a local shell with a 15-minute limit. Type
`exit` or press Ctrl-D to finish. Normal pytest teardown drops only that run's
PostgreSQL database, stops its uniquely named disposable container, closes the
local HTTPS peer, and removes the temporary wheel, token, key, and work files.
Do not force-kill the test process, replace the loopback Controller URL, or use
a production credential.

The shell has ordinary host networking; this fixture is not an OS network
sandbox. Its credential is signed by a temporary key and points only at the
loopback peer. No real Spark, worker, workload dispatch, cache publication,
deployment, or model-quality path is exercised. Record any human outcome in
the shared W19 scorecard separately; this setup does not close W19 or any
physical acceptance gate.
