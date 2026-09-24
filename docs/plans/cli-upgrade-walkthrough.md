# U8 disposable upgrade walkthrough

This facilitator adds a disposable installed-CLI session for the upgrade part
of U8. It does not replace the original U8 outcome card in the
[W19 protocol](cli-operator-walkthrough.md), and it does not close the
human scorecard. The separate [cleanup setup](cli-cleanup-walkthrough.md) owns
the disposable cleanup scenario; do not perform a cleanup mutation in this
upgrade fixture.

Give a participant only the shipped [`vonkctl` runbook](../runbooks/vonkctl.md),
the installed executable, private connection details, and the original U8 task
verbatim. Do not show this document, the test, source, expected outcomes, or a
command sequence. The participant may use the runbook; record each lookup and
any request for help. The U8 human scorecard remains unrun until an independent
participant completes the full card, including explaining cleanup scope and
using JSON in a pipe.

## Run the smoke or open the interactive shell

Run these from the repository root with OrbStack available for the disposable
PostgreSQL test. The test builds and installs the current CLI wheel into a
private temporary directory, starts a registered HTTPS API, and drops only its
own PostgreSQL database during fixture teardown.

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes
export UV_CACHE_DIR=/private/tmp/vonk-forge-cli-implementation-cache

VONK_U8_UPGRADE_MODE=smoke uv run --project control --frozen --with-editable . \
  pytest -q -s control/tests/test_cli_upgrade_walkthrough.py::test_disposable_u8_upgrade_walkthrough_smoke

VONK_U8_UPGRADE_MODE=interactive uv run --project control --frozen --with-editable . \
  pytest -q -s control/tests/test_cli_upgrade_walkthrough.py::test_disposable_u8_upgrade_walkthrough_interactive
```

The interactive shell gets a private `HOME`, a private token file, and disabled
history writing. Its URL is loopback HTTPS; the token value is never printed.
Type `exit` or press Ctrl-D to close the shell and remove the wheel, TLS
material, credentials, temporary files, route files, and workspace. The PostgreSQL
fixture drops its database and stops its disposable container during teardown.
The host shell has ordinary networking and is not an OS network sandbox.

## What this fixture exercises

The installed CLI talks over HTTPS to the registered upgrade route backed by
`AgentUpgradeService`, `AgentJobService`, and PostgreSQL. It seeds two enrolled
test nodes and a shape-validated package-source fixture. A deterministic local
executor claims the first node's queued operation through the real agent-job
owner, then records a controlled failure through its normal failure path. The
smoke checks that a redirected request without `--yes` exits 2, emits one JSON
error, makes no API request, and creates no job. With explicit consent, it
checks one JSON acceptance receipt, then reads the exact job and confirms the
first node is waiting for an operator and no child was created for the second
node.

This reuses the same installed sequential-upgrade contract already covered by
[`test_fleet_upgrade_installed_cli.py`](../../control/tests/test_fleet_upgrade_installed_cli.py).
The separate [read-only JSON pipeline](cli-operator-u8-readonly.md) and
[`test_cli_json_pipeline_installed.py`](../../control/tests/test_cli_json_pipeline_installed.py)
cover a real safe pipe consumer. These connected checks are automated
facilitator evidence, not evidence that a participant discovered the commands.

The API fixture substitutes the external current-package publisher and source
loader with the test's validated package-source values. It exercises actual
Controller admission, authorization, sequencing, durable progress, and failure
reconciliation, but it does not retrieve a release from the publisher, verify
that publisher's package signature, download/install a package, run an agent,
or contact a physical Spark. Controller deployment, publication, and hardware
qualification remain separate evidence gates.
