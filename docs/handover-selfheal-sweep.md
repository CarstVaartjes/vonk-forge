# Sweep clear and cancelled-download recovery

This note records source evidence for the selfheal track. Production was not
contacted. The reported load `d025a5d4-15b5-4fd4-a35d-d884a05c155a` and model
`vonk-forge/skintokens-pytorch-single` are incident routing clues, not verified
current runtime state.

## Controller changes

Run/Switch stop observation now has one acceptance-time budget, using the
existing final-verification observation budget. It covers phase exceptions,
blocked-plan refreshes, pending children and inactive targets. Restart or a
changed retry cause cannot reset it. Expiry ends the observer with
`RunSwitchCode.FINAL_VERIFICATION_TIMEOUT`; the runtime owner retains authority
for issued effects, and no route is changed by that ending.

A missing exact run ends with `RunSwitchCode.STOP_TARGET_DISAPPEARED` and the
existing dead-owner reservation reconciler releases orphaned bookkeeping.
Neither absence nor expiry certifies physical capacity as free. Admission
continues to account for runtime inventory and live resource owners.

For an uncertain stop child, the existing lifecycle observation of the exact
run can establish completion when the run and every rank are stopped and its
route is withdrawn. The parent then advances through ordinary verification,
instead of waiting forever for a lost child acknowledgement.

Hermetic regressions prove each ending admits a fresh operation, expiry keeps
a published route intact, and an observed stop completes. These tests exercise
Controller persistence and its ordinary worker path; they do not establish
physical Spark qualification or PostgreSQL concurrency acceptance.

## Separate recipes-repository work still required

Read-only inspection of `/opt/vonk-forge-recipes/spark_sweep/prefetch.py` found
that terminal `cancelled`/`superseded` observations call `_retire()`, whose
request-key branch changes the record back to observing while retaining the
same key. This directly explains “download.cancelled; observing original
request”. The Controller correctly preserves the original request's terminal
receipt. A new key already admits a fresh selector download, including after
Controller restart; the added regression proves it can complete.

The sweep must distinguish a terminal receipt from an unreadable/nonterminal
observation. On terminal cancellation/supersession it needs to retain audit
identity separately, release the current request identity and schedule fresh
intent with a new key after bounded cooldown. Read failures must keep observing
the original key, rather than issue duplicate effects.

The sweep must also settle terminal clearing-load failures and re-observe
actual fleet state before starting any fresh clear. A terminal receipt must
not remain an indefinite lane/owner gate.

“pin profile could not be read” is produced by the sweep's pin-profile read
path. The supplied text alone cannot establish a Controller refusal or a
missing profile. Its read retry needs bounded scheduling and a visible cause;
failed pin observation must not become an unrelated permanent download gate.
No recipes files were edited by this track.
