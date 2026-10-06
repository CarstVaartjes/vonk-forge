# Principle guard inventories

T1 adds syntax ratchets, not proof of runtime recovery. Existing findings are
recorded under `debt`, without pretending they meet an exception reason. The
owning repair track removes findings and lowers the matching count. Line
numbers are diagnostic; identity is path, enclosing function and finding kind.

Run the guard suite with the locked Controller environment:

```bash
uv run --project control --frozen pytest control/tests/test_principle_guards.py control/tests/test_non_blocking.py -q
```

Use the named scanner modules for individual inventories:

```bash
uv run --project control --frozen python -m control.tests.unbounded_waits
uv run --project control --frozen python -m control.tests.read_refusals
uv run --project control --frozen python -m control.tests.remedy_text
uv run --project control --frozen python -m control.tests.retention_boundaries
uv run --project control --frozen python -m control.tests.anti_principle_tests
```

Each supports `--lower`, which refuses new or increased counts. The tests also
compare allowances against `origin/main`: increasing a listed count or adding
new debt fails. A new exception requires one of `security-edge`,
`irreversible-keep`, `legacy-alias-read`, `input-validation`, `service-absent`,
and a written justification. Exception counts are ratcheted too. Run the guard
suite after editing an exception; it must match actual findings exactly.

The wait inventory covers Python while loops and known blocking subprocess,
URL, wait, join and poll calls, Rust loops and zero-argument wait/recv/notified/join calls, and shell while/until loops under
`control/src`, `rust/crates`, `scripts`, and `src`. A Python deadline must appear
in a loop condition or conditional branch. Rust bounds are recognized by tokens
inside the loop; comments and strings cannot provide a bound. These checks do
not prove a deadline actually expires. Ordinary daemon loops can therefore
remain inventory debt until reviewed. Dynamically dispatched calls and bounds
implemented in a different helper need behavioral coverage.

The read inventory detects explicit error statuses in decorated GET handlers and reachable helpers within the same module;
authentication and not-found statuses are excluded. Actual service-absent
responses, query validation and stream integrity failures remain legitimate
reviewed exceptions; a shared helper may serve both reads and mutations. An
unavailable authoritative database cannot be projected as an empty successful
list. Stored-row damage and an unavailable dependency are different classes. It is a syntax inventory; dynamic dispatch and cross-module helpers remain outside its call graph. The remedy inventory flags imperative string literals
in source, including tooling, so descriptive uses can also need review.
Retention inventories mapped tables without linked ORM/bulk/SQL deletion; a
found delete is not proof of age-based pruning or bounded growth.

The assertion scan uses the shared parser without retaining previously uncached
test trees: they are consumed once, and retaining all of them burdens pytest GC.
The test inventory preserves inequality assertions while flagging positive
operator-wait assertions, GET refusal assertions, transient failure assertions
without an asserted reason code, withdrawal under mismatched observations, and
ending calls without a later fresh request or the shared ending helper.
The transient classification is heuristic and current findings require review
by the track that owns the behavior. It does not rewrite product tests.

`non_blocking.assert_ended_without_blocking` accepts fixture adapters exposing
`state` and `request_key`. Failed endings must carry a typed reason via
`reason_code`, `blockers[].code`, or `failure.code`; an exposed `refusal` must
be empty for a fresh receipt. Actual Job ORM rows may expose the identity as
`request_id`; the helper compares either form without inventing receipt fields.
A service whose typed failure belongs to a child supplies `assert_reason(ended)`
to inspect that canonical child evidence. Worlds expose a Session or
session factory as `sessions`, or an explicit `holds` inventory for small
in-memory fixtures. The SQL path checks removal gates, active/promised
reservations, active model-cache leases, and live job/agent attempts. A fresh
callback must return an admitted receipt with a different nonempty request key.
Service-specific busy flags and other holds belong in the fixture's explicit
hold inventory or its `assert_released` callback, checked before and after
admission. Concurrency and physical effects still need their owning lanes.

`scripts/lifecycle-counts` includes unaudited raise debt and keys operator waits
by operation kinds. JSON also exposes the additional guard inventories. Generated API client extension
dictionaries remain generator-owned and are excluded from the authored mapping inventory. The
four-field before/after report remains usable by existing PR automation.

Builtin/HTTP raises outside the audited classifier can be inventoried without
claiming they are all bookkeeping failures:

```bash
uv run --project control --frozen python -m control.tests.principle_raise_report
```

This report does not fail or establish an exception baseline. Security and
contract raise sites need classification by their owning repair track.

`upgrade_continuity.assert_upgrade_keeps_serving` is the shared fixture adapter
for the serving continuity lane. Supply real observations and an actual
upgrade/restart callback; its self-tests only prove that loss of a running run
or its exact route is rejected. T2 owns connecting that adapter to its real
recovery boundary.
