## Summary
`_start_request` turned an exhausted observation (0.25 s budget, `nowait` lock on the CatalogDocument, unreadable replay evidence) into a synthetic FAILED view with `retryable=False` via `_unaccepted_request_end`. The download route sent it as 202, `ensure_preparation` made a timed blocker, update batches read it as a digest mismatch, and `prepare_install` claimed nothing and returned silently.

Now the last typed `RecipeImageAvailabilityUnknown` is re-raised:
- download route: existing `UnknownOutcomeError` mapping answers 503 (detail carries the RecipeImageCode; the app's `RetryAfterMiddleware` adds Retry-After); the client retries the same request key.
- `ensure_preparation`: lets the unknown propagate; the existing net in fleet_profiles/preparation.py turns it into a WARNING PREPARATION_NOT_STARTED naming the error.
- update batches: already treat a raised Unknown as a retryable observation; they now get it directly instead of through a mismatch.
- `prepare_install`: the raised unknown surfaces to its caller (which already handles `UnknownOutcomeError` as a wait) instead of a silent no-op.
- Deleted `_unaccepted_request_end` (about 60 lines).

Budget: kept 0.25 s with `nowait`. The loop already re-tries the lock with backoff inside the budget (a bounded wait), and anything longer would hold a request thread; the 503 retry is the longer wait.

Note: a damaged stored row under the same request key now stays unknown for that key (a fresh key admits); previously it ended terminally.

- Source-policy refusal (`availability_production.py`) is now a settled `RecipeImageAvailabilityRefused(SOURCE_POLICY_REFUSED_CODE)`, no longer a retryable unknown the sweep resubmitted forever; `_refresh_authority` re-raises security refusals, `_start_request` does not retry it, the route reaches its 409, update batches mark Refused FAILED.
- New added-line guard (`unknown_to_terminal_handlers`, mode waits): an `except *Unknown` handler may not build `LifecycleState.FAILED` or `retryable=False`.

## Class search
Grepped for handlers catching `*Unknown`/`UnknownOutcomeError` that build FAILED or `retryable=False`: the only site was `_unaccepted_request_end` (deleted); the source-policy retryable-unknown inverse is fixed here. The guard blocks new ones.

## Principles
- Unknown never falls through to a terminal state (1, 20): unknown is reported as unknown and retried.
- Delete over add: synthetic view and its residue removed; no new path.
- One contract: the typed Unknown is the single shape for all consumers.
- Same request key stays the retry identity; newest request wins.
- Tests: route 503 with the same key twice; service start/prepare_install raise unknown and the same key succeeds once the authority recovers; three old tests that asserted the synthetic FAILED view now assert the raise.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01NxX8uY9ruX1ZqmuABxwaSa
