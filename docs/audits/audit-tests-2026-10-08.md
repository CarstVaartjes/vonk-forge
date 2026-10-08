# Test audit — 2026-10-08

Audit base: `bc22776753d2dd9b2c15c57d593c25f05dffa027` (PR #1281). Static, targeted inspection of `control/tests`, `tests/`, `agent_protocol/tests`, and `rust/crates/*/tests`; no tests executed, no source or test changes. Discovery covered the scoped tree; this is not a claim that every test body was reviewed.

The owner's supplied principles are the yardstick. In particular, an OS error reading local bookkeeping is not an authorization decision. Do not bypass permissions, follow unsafe paths, trust unverified bytes, or silently change accepted effects to recover. Treat unavailable local evidence as a miss/unknown, reconcile within a bound, and leave fresh authorized requests admissible.

Ranked below by product impact. **P1** requires the wrong product outcome; **P2** makes recovery decisions depend on diagnostics or fails to exercise the claimed recovery; **P3** unnecessarily freezes internal representation. History uses read-only `git blame` on the cited assertions and `git show`/`git log` for commit dates and PR subjects. “October 8” means Europe/Amsterdam. A later edit to the same file is not attributed as introducing an older assertion.

## P1 — wrong expected outcomes

### 1. Local receipt permissions are classified as terminal security refusal

- **Location:** `control/tests/test_runtime_image_preparation.py:1211`, `test_receipt_permission_denial_remains_a_security_refusal`; assertions at 1230–1240.
- **Today:** injects `PermissionError(13, ...)` into `Path.read_text` for a managed local receipt; requires `SecurityRefusalError`, `PERMISSION_DENIED`, and `_retryable(...) == False`. Its docstring explicitly forbids repair or a cache miss. Restoring the read only proves a direct read works, not recovery of the refused request.
- **Conflict:** self-healing; unknown → observe → reconcile; fail closed only at real security edges. This fixture supplies no revoked grant, authenticated denial, or ingress integrity failure. It makes unreadable local metadata a terminal authorization event.
- **Rewrite:** keep the OS restriction intact; assert a typed unknown/miss, healthy sibling reuse, no unverified artifact consumption, and no broad scan failure. Restore readability and drive the actual preparation worker until the current request reuses the exact verified image. Also admit a fresh request while the old attempt is unknown/ended. Separately inject an actual authorization denial and assert no effects; do not infer one from receipt file I/O.
- **Confidence:** high. **Introduced by a PR merged October 8:** no — `dd4fb63e6`, #1181, October 6 (assertion blame).

### 2. Unreadable cached bytes make preview raise instead of report a miss/unknown

- **Location:** `control/tests/test_model_cache.py:1516`, `test_preview_uses_durable_verified_cache_metadata_without_reading_model_bytes`; unreadable branch at 1558–1569.
- **Today:** most missing/damaged cache states produce byte counts; the `unreadable` case patches `os.open` to raise `PermissionError` and requires that exception to escape `download_preview`, then returns early.
- **Conflict:** non-blocking and self-healing. Local cache access failure takes down preview rather than exposing uncertain availability. The early return excludes the very recovery/admission assertions this case needs.
- **Rewrite:** require a readable preview with the affected object excluded from confirmed reusable bytes or explicitly unknown, healthy objects still counted, and no ordinary full-byte rehash. With the fault active, submit fresh preparation and assert it is admitted/observes the dependency without false success. Restore access, advance the real worker, and assert exact-byte reuse or bounded verified preparation. Do not chmod or otherwise bypass the denied read.
- **Confidence:** high. **October 8:** no — unreadable exception assertion `8c773e32e`, #1198, October 7; underlying receipt-based preview test predates it (#783).

### 3. An unreadable stored receipt is required to be an HTTP 422 refusal

- **Location:** `control/tests/test_recipe_image_availability_api.py:398`, parameter of `test_download_keeps_the_special_case_refusals_unchanged` (function at 408).
- **Today:** `runtime_image.receipt_invalid` / “source-build receipt is not readable” is a mocked service exception expected to become HTTP 422 with exact prose.
- **Conflict:** local state must be a miss/unknown, never a request refusal; behaviour over reason strings. The operator's request is valid; stored receipt damage is not invalid input.
- **Rewrite:** remove this local-state case from the invalid-request table. Corrupt an actual stored receipt behind the route's real service, submit a new download request, and assert an accepted operation that repairs/rederives the receipt, or ends unknown within its bound while releasing holds. Repeat with a fresh request key and require admission. Keep 422 for genuinely invalid caller input. Verify valid cached bytes are reused only after their identity is established, and that no work is dispatched with unknown identity.
- **Confidence:** high. **October 8:** no — `5ed8277d2`, #863, September 20.

### 4. A damaged local runtime receipt is baked into a terminal install blocker

- **Location:** `control/tests/test_recipe_operations.py:6534`, `test_a_bounded_install_blocker_keeps_the_specific_cause`; refusal assertion at 6550–6560.
- **Today:** supplies `install.compiled_plan_unavailable` with “runtime image receipt identity is unavailable or malformed”; requires `RecipeOperationConflict`, “install plan is blocked”, and the word “malformed”. Uses an uninitialized service and synthetic plan, so it only proves refusal formatting.
- **Conflict:** self-healing, non-blocking, and no local-state refusals. Better error truncation is useful, but this example turns repairable local evidence into the expected terminal product response.
- **Rewrite:** test safe diagnostic bounding separately with a real invalid request/security case. For this scenario, damage the receipt in a prepared installation, invoke the normal install/preparation path, and assert bounded observation/reconstruction from the verified archive, then install progress. If reconstruction is impossible, assert the attempt ends without effects or retained admission holds and a freshly reviewed request is admitted. Never compile unknown image identity into an executable plan.
- **Confidence:** high. **October 8:** no — `13ee810d1`, #816, September 17.

### 5. Malformed uninstall metadata is grouped with genuine unsafe destructive identity

- **Location:** `rust/crates/vonk-agent/tests/runtime_observation.rs:431`, `uninstall_rejects_its_own_invalid_metadata_identity_and_artifact_paths`; assertions at 452–456.
- **Today:** one loop requires `uninstall(...).is_err()` and the installation directory to remain for `malformed`, `identity`, and `artifact-path`. The malformed case writes `{}` into local `spec.json`; all cases stop at rejection.
- **Conflict:** damaged bookkeeping must reconcile rather than prevent new work. Grouping local parse damage with traversal/changed content encourages one blanket refusal. However, blindly deleting a directory without exact destructive authority would also violate the security principle.
- **Rewrite:** separate these fixtures. For malformed local spec, assert no unproven stop/delete, observe actual containers/current installation using the authorized identity, then assert bounded cleanup or an ended unknown attempt. Submit a fresh valid operation and prove it is not blocked by the damaged record. For traversal or genuinely different content, retain “no unauthorized effects” and assert no escaping file is touched. Neither case should permanently poison unrelated or fresh work.
- **Confidence:** medium: the no-delete assertion is sound; the missing reconciliation/fresh-request boundary is the defect, not permission to delete from `{}`. **October 8:** no — `23d320492`, #754, September 14.

## P2 — diagnostic-driven decisions and incomplete recovery proofs

### 6. New non-blocking proof manufactures the end and admission receipts

- **Location:** `control/tests/test_runtime_image_preparation_scan_recovery.py:85`, `test_unknown_receipt_attempt_ends_without_blocking_fresh_preparation`; callbacks at 104–128.
- **Today:** defines a test-only `AttemptReceipt`. `end` calls a storage reader, pins `WaitReason.RECEIPT_MISSING`, then constructs `FAILED` itself. `fresh` calls `_prepare` directly, then constructs `SUCCEEDED` itself. The hold check acquires a filesystem publication lock; no real queued operation is ended or admitted.
- **Conflict:** non-blocking and eventual consistency must be product behaviour. This test passes even if the production scheduler retains the old busy flag, lease, reservation, or deduplication gate forever. The shared helper is useful, but supplied synthetic receipts cannot prove lifecycle recovery.
- **Rewrite:** create the request through the real availability service; inject missing/unreadable evidence at its storage boundary; drive its worker/clock to actual bounded settlement; reload the persisted operation after restarting the service. Check real owner holds. Submit another request key through the same service and drive it to preparation completion, asserting exact content and no duplicate transfer. Add a superseding request while the first is unknown and show old work cannot win.
- **Confidence:** high. **October 8:** yes — `64a51be0b`, #1276, October 8; the whole test is attributed to that merge.

### 7. Retry policy is tested as membership in a reason-string taxonomy

- **Location:** `control/tests/test_failure_classification.py:32`, `test_other_failures_are_retried`; `:55`, `test_downloaded_byte_integrity_failures_request_redownload`; `:64`, `test_error_code_prefers_typed_code_then_dotted_message_prefix`.
- **Today:** the first test only calls `is_security_failure` on code strings; it never retries anything. The second requires different redownload decisions for named digest/archive/receipt codes. The third explicitly blesses extracting a decision code from a dotted prose prefix. A different implementation that retries forever, or never repairs local receipt damage, can pass all three.
- **Conflict:** decide by behaviour, not reason strings; unknown → observe → reconcile with bounded retries. Transport/receipt state should lead to actual observation/preparation outcomes, not a vocabulary-led control path.
- **Rewrite per test:** (1) inject transient transport and stale local evidence through the real run/profile worker, restore them, and assert bounded retries followed by success/fresh admission; repeat with renamed diagnostics and identical behaviour. (2) substitute downloaded bytes, assert they are never served, replace/reverify through preparation, then succeed; corrupt only the receipt and assert verified reuse/reconstruction instead of unnecessary transfer. (3) test that prose, punctuation, and wrapping do not change authorization or retry decisions; real typed authenticated denial must still cause no effects. Safe diagnostic extraction can remain a presentation test, separate from decisions.
- **Confidence:** high. **October 8:** no — `b9532b537`, #934, September 28 for the cited policy assertions.

### 8. Unreadable receipt/image tests terminate at the explanation

- **Locations:** `control/tests/test_runtime_image_preparation.py:979`, `test_an_unreadable_stored_receipt_names_the_rule_that_rejected_it`; `control/tests/test_library_listing_budget.py:301`, `test_an_unreadable_image_is_named_and_does_not_fail_the_answer`.
- **Today:** the receipt test invokes private `_parse_runtime_image_receipt`, expects an error and pins “runtime image receipt identity” / “runtime_adapter”. The image-index test does isolate a healthy neighbour, but pins `runtime_image.archive_unavailable` and never checks a later lookup after the read recovers.
- **Conflict:** behaviour over reason strings; self-healing. Explaining a validator failure or naming a local I/O problem does not prove a current request can reuse, repair, or retry it.
- **Rewrite per test:** receipt: enter through the preparation/storage service with a damaged stored receipt; verify archive observation reconstructs it and the next request reuses exact content without refusal. Keep strict parser rejection as a small contract test if needed, without asserting that its wording is operator admission policy. Index: clear the probe fault, advance past the bounded refresh interval, assert the same image becomes present and the healthy neighbour stays available; assert no unnecessary repeated probes. Drop exact local reason-code equality.
- **Confidence:** high. **October 8:** no — receipt assertions `13ee810d1` (#816, September 17), with later fixture/parser edits #865/#964; index assertion `1b1ea35dd` (#1089, October 3 Amsterdam).

## P3 — unnecessary implementation and record-keeping constraints

### 9. Recovery guards inspect private source syntax and residue metadata

- **Locations:** `control/tests/test_distributed_recovery_bookkeeping.py:31`, `test_the_recovery_derivation_does_not_raise_a_refusal_for_bookkeeping`; `:53`, `test_a_start_that_is_not_the_exact_accepted_authority_is_retired_with_its_reason`.
- **Today:** the first AST-scans one module for direct calls to `DistributedLifecycleError` inside a whitelist of three function names; helper calls and other exception constructors evade it, and a harmless refactor breaks it. The second calls private `_accepted_start_authority` and pins `Residue.kind`, `.subject`, `BookkeepingReason.ROW_INCOMPLETE`, note text, and structured log metadata; it does not actually retire the run or admit new work.
- **Conflict:** behaviour over private names; no reason taxonomy or record-keeping for its own sake; non-blocking must be observable.
- **Rewrite per test:** first: inject missing/damaged/stale accepted-start state through the worker's public boundary and assert no batch-wide refusal, no unproven Start, bounded reconciliation/end, and healthy sibling progress. Second: drive that damaged run through actual settlement, inspect confirmed effects/hold release, then submit a fresh authorized request. Logs may be safely bounded, but exact residue labels are not the recovery contract.
- **Confidence:** high. **October 8:** no — `47e5d29db`, #1156, October 6.

### 10. Malformed cancellation tests additionally require bookkeeping narration

- **Locations:** `control/tests/test_agent_jobs.py:3562`, `test_claim_admits_and_names_a_malformed_cancel_flag` (3584–3588); `control/tests/test_agent_jobs_postgres.py:1012`, `test_postgres_non_boolean_cancel_flag_does_not_cancel` (1035–1038).
- **Today:** both correctly prove `1`/`"true"` do not cancel the claim, then require a persisted status reason containing `parent-cancel-flag-malformed`; the first also pins the `claim note: ` prefix.
- **Conflict:** the core behaviour is aligned, but mandatory narration steers toward bookkeeping/audit machinery for its own sake. A valid implementation with the same claim/cancellation behaviour fails solely for changing or omitting the note.
- **Rewrite per test:** retain exact claimed operation identity and admission; complete the claim and prove the malformed flag never cancels it. Exercise actual boolean cancellation separately. Remove note/prefix equality; if diagnostics are retained, check safety/bounds independently. Keep the PostgreSQL fixture: it catches real JSON-to-boolean coercion.
- **Confidence:** high for the unnecessary assertions; no criticism of the admission behaviour. **October 8:** no — `7eef9a730`, #834, September 17 (later protocol fixture edits #964).

### 11. Healthy journal recovery requires a particular quarantine filename

- **Location:** `rust/crates/vonk-agent/tests/restart_receipts.rs:314`, `damaged_bookkeeping_is_quarantined_without_blocking_new_claims`; path-prefix assertion at 338–344.
- **Today:** correctly opens recovered state and requires `BeginDecision::Execute`, then insists a sibling file begins `state.sqlite.corrupt-`.
- **Conflict:** behaviour over private file paths; record-keeping is not the product goal. Replacement, bounded discard, or a different safe quarantine naming scheme could provide identical recovery and still fail this assertion.
- **Rewrite:** keep damage variants, recovered admission, and credential isolation; claim, persist a result, reopen, and assert the new result/acknowledgment survives. Verify bounded disk use and no unrelated files are touched. Remove the incidental filename requirement. Interrupted-repair tests can inject storage-boundary failures without making the naming convention the externally required outcome.
- **Confidence:** high. **October 8:** no — `57afa83cc`, #1176, October 7 Amsterdam.

### 12. Removed method names are used as proof that operator blocking is gone

- **Location:** `control/tests/test_agent_operation_lifecycle.py:539`, `test_the_dead_entry_points_are_gone`.
- **Today:** only asserts `AgentJobService` lacks `wait_for_operator` and `uncertain` attributes.
- **Conflict:** private names over outcomes. Blocking can be reintroduced under a new name without failing this test; harmless names can fail it without any operator wait.
- **Rewrite:** queue an operation with uncertain old evidence, drive observation through its bound and service restart, assert actual settlement releases its holds, and require fresh-key admission/healthy sibling progress without operator intervention. This catches the wrong implementation the method-name assertion misses.
- **Confidence:** high. **October 8:** no — `26fce3412`, #1112, October 5.

## Aligned cases checked

- `control/tests/test_run_switch_background_image.py:497` replaces old receipt shapes through the actual background preparation path, including preparation longer than the preflight window. `:531` permits sibling/other-adapter provenance for the same image. These test consumed content and worker progress, not provenance equality.
- `control/tests/test_runtime_image_preparation.py:1086` treats content as identity while allowing different build provenance; `:1134` isolates an unreadable sibling and reuses a healthy match, then demonstrates recovery when the read fault clears.
- `control/tests/test_canonical_cache_build_identity.py:367` reuses a cached file set across a new model revision with identical file content. Mere revision/build ID projection assertions elsewhere are not automatically defects; I found no stronger runtime provenance-equality counterexample than these positive controls.
- `control/tests/test_model_cache_bookkeeping.py:136` ends unreconstructable state and checks real fresh request admission; `:195` reconstructs an envelope from its set; `:214` isolates a damaged manifest. The extra `persisted-state-damaged` assertion at 157 is dispensable, but the end/fresh behavioural check is valuable.
- `control/tests/test_model_cache_split_files.py:284` resumes a completed part after interruption; `:300` reuses identical bytes cached by another model without fetching parts.
- `tests/cluster_profiles/test_cli_update.py:1401` refuses tampered ingress bytes before install, then permits a fresh valid update. This is a real integrity edge and does not permanently poison a new request.
- `rust/crates/vonk-agent/tests/recipe_builder/archives.rs:222` exercises absent/substituted OCI content against a real private store. `rust/crates/vonk-agent/tests/restart_receipts.rs:349` leaves a symlink target untouched. Integrity and path confinement are legitimate protections.
- `rust/crates/vonk-agent/tests/installation_reconciliation.rs:255` fences a replaced directory before destructive finalization. Matching files do not authorize following a swapped filesystem object; I did not classify this inode fence as provenance-equality debt.
- `agent_protocol/tests/test_preflight_finding_codes.py:73` keeps an unknown incoming diagnostic instead of refusing the wire document. Its enum/schema-only neighbours are less valuable than connected behavioural coverage, but no local-state refusal is established by those schema assertions alone.

## Verification boundary

Only this report and `.sol-commit-msg` are written. No local tests, Python/Rust/TypeScript code edits, Git writes, PR, deployment, or runtime qualification. Ruff/format/type checks have no changed source inputs for this documentation-only delivery; no repository-wide passing claim is made. Report whitespace and the artifact paths are checked statically.
