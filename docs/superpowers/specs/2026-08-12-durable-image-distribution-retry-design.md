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

## Design

Pass a deterministic request key for `image-distributed:distribution-retry`
to the existing `Runner.operation` call. The existing helper invokes the
controller's explicit retry endpoint only after a terminal state, verifies
that the retry retains the same owner, and refuses a second terminal result.
Normal first-attempt success performs no retry. Existing digest, byte-count,
layout, owner, mapped-node, and node-evidence checks remain unchanged.

Do not add automatic controller retries, change operation authority, create a
new endpoint, or loop indefinitely. The retry is an acceptance-orchestration
recovery action with one idempotent UUIDv5 key bound to the acceptance record.

## Verification

Extend the existing real runner/server tests so a terminal initial image
distribution is followed by exactly one retry and completes with the accepted
evidence. A second terminal result must still fail and leave the evidence at
`image-built`. Run the focused tests, complete script test file, and repository
format/lint checks before resuming physical acceptance.
