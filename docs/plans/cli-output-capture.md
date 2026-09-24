# Installed CLI output capture

The [qualified captured examples](../examples/cli-operator/README.md) are available
from source `134bcbf9`. The CLI handoff requires examples from the installed executable. The design
plan's invented layouts explain intent; they are not acceptance evidence.

`scripts/capture-cli-handoff` records the actual process streams produced by
existing disposable acceptance scenarios. It runs the installed whole-Fleet
review, stale-review refusal, cancellation/Activity and artifact-result tests.
The scenarios use a newly built CLI wheel, registered loopback HTTPS routes,
PostgreSQL and the existing service owners. Their deterministic fixtures do not
establish physical Spark execution, publication, deployment or human usability.
The idle-profile receipt is a no-effect result, not workload execution.

Run from the integration candidate with its qualified test environment:

```sh
VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes \
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-implementation-cache \
/path/to/qualified/python scripts/capture-cli-handoff \
  --source-snapshot FULL_COMMIT_SHA \
  --output /private/tmp/new-cli-capture
```

The snapshot must include the current CLI, Controller, protocol, schemas,
scenario sources and declared build inputs. The helper compares their Git blob
identities before testing, including files not yet staged in the candidate.
It accepts documentation-only changes. Use a new output directory: existing
evidence is never overwritten. The test environment needs the same disposable
PostgreSQL and local HTTPS capabilities as the installed acceptance tests.

The helper observes calls the scenarios already make; it does not submit extra
operations. It retains evidence only after the selected tests pass. A capture
contains the source checkpoint, time, scenario identifiers, command arguments,
exit statuses and streams. Pseudoterminal output retains its carriage returns.
Private fixture credentials are checked before retention. Executable paths
become `vonkctl` and fixture-root paths become `/disposable-fixture`; the other
output is retained unchanged. Review the complete capture before publishing
any selected examples, because a passing test can still leave a misleading
message unasserted.

The first capture demonstrated that distinction: actual definitive refusals
suggested repeating an invalid artifact submission or following a rejected
profile request. Those outputs are defect evidence, not approved recovery
examples. The [status record](cli-operator-status.md) tracks correction and the
source checkpoint used for the replacement capture.

Keep any published human excerpt beside its canonical JSON or complete capture,
with the scenario and source identified. Label an expected refusal as such.
Do not replace real IDs or digests with invented values, omit a contradictory
message, or describe automated output capture as the independent participant
walkthrough required by [the acceptance protocol](cli-operator-walkthrough.md).
