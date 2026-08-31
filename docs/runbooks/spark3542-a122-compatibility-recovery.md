# Spark3542 a122 compatibility recovery

This runbook covers the one-shot Controller bridge for the pinned Spark3542
dev335 to a122 incident. It is not a general agent-upgrade mechanism and must
not be used for another node, package, job, or operation.

The bridge does not authorize a package install or an arbitrary reboot. It
retries the existing failed a122 operation and may issue only one fixed
60-second scheduled reboot. At boot, the enabled helper socket pulls in the
package recovery already staged on Spark3542. Issuance remains bound to the
exact original payload, package, source identity, certificate, retry fence, and
target digests.

## Preconditions

- Deploy the Controller build containing this runbook and migrations through
  `0009_compat_abandoned_at`.
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
  --confirm reboot-spark3542-to-resume-staged-a122-recovery \
  --json
```

Then repeat with `--apply`:

```console
bin/vonkctl fleet upgrade recover-spark3542 apply \
  --plan-digest '<digest-from-the-immediately-preceding-preview>' \
  --confirm reboot-spark3542-to-resume-staged-a122-recovery \
  --apply \
  --json
```

Apply is idempotent for the same plan digest. A different digest is rejected.
Once armed, the recovery is deliberately fail-closed and cannot be cancelled
or retargeted through this API. The Spark will reboot approximately 60 seconds
after the legacy agent accepts the grant.

If an operator previously used the abandonment endpoint to release the
mutation lane, run preview again. The Controller may offer a new digest-bound
preview only when the abandoned recovery is still grantless, the exact fourth
attempt and current authenticated dev335 identity still match, the historical
job and operation remain cancelled, and every other mutation is stopped or
waiting for an operator. Applying that fresh digest reopens the same attempt;
it does not create or re-enable a grant. The durable `abandoned_at` timestamp
is retained, and any paused mutation identity or payload-digest drift makes the
preview stale.

## Observe and finish

Poll the preview command. Expected states are `armed`, `issued`,
`awaiting-identity`, and then `completed` (or
`completed-before-dispatch` if a122 recovered before the grant was issued).

Completion requires the exact authenticated a122 certificate, protocol,
capabilities, payload, binary/build digests, and successful self-test. Five
minutes without dispatch or fifteen minutes without the exact post-grant
identity transitions to `operator-blocked`; the Controller does not issue a
replacement grant. Identity-only completion from `armed` or `operator-blocked`
never issues or re-enables a grant.

Only after a122 completion:

1. Preview a normal signed package upgrade for Spark3542 to the current release.
2. Apply it and verify Spark3542's exact target version, binary/build digests,
   self-test, and ready state.
3. Repeat the normal upgrade for Spark2297.
4. Remove this incident-only bridge in a follow-up release after retaining the
   audit evidence.
