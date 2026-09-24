# U8 read-only JSON pipeline

This exercise covers the safe JSON-pipeline part of U8. Run it only against a
disposable Controller with at least one enrolled test Spark. It reads Fleet
state and does not need or grant mutation consent.

```bash
set -o pipefail
vonkctl --no-input --json fleet \
  | jq -ec '
      if (.nodes | type) == "array" then
        {node_count: (.nodes | length)}
      else
        error("Fleet result has no nodes array")
      end
    '
```

For one registered node, the consumer prints one compact result such as
`{"node_count":1}` and exits 0. `--no-input` prevents prompting; it does not
authorize a mutation. `pipefail` preserves a failure from either `vonkctl` or
`jq`, so a malformed result or failed Controller read remains visible as a
nonzero command status.

The installed-process regression is
[`test_cli_json_pipeline_installed.py`](../../control/tests/test_cli_json_pipeline_installed.py).
It connects the installed wheel to a disposable HTTPS API and PostgreSQL Fleet
owner, pipes the actual CLI output into a separate JSON consumer, and checks
both process exits and unchanged operation counts.

Run exactly this node from the repository root. The required-execution plugin
turns a skipped PostgreSQL/Docker-dependent selection into a failed command:

```bash
PYTHONPATH="$PWD:$PWD/src:$PWD/control/src" \
VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes \
UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache \
uv run --project control --frozen --with-editable . \
  pytest -p control.tests.required_execution -q -s \
  control/tests/test_cli_json_pipeline_installed.py::test_installed_no_input_fleet_json_pipeline_is_read_only
```

This read-only exercise does not authorize cleanup. Use the separate
[disposable cleanup setup](cli-cleanup-walkthrough.md) for that part of U8 and
check its qualification in the [current W19 package row](cli-operator-status.md#package-state).
