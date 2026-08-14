# Durable image-distribution retry design

## Outcome

The physical acceptance runner can resume an existing evidence file after one
image-distribution attempt ended terminally. It performs at most one explicit,
deterministically keyed retry and then continues only when the retried
operation succeeds with exact node evidence.

## Observed gap

The accepted DS4 image was built and uploaded, but its first import operation
failed before Docker ran because the old agent requested a helper grant longer
than the remaining operation lease. After the repaired agent was activated,
rerunning the acceptance command replayed the same terminal distribution
operation. `Runner.operation` already supports exactly one explicit retry and
the build stage uses it, but `image_distributed` did not supply a retry key.

After the runner supplied that key, the controller returned HTTP 409. Its
generic recipe retry endpoint accepts failed build and install groups but does
not recognize `recipe.image.import.v1`. The recovery path therefore needs both
the runner request and a narrow controller implementation for this existing
operation kind.

## Design

Pass a deterministic request key for `image-distributed:distribution-retry`
to the existing `Runner.operation` call. The helper invokes the controller's
explicit retry endpoint only after a terminal state, verifies that the retry
retains the same owner, and refuses a second terminal result. Normal
first-attempt success performs no retry. Existing digest, byte-count, layout,
owner, mapped-node, and node-evidence checks remain unchanged.

Extend the existing controller retry service to accept a terminally failed or
`waiting-for-operator` `recipe.image.import.v1` group. The latter is the
durable outcome when an unsafe mutation is interrupted and the agent cannot
prove completion. It queues a new group with the exact persisted
plan digest, owner, target-node set, child payloads, and authority digest from
the terminal group. It retries every original target because image import is
digest-bound and idempotent, and because the replacement group must produce a
complete independently verifiable evidence set. It does not re-plan against a
possibly changed mapping. The service rejects malformed persisted groups,
non-terminal groups, and a second active retry for the same owner and plan. The
existing request-key lookup makes a repeated identical retry request return the
same replacement operation.

Do not add automatic controller retries, change operation authority, create a
new endpoint, or loop indefinitely. The retry is an operator-initiated
recovery action through the existing authenticated endpoint, with one
idempotent UUIDv5 key bound to the acceptance record.

## Verification

Add controller service tests proving that failed and operator-held import
groups are recreated with the exact original payloads, that request replay is
idempotent, and that a second active retry is rejected. Extend the existing
real runner/server tests so a terminal initial image distribution is followed
by exactly one retry and completes with the accepted evidence. A second
terminal result must still fail and leave the evidence at `image-built`. Run
the focused control and script tests, their complete test files, and repository
format/lint checks before resuming physical acceptance.
