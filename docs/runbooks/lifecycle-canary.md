# Lifecycle hardware canary

`scripts/lifecycle-canary` proves, on a real Controller and real Sparks, the
promises of the lifecycle audit. Run it by hand after every release that
changes the lifecycle core, an adapter, the agent's results or the stored
states. CI never runs it and nothing schedules it. Do not point it at a
Controller whose profile you would not want loaded: a profile load applies the
whole profile.

## Run

Use a disposable profile (a saved profile with one small recipe on a canary
Spark) and a Controller you chose.

```bash
export VONK_CONTROL_URL=https://forge.example.test
export VONK_CONTROL_TOKEN_FILE="$HOME/.config/vonk-forge/controller-token"
scripts/lifecycle-canary --profile 3 --minutes 20 \
  --restart-command "ssh nas docker compose -f /srv/vonk/compose.yml restart control"
```

`--minutes N` is how long an application may stay non-terminal before it counts
as a stuck admission (default 20). Without `--restart-command` the script
prints when to restart the Controller by hand during the third scenario. The
CLI is `vonkctl` unless `VONKCTL` or `--vonkctl` names another.

## What it does

1. **Load.** `profile load --yes --detach`, followed to a terminal state. It
   must end `succeeded`.
2. **Cancel during start.** A second load is cancelled right after it was
   accepted. It must end terminal as `cancelled` (or `succeeded` or
   `superseded` when the cancel lost the race), never stuck, never waiting for a
   person.
3. **Controller-restart-tolerant wait.** A third load is followed while the
   Controller restarts. Errors while the Controller is away are counted and
   retried; the load must still end `succeeded`.

During every wait, and after each scenario, it reads `fleet activity` and fails
if any row is in an operator wait (`needs-operator`, or the legacy
`waiting-for-operator`) without an advertised operator action. A load that is
still not terminal after N minutes fails as a stuck admission.

Exit status: 0 all passed, 1 an invariant failed (each failure is printed),
2 the connection was missing or unusable and nothing was started.

## When it fails

Keep the output and `vonkctl fleet evidence OPERATION_ID --output evidence.json`
for the failing operation, and block the release. A failure of the second
invariant (operator wait without an action) is a lifecycle bug, not an
operator task: do not `resume` it away before capturing the evidence.
