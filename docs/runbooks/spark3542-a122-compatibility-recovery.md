# Spark3542 a122 compatibility recovery

This runbook covers the one-shot Controller bridge for the pinned Spark3542
dev335 to a122 incident. It is not a general agent-upgrade mechanism and must
not be used for another node, package, job, or operation.

The bridge does not authorize a package install or reboot. It retries the
existing failed a122 operation and may issue only the exact
`restart-vonk-unit(helper)` grant required to resume the package recovery that
is already staged on Spark3542.

## Preconditions

- Deploy the Controller build containing this runbook and migration `0006`.
- Leave the ordinary current-version upgrade for Spark3542 in
  `waiting-for-operator`; do not resume it.
- Do not start an upgrade or repair on Spark2297 while recovering Spark3542.
- Use an administrator token and the intended Controller URL.
- Confirm Spark3542 is online. The preview independently verifies its exact
  certificate, dev335 binary/build identity, protocol capability, self-test,
  historical job/operation/attempt, and staged a122 payload.

## Preview

Run the fail-closed preview and keep the returned `plan_digest`:

```console
bin/vonkctl fleet upgrade recover-spark3542 preview --json
```

The command must report state `preview` or the current durable recovery state.
A `409` means the live state no longer matches the pinned incident; stop and
investigate instead of modifying the historical transaction.

## Apply

First render the exact mutation without sending it:

```console
bin/vonkctl fleet upgrade recover-spark3542 apply \
  --plan-digest '<digest-from-the-immediately-preceding-preview>' \
  --confirm restart-staged-a122-recovery-on-spark3542 \
  --json
```

Then repeat with `--apply`:

```console
bin/vonkctl fleet upgrade recover-spark3542 apply \
  --plan-digest '<digest-from-the-immediately-preceding-preview>' \
  --confirm restart-staged-a122-recovery-on-spark3542 \
  --apply \
  --json
```

Apply is idempotent for the same plan digest. A different digest is rejected.
Once armed, the recovery is deliberately fail-closed and cannot be cancelled
or retargeted through this API.

## Observe and finish

Poll the preview command. Expected states are `armed`, `issued`,
`awaiting-identity`, and then `completed` (or
`completed-before-dispatch` if a122 recovered before the grant was issued).

Completion requires the exact authenticated a122 certificate, protocol,
capabilities, payload, binary/build digests, and successful self-test. Five
minutes without dispatch or fifteen minutes without the exact post-grant
identity transitions to `operator-blocked`; the Controller does not issue a
replacement grant.

Only after a122 completion:

1. Preview a normal signed package upgrade for Spark3542 to the current release.
2. Apply it and verify Spark3542's exact target version, binary/build digests,
   self-test, and ready state.
3. Repeat the normal upgrade for Spark2297.
4. Remove this incident-only bridge in a follow-up release after retaining the
   audit evidence.
