# U5 observer walkthrough setup

**Status:** disposable setup and automated process evidence only. No human
participant scorecard has been completed; this does not close W19.

The opt-in [`test_cli_observer_walkthrough.py`](../../control/tests/test_cli_observer_walkthrough.py)
reuses the real Profile application setup and installed CLI HTTPS helpers. It
accepts an initial queued application through `FleetProfileService`, with no
worker started. After the first latest-progress read, the actual Profile API
owner admits a newer queued application for a disjoint fixture node. The
operator credential has viewer permission; the separate fixture administrator
credential is kept in process memory and is not written to the shell's token
file. Neither application is marked complete or given a synthetic success.

Run the bounded installed-process qualification from the repository root:

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes
export UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache

VONK_OBSERVER_WALKTHROUGH_MODE=smoke \
PYTHONPATH="$PWD:$PWD/src:$PWD/control/src" \
uv run --project control --frozen --with-editable . \
  pytest -p control.tests.required_execution -q -s \
  control/tests/test_cli_observer_walkthrough.py::test_disposable_cli_observer_walkthrough_smoke

VONK_OBSERVER_WALKTHROUGH_MODE=interactive \
PYTHONPATH="$PWD:$PWD/src:$PWD/control/src" \
uv run --project control --frozen --with-editable . \
  pytest -p control.tests.required_execution -q -s \
  control/tests/test_cli_observer_walkthrough.py::test_disposable_cli_observer_walkthrough_interactive
```

The smoke interrupts the real installed follower, verifies that its JSON keeps
the interruption result separate from the still-queued remote application, and
starts a fresh installed process pinned to that application's ID. The fresh
observer times out while a newer application is now the Profile's latest. The
test also confirms that the viewer cannot cancel the application, the CLI made
only GET requests, both durable owners remain queued, and its temporary wheel,
token, TLS files, and managed files were removed. Pytest teardown drops only
the run's PostgreSQL database and stops its uniquely named disposable
PostgreSQL container.

The second command opens the human shell. The first
`profile progress` request reads the original receipt; while the test peer is
delivering it, the API admits the newer disjoint application. The facilitator
should give the participant only the shipped [`vonkctl` runbook](../runbooks/vonkctl.md)
and U5 outcome card. The participant can interrupt a foreground observer and
use its exact-ID reconnect receipt from a fresh process or shell. Close the
shell with `exit` or Ctrl-D so
the test can stop the HTTPS peer and remove its temporary files. Forced process
termination may bypass pytest fixture cleanup.

The API is a loopback HTTPS peer over disposable PostgreSQL and the host keeps
its normal network access; this is not an OS network sandbox. Keep the local
Controller URL and disposable credential. The setup starts
no workers or real Spark target and makes no claim about execution, hardware, or
human usability. The participant scorecard and the remaining U1–U8
acceptance prerequisites remain separate gates.
