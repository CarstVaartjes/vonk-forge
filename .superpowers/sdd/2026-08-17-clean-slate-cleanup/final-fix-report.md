# Final fix report: clean-slate cleanup

## Status

Implemented the final whole-branch review fixes in one coherent wave:

- Removed the obsolete live `package.*` operation ABI from agent/protocol/control boundaries with no compatibility aliases.
- Preserved the concrete package-helper receipt/grant client primitive while removing the deleted operation engine/dispatch path.
- Rewrote stale Playwright e2e expectations away from Catalog and `/catalog`.
- Updated supported README/runbook copy away from `/catalog`, Catalog drafts/imports, and Admin → Updates page wording.
- Extended docs contract coverage to block those removed surface names from supported docs.
- Replaced the `package.rollout.approved` audit fixture with retained Library operator activity.

## TDD red checks

```text
$ uv run --project agent_protocol --frozen pytest -q agent_protocol/tests/test_contracts.py::test_operation_enum_contains_only_supported_operations agent_protocol/tests/test_contracts.py::test_removed_package_operation_strings_are_not_protocol_claims agent_protocol/tests/test_rust_fixtures.py
3 failed, 1 passed
```

```text
$ uv run --python 3.12 --project agent --frozen pytest -q agent/tests/test_client.py::test_claim_uses_fixed_mtls_post_and_parses_canonical_protocol_claim agent/tests/test_operations.py::test_removed_package_operation_claims_never_dispatch
2 failed
```

```text
$ uv run --frozen pytest -q tests/test_docs_contract.py::test_supported_docs_do_not_present_catalog_as_an_operator_surface
1 failed
```

## Focused green checks

```text
$ uv run --project agent_protocol --frozen pytest -q agent_protocol/tests/test_contracts.py agent_protocol/tests/test_rust_fixtures.py agent_protocol/tests/test_package_helper_authority.py
365 passed in 0.33s
```

```text
$ uv run --python 3.12 --project agent --frozen pytest -q agent/tests/test_client.py::test_claim_uses_fixed_mtls_post_and_parses_canonical_protocol_claim agent/tests/test_operations.py::test_removed_package_operation_claims_never_dispatch
2 passed in 0.65s
```

```text
$ uv run --python 3.12 --project agent --frozen pytest -q agent/tests/test_client.py agent/tests/test_operations.py agent/tests/test_package_helper_client.py agent/tests/test_package_helper.py agent/tests/packages
251 passed, 1 skipped in 17.20s
```

```text
$ uv run --python 3.12 --project control --frozen --with-editable . pytest -q control/tests/test_agent_jobs.py::test_package_operation_is_not_a_control_plane_queue_operation control/tests/test_agent_jobs.py::test_package_capabilities_are_not_control_plane_agent_capabilities control/tests/test_agent_reconciliation.py::test_package_evidence_is_not_a_retained_reconciliation_result control/tests/test_orchestration.py::test_package_operations_are_not_control_plane_graph_operations control/tests/test_orchestration.py::test_persisted_plan_consumers_reject_deleted_package_operations
5 passed in 0.82s
```

```text
$ npm test -- --run src/components/admin-menu.test.tsx src/components/app-shell.test.tsx src/pages/library.test.tsx
Test Files 3 passed (3)
Tests 20 passed (20)
Duration 2.96s
```

```text
$ npm run build
✓ built in 115ms
```

```text
$ uv run --frozen pytest -q tests/test_docs_contract.py
44 passed in 0.04s
```

```text
$ cargo test -p vonk-agent-protocol
test result: ok. 0 passed; 0 failed
test result: ok. 4 passed; 0 failed
test result: ok. 2 passed; 0 failed
test result: ok. 6 passed; 0 failed
Doc-tests vonk_agent_protocol: 0 passed; 0 failed
```

## Static and e2e checks

```text
$ if rg -n "package-abi|package-backend|PackageOperationRequest|PACKAGE_OPERATIONS|RELEASE_BOUND_PACKAGE_OPERATIONS|PackageEngine|PackageDisposition|PackageInspection|PackageOperationsBoundary|\"package\.(prepare|activate|health|stop|rollback|remove|repair|gc)\"" agent/src agent_protocol/src control/src/vonk_control/agent_jobs.py rust/crates/vonk-agent-protocol/src; then exit 1; fi
<no output>
```

```text
$ if rg -n "Catalog|/catalog|Recipe catalog|Open advanced catalog" control/web/e2e; then exit 1; fi
<no output>
```

```text
$ if rg -n "Catalog drafts|Catalog imports|Admin → Updates page|Admin -> Updates page|Updates page|/catalog|Recipe catalog|Open advanced catalog" README.md docs/runbooks/platform-update.md docs/runbooks/platform-release-update.md control/web/e2e; then exit 1; fi
<no output>
```

```text
$ git diff --check
<no output>
```

```text
$ npx playwright test --list
Listing tests:
  admin.spec.ts:9:1 › the redesigned shell exposes only Fleet and Library
  fleet-library.spec.ts:210:1 › Fleet cards and bounded history are keyboard-accessible with local evidence
  fleet-library.spec.ts:236:1 › Node history chooses honest rollups on desktop and mobile
  fleet-library.spec.ts:258:1 › Fleet has no document overflow from phone through large desktop
  fleet-library.spec.ts:288:1 › Library keeps URL drill-down below 900px and three coordinated panes above it
  fleet-library.spec.ts:385:1 › Library fixture journey keeps visual authority primary through preview, partial retry, and Advanced recovery
  fleet-library.spec.ts:467:1 › Library local fixture recovers from errors and exposes an empty-state escape hatch
Total: 7 tests in 2 files
```

## Limitations and concerns

Playwright browser execution is blocked in this environment by a missing system library:

```text
$ npx playwright test e2e/admin.spec.ts
chrome-headless-shell: error while loading shared libraries: libnspr4.so: cannot open shared object file: No such file or directory
```

The complete `agent_protocol/tests` suite still has an existing fixture-data concern unrelated to the removed operation ABI:

```text
$ uv run --project agent_protocol --frozen pytest -q agent_protocol/tests/test_workload_packages.py::test_checked_in_release_lock_identity_matches_filename_and_deployment -vv --maxfail=2
FAILED agent_protocol/tests/test_workload_packages.py::test_checked_in_release_lock_identity_matches_filename_and_deployment[ds4-deepseek] - assert 0 == 1
FAILED agent_protocol/tests/test_workload_packages.py::test_checked_in_release_lock_identity_matches_filename_and_deployment[mia-deepseek] - assert 0 == 1
```

No Docker-gated check was run or blocked during this final pass.
