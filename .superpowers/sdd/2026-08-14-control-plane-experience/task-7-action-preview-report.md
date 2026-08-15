# Task 7 action-parity report — digest-bound Stop and Remove previews

Implementation commit: `48fb0a2` (`feat(control): add digest-bound recipe action previews`), based on `68efdf20cdef0ec2395d3ec183c63a4195efe5ef` on `work/control-plane-frontend-ux`.

## Delivered contract

- Added strict administrator-only Stop and uninstall preview DTOs and routes with explicit operation IDs. Apply requires both `plan_digest` and `request_key`; there is no request-key-only bypass.
- Added canonical action plans whose SHA-256 digests cover stable owner, authority, state, immutable rank, route, reservation, recipe-content, byte, and active-run facts while excluding human copy.
- Stop recomputes and locks the selected run's exact rank and memory-reservation evidence. Uninstall recomputes and locks the selected installation's exact rank, content, byte, active-run, and active-operation evidence.
- Idempotency binds request key, operation kind, owner kind/ID, and submitted action digest. A unique request-key race resolves to the matching operation or the normal conflict shape, never a raw `IntegrityError`.
- Stop and uninstall queue one parent plus the complete exact child group transactionally. Partial Stop failure retains all active capacity reservations. Uninstall never implicitly stops a run and retains the local recipe/revision catalog authority.
- Unknown failed/partial uninstall residue reports `bytes_removed: null`, `uninstall.bytes_unknown`, and `allowed: false`.

## Route publication transaction

Every `RecipeRouteService` publish, withdrawal, and maintenance publication now enters one `RoutePublicationOwner` singleton critical section. PostgreSQL uses `SELECT ... FOR UPDATE OF route_publication_owner`; SQLite uses a process-wide reentrant lock because SQLite ignores `FOR UPDATE`.

The public wrappers own their transaction. Candidate construction, external publication, activation projection, and all affected `RecipeRun` route-state writes use the same caller session. Candidate runs and `RunNode` rows are bulk-loaded and locked in deterministic order.

Stop performs a fast owner/digest replay read, enters the owner-serialized transaction, rechecks replay, locks and recomputes the action plan, prepares the same-session route candidate, writes and flushes the complete queue group, externally publishes the withdrawal, projects activation and route states, commits, releases serialization, then notifies workers.

Failure behavior is deliberately asymmetric and safe:

- A stale or blocked digest has no route or queue side effect.
- A queue failure happens before external publication and rolls back the run/job group, with no withdrawal.
- An external withdrawal failure rolls back the run/job transaction.
- If external withdrawal succeeds but database commit fails, the external route remains withdrawn while the database job/run mutation rolls back. This is the safe side: there is no live route racing uncommitted Stop work and no job falsely claiming execution. A fresh preview/apply can retry from authoritative database state.

## TDD evidence

Observed RED before implementation/reconciliation:

- Concurrent disjoint SQLite withdrawals published crossed candidates; the final candidate still contained `second` instead of being empty.
- The PostgreSQL owner-lock statement seam did not exist.
- Stop/uninstall preview API requests returned missing-route failures and digest-bound apply requests failed against the old request-key-only contract.
- The central authorization-matrix test reported both new preview routes as unregistered mutations.

Regression coverage includes stable and changed action digests, exact selected-owner/rank isolation, reservation authority, stale route/state/rank/reservation/byte rejection before side effects, owner-bound replay and cross-owner conflicts, queue rollback, withdrawal rollback, post-publication commit failure, partial Stop capacity retention, catalog retention, no implicit Stop, unknown bytes, strict/admin API schemas, OpenAPI operation IDs, SQLite duplicate requests and disjoint publication, PostgreSQL lock compilation, and Docker-gated PostgreSQL disjoint/duplicate races.

## Verification

| Command/scope | Result |
| --- | --- |
| Focused operations/routes/API/OpenAPI/auth matrix | `84 passed, 3 skipped, 15 warnings` |
| Relevant broader recipe/admission/library/catalog/API suite | `213 passed, 4 skipped, 21 warnings` on the final post-commit run |
| PostgreSQL recipe concurrency collection | Three clean skips: Docker required for final-rank serialization, disjoint Stop serialization, and duplicate Stop request tests |
| `uvx --from ruff==0.16.1 ruff check` on all ten implementation/test Python files | All checks passed |
| Pinned Ruff format check on eight owned/formatted files | Eight files already formatted |
| CPython `compileall` on all changed Python files | Passed |
| `git diff --check` / staged diff check | Passed |

A diagnostic full `control/tests` run reached `1863 passed, 61 skipped`, then reported `65 failed, 42 errors`. Those failures are outside the changed recipe/API paths and are the repository's documented macOS/no-Docker gate limitations: mandatory PostgreSQL fixtures call `pytest.fail` without Docker, while Linux/root/`/proc/self/fd`/Unix-socket tests fail on this host. Relevant focused and broader suites are green. `api.py` and `test_operation_api.py` retain pre-existing whole-file Ruff-format debt; their changed hunks are minimal and formatter-aligned, avoiding unrelated rewrites.

## Scope

Changed only recipe action plans, recipe operation/route/API wiring, the explicit authorization registry, and corresponding backend tests. No generated client, frontend, Rust, MIA, runtime, readiness, desired-state, dependency, migration, or live-system file changed. Nothing was pushed and no pull request was opened.
