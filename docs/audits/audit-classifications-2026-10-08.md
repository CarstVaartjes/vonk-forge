# Classification audit — 2026-10-08

Audit only, at `bc2277675` (PR #1281). Yardstick: the owner's instructions for this track, including local-state damage as miss/unknown, request supersession, content identity, and fail-closed only at genuine trust/authority boundaries. In particular, retaining bytes during uncertain deletion is necessary; refusing the removal request is not the same behaviour as retaining bytes and reconciling.

Coverage: all **144** `security-edge` / `input-validation` families, **1,399** site records representing **1,640** raise occurrences, in **186** Python paths. Read the written reasons and source conditions/handlers at every named function; all inventory function names resolved. Also inspected all **92** registered retry handlers and their stated reasons, with deeper caller inspection for the findings below. This is static evidence, not a runtime recovery proof. No local tests, remote operations, code changes, or Git writes. The final coverage table distinguishes aligned boundary checks from mixed families; it does not endorse a whole module because one check is valid.

History below uses read-only `git log`, `git show`, and line blame. “Carried today” means the current file/line arrived in a 2026-10-08 split PR; that is **not** evidence the behaviour originated that day. “Older” means the cited behaviour predates today. Classification changes are distinguished from behaviour changes. A lower debt count proves neither recovery nor correctness.

## Findings

### F01. All 19 deletion-scan checks were relabelled, not healed

- **Sites:** `control/src/vonk_control/artifact_reference_scan.py:296,317,355,380,393,399,570,607,635,646,652,775,812,845,857,929,945,989,1030`; `control/src/vonk_control/artifact_lifecycle.py:137`; `control/src/vonk_control/model_cache/removal_review.py:114,293`; `control/src/vonk_control/recipe_image_availability/removal_acceptance.py:108,200`; `control/src/vonk_control/cache_removal_review.py:230`
- **Today:** `ArtifactReferenceUnverified` inherits `SecurityRefusalError`. Missing model-set rows, malformed profile/application/job JSON, absent recipe heads/installations, cumulative 16 MiB scan limits, and mismatched stored image-owner intent all take this path. Review turns them into blockers; `refusing_removal_blockers` retains scan blockers, and removal admission raises before recording pending work. `retryable=True` only advertises retry; it creates no reconciliation.
- **Conflict:** Self-healing, unknown → observe → reconcile, non-blocking. These are local-owner observations, not unverified incoming bytes, identity or authority. The written deletion-only justification changes the category without changing refusal behaviour. One damaged unrelated profile or aggregate scan budget can stop every selected deletion.
- **Required behaviour:** Keep bytes; accept/defer exact authorized removal as unknown, scope uncertainty to the affected owner, re-observe/reconcile with bounded rates and a terminal budget. Paginate owner scans rather than making aggregate size a permanent refusal. Never infer an empty reference set or unlink on uncertainty.
- **Confidence:** high. **2026-10-08 PR history:** Yes: #1273 (`ba2c59c1d`, 2026-10-08) explicitly replaced these 19 raises and added the security subclass; existing scan logic predates it.

### F02. Removal scope and dead-owner bookkeeping still refuse

- **Sites:** `control/src/vonk_control/model_cache/removal_scope.py:46,55,62,178`; `control/src/vonk_control/model_cache/removal_review.py:420,429,436,442,456,462,473,586`; `control/src/vonk_control/model_cache/removal_acceptance.py:182,190,217`; `control/src/vonk_control/artifact_reference_scan.py:193`; `control/src/vonk_control/recipe_image_availability/removal_review.py:452,458,463,469`
- **Today:** Missing manifests, membership disagreement, inconsistent stored lengths, damaged/non-active gate owners, absent immutable child plans, and inconsistent internal asset-status projections become refusal/invalid errors. The acceptance wrapper converts any `ArtifactLifecycleError` into `ModelCacheConflictRefused`, including ordinary uncertainty.
- **Conflict:** Local-state-as-unknown and request-led recovery. A valid new removal is judged by damaged historical gate/manifest state. The broad “destructive owner guard” reason mixes a genuine fence against another executor with unreadable bookkeeping.
- **Required behaviour:** Retain current bytes and actual live-owner fences, reconcile owner effects and rebuild derived membership, then continue the same removal. Return unknown for incomplete projection; do not label it caller-invalid or convert unknown to security refusal.
- **Confidence:** high. **2026-10-08 PR history:** Carried today: #1238 (`6d03bd7d5`) for model-cache paths, not proof of new behaviour. `artifact_reference_scan.py:193` is older #1157 (2026-10-06).

### F03. Enrollment state damage is treated as authentication denial

- **Sites:** `control/src/vonk_control/enrollment.py:556,572,593,783,805,1029,1139,1202`
- **Today:** After CA observation/issuance, non-issuing state, a missing persisted generation, invalid rotation state, missing/different rotation bookkeeping, or conflicting local orphan-revocation evidence raises `EnrollmentDenied`; several messages demand manual recovery.
- **Conflict:** Fail closed only at real authority edges, self-healing, eventual consistency. The verified CA effect may already exist; local state inconsistency is neither a bad CSR signature nor revoked authority.
- **Required behaviour:** Observe the exact CA journal and certificate identity, reconcile local persistence/revocation under its fence, and end stale executors without denying a fresh authorized enrollment/rotation. Keep actual revoked grants/nodes and invalid certificate/CSR identity fail-closed.
- **Confidence:** high. **2026-10-08 PR history:** Older: 556/572 #1198 (`8c773e32e`, 2026-10-07); 783 #652 (`f643c54c0`, 2026-09-10); 1139 `d31b14b2c` (2026-08-04); 1202 `443913945` (2026-08-05). No today-origin evidence.

### F04. A newer valid rotation cannot supersede a different issuing intent

- **Sites:** `control/src/vonk_control/enrollment.py:774,780,1006,1012`
- **Today:** An existing rotation for another CSR/source serial returns “manual recovery” uncertainty from preparation, or `EnrollmentDenied` from claim. The old intent remains the node-wide rotation owner.
- **Conflict:** Request-led and non-blocking. Another authenticated, valid CSR is not inherently unauthorized merely because older local intent differs. Repeating the same checks cannot resolve a stuck old owner.
- **Required behaviour:** Fence the old issuer, observe/revoke its exact effect as necessary, and reconcile toward the latest authorized CSR. Bound old-effect cleanup without substituting a key in an already bound CA request.
- **Confidence:** high. **2026-10-08 PR history:** Older: 780 #652 (`f643c54c0`, 2026-09-10); 1012 `d382b2491` (2026-08-20).

### F05. Response-capacity refusals are not all pre-effect request checks

- **Sites:** `control/src/vonk_control/enrollment.py:530,924`; `control/src/vonk_control/step_ca.py:667,689,696`
- **Today:** Enrollment maps provider response-unrepresentable errors to input-validation after invoking `issue_node`. Step-CA checks include locally configured reader capacity and computed response metadata capacity; the supplied caller CSR is not necessarily invalid.
- **Conflict:** The inventory reason says validation occurs before any effect. The enrollment catches occur after a possible CA effect, and a deficient local reader is kit/configuration state.
- **Required behaviour:** Preflight supported wire capacity before issuance; if outcome may already exist, preserve the exact journal and reconcile it as unknown. Distinguish a truly out-of-contract submitted request from insufficient local response handling.
- **Confidence:** medium. **2026-10-08 PR history:** Older: #1198 (`8c773e32e`, 2026-10-07) introduced the enrollment catch and response capacity guards; not a today-origin PR.

### F06. Local activation damage is called route ingress security

- **Sites:** `control/src/vonk_control/route_runtime.py:479,483,486,522`; `control/src/vonk_control/route_runtime.py:397`
- **Today:** Malformed/noncanonical local `activation.json` and invalid local active route documents raise `RouteRuntimeError`; staged ordinary files with different bytes also raise. The written reasons call JSON parsing “authenticated activation authority” and immutable-stage disagreement security. No external authenticated marker enters these reads.
- **Conflict:** Local-state-as-miss/unknown and trust the kit. Do not consume corrupt route bytes, but syntactic damage in managed publication metadata is not an attacker-authentication verdict. The stage branch combines genuine unsafe paths with recoverable partial-file differences.
- **Required behaviour:** Ignore/quarantine unusable managed metadata, observe supervisor state, regenerate from current authorized intent in a fresh atomic generation. Preserve path/symlink protections and verify bytes before exposing a route. Fresh generation allocation already helps bypass old staging; classify that as recovery, not refusal security.
- **Confidence:** high. **2026-10-08 PR history:** JSON raise changed today in #1280 (`dc51e94f9`); fields/canonical/document/stage checks older (2026-08-05/11). Today reclassification is not a new authentication boundary.

### F07. Accepted-route cache validation is mixed with actual network authority

- **Sites:** `control/src/vonk_control/recipe_routes.py:382,391,394,410,419`; `control/src/vonk_control/recipe_routes.py:1735`
- **Today:** Reuse of a checksum-verified local bundle raises for generation/plan-marker disagreement, ambiguous run ownership, endpoint shape, or inconsistent local LiteLLM policy. An accepted alias collision is labelled input-validation despite occurring while rebuilding from persisted accepted runs.
- **Conflict:** Content identity and local-state-as-miss/unknown. A stale or damaged accepted projection cannot be trusted for reuse; it should not become a refusal or new-caller validation. Actual management-network policy and node revocation are separate genuine authority checks.
- **Required behaviour:** Return a reuse miss/unknown for unusable cached projection, observe exact live run state and republish current desired routes. Resolve accepted alias ownership from latest authorized intent without silently routing to another run.
- **Confidence:** high. **2026-10-08 PR history:** Older: accepted-run checks #1179 (`2a9b6ff10`, 2026-10-06); alias overlap #1045 (2026-10-01).

### F08. Persisted catalog reads are counted as caller validation

- **Sites:** `control/src/vonk_control/catalog_revision_contract.py:195,239,246,253`; `control/src/vonk_control/catalog_entities.py:656`; `control/src/vonk_control/catalog_service.py:570,607`
- **Today:** Readers reject damaged stored projections/documents and local identity/digest disagreements with catalog validation errors. `_rederive_projection` does heal a valid document’s projection, but raises on damaged document/digest evidence. The reason broadly says the caller fixes input.
- **Conflict:** Self-healing and local-state-as-unknown. These inputs came from stored revision rows, not a new catalog submission. A digest disagreement must prevent reuse without poisoning a fresh catalog sync.
- **Required behaviour:** Treat unreadable local revision/projection as unavailable, rebuild projections from verified content, or re-ingest the exact content from the normal authenticated catalog path. Do not bless mismatched bytes; keep actual submitted document validation at ingress.
- **Confidence:** high. **2026-10-08 PR history:** Older: read-projection `6399b5a60` (2026-09-08); rederive #1053 (2026-10-01); service lines older (format-only blame #776). Today #1281 adds other repairs but these checks remain.

### F09. Local Library and Activity projections fail as invalid requests

- **Sites:** `control/src/vonk_control/library_projection.py:216,274,430,444,448,454,459,486`; `control/src/vonk_control/operation_api/diagnostics.py:43,53`; `control/src/vonk_control/operation_api/providers.py:50,54,56,112,119,124,163`; `control/src/vonk_control/model_cache_api.py:344,350,355`
- **Today:** Invalid internal local-state mappings, stored upgrade diagnostics, provider timestamps/node IDs/order/duplicates, or missing cursor configuration raise projection/validation errors. Read results from internal providers are represented as caller faults.
- **Conflict:** Non-blocking, scope local damage to its owner, and no record-keeping for its own sake. One malformed provider row can abort an otherwise useful read; caller filters do not repair it.
- **Required behaviour:** Project the affected value as unknown/unavailable, omit an unusable optional diagnostic or row with truthful partial-result status, and re-observe/rebuild it. Retain authenticated cursor verification; do not emit unsigned cursors or false empty success.
- **Confidence:** high. **2026-10-08 PR history:** Library checks older; operation split carried today #1266 (`cced635f5`). Cursor projection changes today #1281 (`bc2277675`).

### F10. Stored recipe/build availability is not uniformly request shape

- **Sites:** `control/src/vonk_control/install_admission.py:355,366,551`; `control/src/vonk_control/run_admission.py:523,529,536,637,701`; `control/src/vonk_control/availability_production.py:313,385`; `control/src/vonk_control/execution_plan_service.py:305`
- **Today:** Stored missing/unresolved revision/build, stale mapping, absent exact dependencies, unavailable internal runtime projection/memory estimate/endpoint, or no selected build become invalid-plan/value errors. Broad exception handling in availability authority maps compilation failure to malformed runtime.
- **Conflict:** Unknown → observe → reconcile and trust the kit. Pure validation of explicit submitted options is legitimate; an internal receipt or mapping availability observation is not equivalent. The “before effect” reason alone does not establish caller ownership.
- **Required behaviour:** Expose readiness/dependency uncertainty and initiate normal exact preparation/observation; admit pending authorized work with bounded retries where possible. Reject only genuinely malformed options or changed reviewed effects.
- **Confidence:** medium. **2026-10-08 PR history:** Older admission/compiler/availability logic; #1276 today reviewed compiler categories, not evidence of newly introduced missing-build branch (compiler:305 is #964, 2026-09-29).

### F11. Runtime preparation “argument contract” reads persisted receipts

- **Sites:** `control/src/vonk_control/runtime_image_preparation/preparation.py:131,177,447`; `control/src/vonk_control/runtime_image_preparation/storage.py:219`; `control/src/vonk_control/execution_plan_service.py:384,395`
- **Today:** Preparation looks up a source build in SQL, demands a stored runtime receipt, and rejects an unreadable source-build object mapping as input-invalid. Stored image length differing from recorded length raises a refusal. The argument-contract reason explicitly claims nothing is read from stored state.
- **Conflict:** Local-state-as-miss/unknown, self-healing. The reason is factually wrong for the SQL/receipt lookups. A portable receipt must validate before use, but a local derived receipt rejected on read should lead to repair, not blame the caller.
- **Required behaviour:** Missing/unreadable local receipt is a cache miss; re-observe/rederive from verified build/image content or rebuild through the existing worker. Keep incoming content-digest verification; never publish bytes under a mismatched receipt.
- **Confidence:** high. **2026-10-08 PR history:** Carried/modified today: #1276 (`64a51be0b`). That split does not establish original age of each guard.

### F12. Stored model-set collision checks still act as refusals

- **Sites:** `control/src/vonk_control/model_cache/resolution.py:534`; `control/src/vonk_control/model_cache/transfer.py:287`; `control/src/vonk_control/artifact_jobs/storage.py:246`
- **Today:** Stored model manifest digest disagreement and existing set-content disagreement raise refusals. Blob attachment treats an existing SQL row with different stored size/key as a “content-addressed collision” without proving that incoming verified bytes conflict.
- **Conflict:** Local-state-as-miss/unknown. A corrupted SQL manifest/blob row is not evidence of a cryptographic collision or hostile incoming upload.
- **Required behaviour:** Reconcile/rebuild metadata from verified content identities, replace unusable local records under the writer fence, and continue the current request. Refuse only when verified incoming bytes actually disagree with their bound digest/authority.
- **Confidence:** high. **2026-10-08 PR history:** Carried today #1238 for model-cache; #1266 for artifact jobs. Behaviour-origin not proven by those splits.

### F13. Damaged artifact contracts close transfers and invalidate results

- **Sites:** `control/src/vonk_control/artifact_jobs/outputs.py:155,219,406`; `control/src/vonk_control/artifact_jobs/service.py:309`; `control/src/vonk_control/artifact_jobs/inputs.py:171`
- **Today:** A `Residue` compiled contract closes an otherwise authorized transfer via `ArtifactJobTransferClosedError`; unreadable contract makes a successful result caller-invalid, and damaged input manifest is “incomplete input”.
- **Conflict:** Local-state-as-unknown and trust the kit. Closing transfer because authority truly ended is valid; closing it because stored contract metadata cannot be parsed is a different condition.
- **Required behaviour:** Rederive the exact compiled contract from current accepted recipe/run content, or observe/end this damaged attempt as unknown while releasing its reservations. Keep valid agent/result identity and actual declared output limits enforced.
- **Confidence:** high. **2026-10-08 PR history:** Carried today #1266 (`cced635f5`); not evidence of new guard behaviour.

### F14. A damaged old artifact receipt blocks every new job on the run

- **Sites:** `control/src/vonk_control/artifact_jobs/service.py:409,424`
- **Today:** Submit scans all prior jobs on the run and rejects whenever a prior effect is unknown or its issued result evidence is damaged, even when it is not live. This is classified caller conflict.
- **Conflict:** Non-blocking and request-led. Terminal/damaged historical bookkeeping can veto a new valid operation; no reconciliation or deadline is initiated in this rejection branch.
- **Required behaviour:** Accept latest intent pending exact live-effect observation, reconcile/stop genuinely surviving effects within a bounded budget, retire unusable history, and release only confirmed claims. Do not rerun an uncertain destructive user job automatically.
- **Confidence:** high. **2026-10-08 PR history:** Carried today #1266 (`cced635f5`).

### F15. Fleet worker state is classified as submit-time validation

- **Sites:** `control/src/vonk_control/fleet_profiles/application_projection.py:401`; `control/src/vonk_control/fleet_profiles/assessment.py:344,369`; `control/src/vonk_control/fleet_profiles/run_switch_adapter.py:476`; `control/src/vonk_control/fleet_profiles/pending_admission.py:99`
- **Today:** Persisted application intent mismatch, internal planner residue/scope mismatch, preparation assessment scope drift, and an internally serialized worker clock cutoff raise invalid errors. None is a supplied caller request at these sites.
- **Conflict:** Local-state-as-unknown, behaviour over type names. Current accepted selection/consent fences are legitimate, but damaged internal projection and a TypeAdapter invariant are not caller validation.
- **Required behaviour:** Re-observe/rebuild the exact accepted projection or settle the damaged attempt without blocking fresh intent; treat worker/serialization defects as internal uncertainty. Preserve true newer-selection and changed-review checks.
- **Confidence:** high. **2026-10-08 PR history:** Carried today #1248 (`73b4e9b0f`); original guard age not established.

### F16. Recipe submission wrappers turn kit/storage failures into invalid requests

- **Sites:** `control/src/vonk_control/recipe_operations/install.py:88,120,127`; `control/src/vonk_control/recipe_operations/installation_preparation.py:139,156,166,170`; `control/src/vonk_control/recipe_operations/start.py:197`; `control/src/vonk_control/recipe_operations/job_activation.py:162`
- **Today:** Broad `RuntimeError`/`ValueError` catches around internal receipt refresh/admission and stored-plan parsing become `RecipeRequestInvalid`. Installation preparation checks its own just-written persisted plan after admission/reservation within a transaction, yet is classified validation before effects.
- **Conflict:** Trust the kit, unknown recovery, and the promised pre-effect boundary. Transaction rollback may contain the effect, but does not make an internal persistence failure a caller-input defect.
- **Required behaviour:** Preserve security and unknown error types across wrappers; roll back failed persistence and re-observe/reprepare on bounded retries. Validate genuine request shape before admission.
- **Confidence:** high. **2026-10-08 PR history:** Carried today #1255 (`f6c22d090`).

### F17. Old build cancellation refuses a fresh build

- **Sites:** `control/src/vonk_control/recipe_operations/build.py:305,393`; `control/src/vonk_control/recipe_operations/build_cancellation.py:188,308`
- **Today:** An older cancel awaiting cleanup refuses build submission as NOT_READY. Build-consumer parse/ownership errors during cancellation become invalid requests. The outer build retry catches unknown errors, not these invalid errors.
- **Conflict:** Request-led and non-blocking. Older incomplete cleanup/damaged consumer bookkeeping must be reconciled, not turn a valid new build into invalid input.
- **Required behaviour:** Queue the latest exact build behind bounded old-effect cleanup, reconcile consumers, and resume automatically after confirmed release. Keep cancellation of a truly shared authorized build scoped to its actual current consumers.
- **Confidence:** high. **2026-10-08 PR history:** Carried today #1255 (`f6c22d090`).

### F18. Update batches reject stored damage and compare child provenance

- **Sites:** `control/src/vonk_control/recipe_update_batches.py:126,132,141,904,944`
- **Today:** Malformed persisted update document/hash/cycle becomes nonretryable invalid; claim rejects that batch. A child receipt with the right request/content but a different operation row ID also becomes nonretryable invalid. Invalid observed stored evidence ends the child FAILED.
- **Conflict:** Local-state-as-unknown and content identity. Ending a damaged attempt without executing it is allowed; classifying it caller-invalid is not. A row ID alone does not prove different bytes/effects.
- **Required behaviour:** End/reconcile damaged parent state as unknown, retain/observe independently issued children, and let fresh batch intent proceed. Match the content/request/effect binding; use row IDs only for executor fencing, not artifact acceptance.
- **Confidence:** high. **2026-10-08 PR history:** Older guard lines #1157 (`c3950ef4b`, 2026-10-06). Today retry claims do not heal these nonretryable branches.

### F19. Run/Switch still rejects equal content from another build row

- **Sites:** `control/src/vonk_control/run_switch_operations/run_planning.py:300`; `control/src/vonk_control/run_switch_operations/artifact_validation.py:153,169`
- **Today:** Profile preview compares selected `recipe_build_id` with accepted image `build_id`; artifact verification compares `verified_build_id` with the plan build ID. A different row is conflict/retry even if image digest and build input content are identical.
- **Conflict:** Content identity, not provenance. Retrying the same immutable ID comparison cannot establish equality after equivalent content is rebuilt/reindexed.
- **Required behaviour:** Compare build-input digest and exact image content identity (plus actual required architecture/interface/archive bytes), and preserve accepted effect/authority fencing separately. Do not require a new profile load for equal content.
- **Confidence:** high. **2026-10-08 PR history:** Carried today #1252 (`288fc1f25`); split is not proof the comparisons are new.

### F20. Exact Stop checks mix real consent with damaged local bookkeeping

- **Sites:** `control/src/vonk_control/profile_stop_authority.py:308,359,369,426`; `control/src/vonk_control/recipe_operations/stop.py:299`; `control/src/vonk_control/recipe_operations/stop_acceptance.py:74,114,146,180,206,235`; `control/src/vonk_control/host_runtime_plan_authority.py:778,805`
- **Today:** Unreadable stored Stop/profile progress, malformed accepted document, missing installation/phase/parent, or local child-manifest disagreement raises security refusal. Genuine wrong target, newer selected intent, or changed immutable command shares the same family.
- **Conflict:** Local-state-as-unknown and non-blocking. Do not issue an unverified Stop, but missing/damaged evidence must start observation/reconciliation rather than become an authorization verdict. Repeated reread of the same malformed record is not repair.
- **Required behaviour:** Reconstruct exact current targets from verified accepted command and live runtime ownership, fence stale executors, and bound observation/cleanup. Keep explicit unreviewed Stop targets and actual superseded authority refused; do not broaden consent.
- **Confidence:** high. **2026-10-08 PR history:** Profile guards older (#1154/#1214); recipe Stop carried today #1255; host-authority guard is older. No claim that all were introduced today.

### F21. Stop authorization depends on an English reason string

- **Sites:** `control/src/vonk_control/profile_stop_authority.py:842,844`; `control/src/vonk_control/recipe_operations/stop_dispatch.py:443`
- **Today:** Profile JobRun Stop requires stored cancellation `reason == "superseded by newer workload intent"` alongside actual request ID/actor/cancel flag. Recovery dispatch also chooses supersession handling by `str(error) == "workload intent was superseded"`.
- **Conflict:** Decide by behaviour, not reason strings; not an auditing tool. A harmless wording change or damaged diagnostic can deny exact authorized Stop or change recovery handling.
- **Required behaviour:** Authorize by canonical cancellation/request/intent/effect identity and current ownership. Preserve human wording solely for explanation; derive recovery behaviour from authoritative state and typed disposition.
- **Confidence:** high. **2026-10-08 PR history:** Reason guard older #1214 (`0f05e8fc2`, 2026-10-07); dispatch carried today #1255.

### F22. The image receipt scan retry reason contradicts its code

- **Sites:** `control/src/vonk_control/runtime_image_preparation/storage.py:433,447`; `control/src/vonk_control/runtime_image_preparation/storage.py:356,394`
- **Today:** `_iter_receipts` records an unreadable receipt when corresponding image bytes exist, then raises it after scanning. Thus an unrelated unreadable receipt aborts an otherwise ordinary `find_verified`/`find_build` miss. `find_build` has no registered catch body; its alleged retry is delegation to a caller. The written scan reason says unavailable candidates are skipped and unrelated misses never wait.
- **Conflict:** Scope failures to owner, non-blocking and honest retry proof. Repeated identical metadata reads do not rebuild the damaged candidate and can block unrelated preparation paths. `_prepare_from_build` does catch unknown for one path; that does not establish the claim for every caller.
- **Required behaviour:** Skip unrelated unusable metadata as a reuse miss; exact request-led preparation should repair/rederive only its needed receipt. Demonstrate the real caller → store → worker recovery path rather than crediting a scan loop.
- **Confidence:** high. **2026-10-08 PR history:** Yes, current implementation/reason arrived today #1276 (`64a51be0b`).

### F23. Gateway mutation “already-retried” does not retain unresolved create intent

- **Sites:** `control/src/vonk_control/gateway_keys.py:301,311,428,445,449,479`
- **Today:** Create makes one mutation and one same-key observation on failure, then returns generic UnknownError. The randomly generated requested secret exists only in the call. If both mutation reply and observation are lost but create succeeded, the next create sees the alias and raises conflict; it cannot recover that secret. Revoke/roll/ensure wrappers similarly end calls rather than retry them. Default maintenance does have a real recurring loop.
- **Conflict:** Request-led eventual consistency and truthful retry evidence. The written reasons candidly allow terminal mutation attempts, which is valid for non-blocking, but that does not prove reconciliation of lost random-key creation.
- **Required behaviour:** Persist/reconnect the authorized mutation identity and secret securely before effects, then observe the same key on bounded retries. A truly new replacement request may supersede/reconcile an old key. Do not blindly replay random key generation.
- **Confidence:** medium. **2026-10-08 PR history:** Yes: wrappers, lost-response observer and alias conflict changed today #1280 (`dc51e94f9`).

### F24. Local transfer damage and rate limits are mislabelled security

- **Sites:** `control/src/vonk_control/model_cache_ranges.py:109,232`; `control/src/vonk_control/model_cache/split_transfer.py:138,161,168`; `control/src/vonk_control/model_cache/github.py:185`; `control/src/vonk_control/model_cache/http.py:94,142`; `control/src/vonk_control/model_cache/download.py:176`
- **Today:** Local retained-prefix/partial-size damage raises RangeResponseError or storage refusal. Provider rate limits and short source responses are also in `model.security-edge`. Some have real resumable failure handling; `_append_part` discards a wrong-size part.
- **Conflict:** Fail closed only at genuine trust edges and local-state-as-unknown. Rate limits/short transfer/local checkpoint loss do not identify unauthorized content. Actual Content-Range mismatch and completed digest mismatch must still prevent trusting the bytes.
- **Required behaviour:** Reset/reconcile damaged local checkpoints, resume partial transfers, and persist provider backoff as unknown. Keep digest verification at publication. Count these as recovery/capacity observations, not security success in the debt inventory.
- **Confidence:** high. **2026-10-08 PR history:** Range guards older #640 (2026-09-09); split/model-cache code carried today #1238. Today #1281 adds real short-range retries; it does not change these other categories.

### F25. Stored OCI parsing is not ingress verification

- **Sites:** `control/src/vonk_control/oci_image_store.py:149,392,408`
- **Today:** `read` parses a local manifest and raises a security-classified store error for damaged JSON or descriptor shape. Missing blobs return a miss and unreadable files return StoreUnknown, so syntax damage is treated differently from equivalent local cache loss.
- **Conflict:** Local-state-as-miss/unknown. The written reason says every OciImageStoreError verifies ingress; these execute on cache reads.
- **Required behaviour:** Return miss/unknown, discard/re-pull damaged managed content by exact image digest, and let the ordinary worker recover. Continue rejecting incoming image references or bytes that fail their actual ingress checks.
- **Confidence:** high. **2026-10-08 PR history:** Older `read` #1084 (`4b6ae3e9f`, 2026-10-02).

### F26. Capacity exhaustion is counted as malformed caller input

- **Sites:** `control/src/vonk_control/artifact_blob_store.py:504,580`
- **Today:** Current accounted stored/reserved/temporary bytes exceeding quota raises `ArtifactBlobQuotaExhausted` with invalid-request limit reason. `_commit` executes after bytes have been streamed and verified, not solely before an effect.
- **Conflict:** Resource shortages wait/reconcile; validation-before-effects. A valid bounded upload competing for temporary storage is not an invalid shape. The quota itself must stay enforced.
- **Required behaviour:** Expose measured storage capacity and a visible resume condition; defer within a bounded attempt, or end this attempt as unknown/unavailable and release its temporary claim so fresh work can proceed after space recovers. Reserve genuine out-of-contract request rejection for declared upload limits.
- **Confidence:** high. **2026-10-08 PR history:** Older typed raises #1157 (`c3950ef4b`, 2026-10-06).

### F27. Step-CA family includes network and health failures, not just identity verification

- **Sites:** `control/src/vonk_control/step_ca.py:327,351,657,741,748`
- **Today:** Unexpected health/revoke status, malformed provider JSON, non-success generic response and HTTP/OSError use StepCAError classified wholesale as security. Enrollment catches many issuance errors and translates them to uncertainty, so this is not proof all network faults reach the operator as security refusal.
- **Conflict:** Behaviour-based classification, unknown observation. A network failure or unavailable provider is not evidence of invalid certificate identity. The broad class-based reason hides this difference.
- **Required behaviour:** Keep exact issuance/revocation observation under bounded retry and classify transport/health uncertainty accordingly. Retain CSR/certificate signature, issuer, public-key, serial and signed journal-binding checks fail-closed.
- **Confidence:** high. **2026-10-08 PR history:** Older provider path `d04d48316` (2026-08-04).

### F28. Presence age is local observation uncertainty, not caller invalidity

- **Sites:** `control/src/vonk_control/presence.py:310,315,317`
- **Today:** Latest presence rejects a changed stored binding, future timestamp or stale last-observed address via PresenceError in input-validation. Active certificate/node and management-network checks are mixed into the same module inventory.
- **Conflict:** Unknown → observe → reconcile. Caller-selected maximum age is valid; expired local evidence needs refresh, not caller repair.
- **Required behaviour:** Re-observe authenticated contact on bounded scheduling and return presence unknown until fresh. Keep revoked/invalid agent identity and forbidden management addresses enforced.
- **Confidence:** high. **2026-10-08 PR history:** Older presence implementation `e48f8c1eb` (2026-08-05); today #1280 does not introduce these age checks.

### F29. Signed package content still has a redundant provenance refusal

- **Sites:** `control/src/vonk_control/recipe_packages/__init__.py:1342,1343`
- **Today:** `_bind_release` refuses the whole index if snapshot.commit differs from release.commit; subsequent per-package checks bind path, package digest and actual asset presence against verified signed checksums.
- **Conflict:** Content identity, not provenance. Signed checksum/package content checks are real ingress edges; comparing source commit strings is not a substitute and can reject identical verified package content from another publication.
- **Required behaviour:** Bind the signed index/package byte digests and expected repository authority. Keep commit metadata informational unless it is an explicit caller-reviewed source constraint; never use it alone as artifact reuse/refusal identity.
- **Confidence:** medium. **2026-10-08 PR history:** Older: #944 (`bc7875f9c`, 2026-09-28) introduced this commit comparison; #1281 carries package work today but did not originate the check.

### F30. Runtime initialization labels ordinary kit/storage failures security

- **Sites:** `control/src/vonk_control/runtime_init.py:127,217`
- **Today:** Public runtime-config staging uses the secret-file helper: generic OSError during atomic staging raises RuntimeSecretError, and an empty shipped runtime-assets inventory raises the same security-classified error. These are public release configuration files, not just signing-key material.
- **Conflict:** Trust the kit, local-state-as-unknown, and genuine-security-only refusal. Storage interruption or missing packaged assets does not establish an unsafe key or unauthorized caller.
- **Required behaviour:** Keep existing verified runtime configuration while recovering/re-staging the current authorized kit on bounded retry. Distinguish actual private-key/path safety from ordinary local file availability; never invent a successful rollout.
- **Confidence:** high. **2026-10-08 PR history:** Older: missing public assets guard `f24716c76` (2026-09-28); staging error `8109d8862` (2026-08-19).

### F31. Failed build bookkeeping refuses later matching verified success

- **Sites:** `control/src/vonk_control/recipe_builds.py:1599`
- **Today:** After validating result digest/size and matching build-input digest, record_success rejects a build row outside planned/building/succeeded, including failed, solely because of stored state. The exact-identity reason explicitly endorses refusal over failed evidence.
- **Conflict:** Eventually consistent and local-state-as-unknown. A prior failure status does not establish different content or revoke current authorized build identity. Late verified completion must be reconciled rather than defeated by bookkeeping state.
- **Required behaviour:** Reconcile the current request/attempt fence and exact build/image content. End truly superseded executors without changing newer intent; accept/recover matching completion under current authority regardless of an old failure label. Preserve rejection of actually different bound content.
- **Confidence:** medium. **2026-10-08 PR history:** Older: failed-state guard #1157 (`c3950ef4b`, 2026-10-06); inventory re-reviewed today in #1265 (`d2334a51c`).

## Checked and aligned

- `auth.py:335` / `CursorCodec.decode`, `browser_auth.py:273,315`, `enrollment_validation.py:29,38,55`, and the certificate signature/issuer/key/serial checks in `step_ca.py:460,469,491–598`: verify incoming identity, authenticated tokens/cursors, active grants and certificate authority. Keep these protections.
- `distribution.py:162,197,911,924,950,997`: safe managed root, actual access denial/revocation, grant node, grant expiry and assigned-object scope. These are real serving/authority boundaries. Stored object unavailability is separately represented; its existing bookkeeping-debt family should not be called fixed.
- `artifact_blob_store.py:179,218,244`, `model_cache/download.py:215`, `source_bundles.py:186,287`, package signed-checksum/member-digest checks: prevent trust/publication of unverified incoming bytes. They do not justify refusing a damaged local receipt forever.
- Route publication `_identity` validates caller UUID/digest syntax; `_activate` validates submitted bytes before staging. Symlink/unsafe-path checks remain valid. `inspect(expected=...)` correctly does not refuse a healthy current activation merely because an old expectation differs.
- Pure compiler/shape boundaries (`compiled_execution_plan`, harnesses, topology placement, interface/runtime adapters, canonical arguments, request options, JSON bounds, settings/configuration constructors) are generally valid where they inspect supplied documents before dispatch. Internal producer defects/availability paths listed above are the exceptions; pure receipt consistency validation alone is not a runtime-recovery proof.
- Explicit submitted request-key reuse with different actor/effects, stale reviewed effects, malformed selectors/digests/cursors, unsupported interfaces and active user-authority checks are legitimate. They should not be expanded to stale local availability or unrelated historical row state.
- Finite admission loops actually retry from fresh transactions in agent result consumption, build observation, install/run acceptance and recipe submit wrappers **for the UnknownOutcomeError paths they catch**. They do not retry InvalidRequestError/SecurityRefusalError misclassifications listed above.
- `catalog_sync.sync` releases `active_slot` through its ending path on unknown source outcomes; `run_automatic_sync` has real bounded-rate recurrence. Wrong configured source repository is a genuine catalog ingress authority check. A caller explicitly reviewing a commit may require that commit before effects; that does not make commit identity an artifact cache key.
- Worker source containment, default-key maintenance, short-range retry with fsynced prefixes, and model-cache transfer failure settlement have real recurrence/partial-progress handling. Bounded expiry/ending is acceptable where new intent stays eligible. Re-observation of unchanged malformed bookkeeping, human-only “retry”, and ending an API call are not interchangeable proofs of automatic repair.

## Family-by-family coverage

Each targeted family appears once below, with its inventory occurrence count. `Boundary` means the listed raise conditions are appropriate at their supplied-input/trust boundary in this static review. `Mixed Fxx` identifies exceptions; it does not recommend weakening the remaining legitimate checks. `Scope` means the family contains internal persisted/producer checks as well as valid request validation, covered by the referenced finding. Retry families are discussed separately above and were not part of the 144-family target count.

| Family | Occurrences | Disposition |
| --- | ---: | --- |
| `agent.input-validation` | 7 | Boundary |
| `artifact-job.security-edge` | 3 | Mixed F13 |
| `model.security-edge` | 64 | Mixed F02, F12, F24 |
| `model.input-validation` | 51 | Boundary |
| `model.artifact-identity` | 27 | Boundary |
| `model.removal-guard` | 4 | Mixed F02 |
| `model.served-file-allowlist` | 2 | Boundary |
| `model.lookup-by-id` | 18 | Mixed F10 |
| `model.request-shape` | 9 | Boundary |
| `runswitch.security-edge` | 4 | Boundary |
| `runswitch.input-validation` | 20 | Mixed F19 |
| `runswitch.submit-time` | 8 | Boundary |
| `auth.input-validation` | 1 | Boundary |
| `auth.security-edge` | 10 | Boundary |
| `bounded_json.input-validation` | 3 | Boundary |
| `browser_auth.security-edge` | 7 | Boundary |
| `catalog_entities.input-validation` | 12 | Mixed F08 |
| `catalog_revision_contract.input-validation` | 5 | Mixed F08 |
| `catalog_service.input-validation` | 18 | Mixed F08 |
| `catalog_sync.input-validation` | 7 | Boundary |
| `cluster_mappings.input-validation` | 17 | Mixed F10 |
| `compiled_execution_plan.input-validation` | 21 | Boundary |
| `contract_graph.input-validation` | 20 | Boundary |
| `distribution.security-edge` | 10 | Boundary |
| `distribution_executor.input-validation` | 1 | Boundary |
| `failure_evidence.input-validation` | 1 | Boundary |
| `fleet_events.input-validation` | 1 | Boundary |
| `fleet_profiles.input-validation` | 83 | Mixed F15 |
| `fleet_profiles.security-edge` | 2 | Boundary |
| `fleet_projection.input-validation` | 1 | Boundary |
| `gateway_keys.input-validation` | 1 | Mixed F23 |
| `canonical.input-validation` | 23 | Boundary |
| `canonical.security-edge` | 1 | Boundary |
| `common.input-validation` | 29 | Boundary |
| `host_helper_authority.security-edge` | 25 | Boundary |
| `host_runtime_plan_authority.security-edge` | 13 | Mixed F20 |
| `install_admission.input-validation` | 14 | Mixed F10 |
| `interface_adapters.input-validation` | 2 | Boundary |
| `jobs.security-edge` | 1 | Boundary |
| `library_projection.input-validation` | 25 | Mixed F09 |
| `litellm.input-validation` | 7 | Boundary |
| `model_cache_api.input-validation` | 3 | Mixed F09 |
| `model_cache_ranges.security-edge` | 6 | Mixed F24 |
| `oci_image_store.security-edge` | 5 | Mixed F25 |
| `operation_api.input-validation` | 16 | Mixed F09 |
| `operator_projection_api.input-validation` | 1 | Boundary |
| `passwords.input-validation` | 1 | Boundary |
| `prebuilt_images.input-validation` | 1 | Boundary |
| `presence.input-validation` | 18 | Mixed F28 |
| `presence.security-edge` | 5 | Boundary |
| `recipe_builds.input-validation` | 13 | Boundary |
| `recipe_execution_contract.input-validation` | 1 | Mixed F16 |
| `recipe_image_availability.input-validation` | 34 | Mixed F10 |
| `recipe_packages.security-edge` | 32 | Mixed F29 |
| `recipe_routes.input-validation` | 2 | Boundary |
| `recipe_runtime_specs.input-validation` | 18 | Mixed F10 |
| `recipe_start_payloads.input-validation` | 3 | Boundary |
| `recipe_stop_payloads.input-validation` | 2 | Boundary |
| `route_runtime.security-edge` | 10 | Mixed F06 |
| `run_admission.input-validation` | 16 | Mixed F10 |
| `runtime_adapters.input-validation` | 3 | Boundary |
| `runtime_image_preparation.security-edge` | 2 | Boundary |
| `runtime_init.security-edge` | 14 | Mixed F30 |
| `settings.input-validation` | 5 | Boundary |
| `settings.security-edge` | 7 | Boundary |
| `source_bundles.input-validation` | 6 | Boundary |
| `source_bundles.security-edge` | 19 | Boundary |
| `source_policy.input-validation` | 1 | Boundary |
| `topology.input-validation` | 4 | Boundary |
| `profile.security-edge` | 3 | Boundary |
| `profile.input-validation` | 29 | Mixed F15 |
| `profile.exact-identity` | 1 | Boundary |
| `recipe.request-validation` | 65 | Mixed F16, F17 |
| `recipe.stop-authority` | 12 | Mixed F20 |
| `recipe_image_availability.removal-guard` | 13 | Mixed F02 |
| `recipe_image_availability.request-shape` | 11 | Boundary |
| `recipe_image_availability.configuration` | 4 | Mixed F10 |
| `recipe_image_availability.authority-document` | 2 | Mixed F10 |
| `recipe_builds.exact-identity` | 5 | Mixed F31 |
| `recipe_builds.recipe-contract` | 6 | Mixed F10 |
| `recipe_builds.capacity-contract` | 1 | Boundary |
| `runtime_image_preparation.exact-image` | 9 | Mixed F11 |
| `runtime_image_preparation.argument-contract` | 18 | Mixed F11 |
| `agent_jobs.input-validation` | 37 | Boundary |
| `agent_jobs.security-edge` | 13 | Boundary |
| `agent_upgrades.input-validation` | 16 | Boundary |
| `artifact_blob_store.security-edge` | 8 | Boundary |
| `artifact_blob_store.input-validation` | 14 | Mixed F26 |
| `artifact_jobs.input-validation` | 98 | Mixed F13, F14 |
| `artifact_jobs.security-edge` | 4 | Mixed F12 |
| `artifact_lifecycle.security-edge` | 1 | Boundary |
| `artifact_lifecycle.input-validation` | 5 | Boundary |
| `artifact_reference_scan.input-validation` | 2 | Mixed F02 |
| `availability_production.input-validation` | 9 | Mixed F10 |
| `distributed_lifecycle.input-validation` | 3 | Boundary |
| `host_runtime_plan_authority.input-validation` | 12 | Mixed F20 |
| `jobs.input-validation` | 16 | Boundary |
| `agent_operation.input-validation` | 1 | Boundary |
| `artifact_job.input-validation` | 1 | Boundary |
| `recipe_operation.input-validation` | 1 | Boundary |
| `model_cache.input-validation` | 23 | Boundary |
| `recipe_build_cancellation.input-validation` | 6 | Mixed F10 |
| `recipe_operation_worker.input-validation` | 1 | Boundary |
| `recipe_update_batches.input-validation` | 16 | Mixed F18 |
| `recipe_update_batches.security-edge` | 1 | Boundary |
| `run_switch_operations.input-validation` | 17 | Boundary |
| `run_switch_operations.security-edge` | 4 | Boundary |
| `runtime_image_preparation.input-validation` | 5 | Mixed F11 |
| `recipe_operations.input-validation` | 14 | Mixed F21 |
| `execution_plan_service.input-validation` | 16 | Mixed F10 |
| `distributed_recovery.input-validation` | 4 | Mixed F10 |
| `fleet_profiles.adoption-authority` | 2 | Boundary |
| `recipe_builds.security-edge` | 4 | Boundary |
| `recipe_routes.accepted-route-authority` | 10 | Mixed F07 |
| `recipe_routes.accepted-alias-uniqueness` | 1 | Mixed F07 |
| `step_ca.security-edge` | 35 | Mixed F27 |
| `enrollment.input-validation` | 2 | Mixed F05 |
| `step_ca.input-validation` | 3 | Mixed F05 |
| `enrollment.security-edge` | 64 | Mixed F03, F04 |
| `recipe.input-validation` | 38 | Mixed F16, F17 |
| `profile_stop_authority.security-edge` | 30 | Mixed F20, F21 |
| `exact-ordinary-jobrun-stop-security-edge` | 15 | Mixed F20 |
| `exact-ordinary-jobrun-stop-input-validation` | 1 | Mixed F20 |
| `recipe_operations.service-stop-review-security-edge` | 23 | Mixed F20 |
| `recipe_operations.service-stop-review-input-validation` | 5 | Mixed F20 |
| `profile_stop_authority.exact-scope-shape` | 2 | Mixed F20 |
| `distribution.input-validation` | 3 | Boundary |
| `oci_image_store.input-validation` | 1 | Boundary |
| `route_runtime.input-validation` | 1 | Boundary |
| `route_runtime.publication-input-shape` | 3 | Boundary |
| `canonical.image-handle-request` | 2 | Boundary |
| `catalog_entities.exact-reference-request` | 1 | Boundary |
| `compiled_execution_plan.receipt-request` | 2 | Boundary |
| `common.projection-request` | 1 | Boundary |
| `artifact_reference_scan.destructive-scope-security` | 19 | Mixed F01 |
| `topology.runtime-request-validation` | 1 | Boundary |
| `route_runtime.pre-effect-validation` | 3 | Boundary |
| `route_runtime.ingress-contract` | 3 | Mixed F06 |
| `route_runtime.lock-path-ingress` | 1 | Boundary |
| `route_runtime.marker-json-ingress` | 1 | Mixed F06 |
| `execution_plan_service.input-validation-reviewed` | 4 | Mixed F10 |
| `execution_plan_service.security-edge-reviewed` | 3 | Mixed F11 |
| `recipe_packages.input-validation` | 2 | Boundary |
| `catalog_sync.security-edge` | 1 | Boundary |

## Validation and limits

Only this audit document and `.sol-commit-msg` were written. No code/generated/schema files changed. Python Ruff/format and type checks have no changed Python input in this documentation-only track; they are not claimed as suite passes. Local tests stayed off. Read-only `git diff --check` and inventory/count/document checks are the applicable verification. No runtime, PostgreSQL, CI, deployment or physical Spark outcome is asserted. The coordinator owns staging, committing and any further checks.
