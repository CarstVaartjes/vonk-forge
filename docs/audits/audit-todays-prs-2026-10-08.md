# Audit of October 8 merged PRs

Audited snapshot: `bc22776753d2dd9b2c15c57d593c25f05dffa027` (supplied `main` and task branch). Source inspection only; **local tests off**. No fetch, Git mutation, code change, deployment, or physical qualification. Findings use current snapshot line numbers. The user's owner principles are the yardstick, including treating damaged local state as miss/unknown rather than an authorization refusal.

The requested bare `git log main --since=2026-10-08` returned no entries in this environment. Explicit Amsterdam calendar boundaries select **46 first-parent PR commits**:

```sh
git log main --first-parent \
  --since='2026-10-08T00:00:00+02:00' \
  --until='2026-10-09T00:00:00+02:00' \
  --format='%h %ad %s' --date=iso
```

The range starts at #1228 and ends at #1281; #1282 is absent from this snapshot. #1242 includes #1230, #1227, #1233, #1236, #1238 and #1239; #1238 also has an earlier standalone landing. Reviewed per-commit diffs, followed relevant callers into current source, and compared extracted Python function bodies across moves to distinguish behavior changes from splits. Generated declarations and moved lines are not evidence of new behavior by themselves. Confidence below is static-source confidence, not a tested reproduction.

## Findings

### F1 — Damaged repair-pending state disables the repair's own deadline

**Location:** `control/src/vonk_control/run_switch_journal_repair.py:706` (also 657 and 804); caller `control/src/vonk_control/run_switch_operations/advance.py:113`.

**Today:** `_pending` directly serializes and validates `row.progress`. A malformed pending document, or the typed `Residue` produced by its ContractJSON reader, raises instead of yielding an observation. The caller runs this before ordinary advancement. Deadline expiry is checked only after `_pending` succeeds, so repeating the same read cannot reach bounded retirement. Cancellation uses the same path.

**Conflict:** self-healing, bounded unknown → observe → reconcile, and non-blocking. The repair's own local bookkeeping can keep the job in its live state indefinitely; merely polling it does not repair it.

**Required behavior:** rederive recoverable pending state from the owning accepted request and available cancellation evidence. If that cannot be done, end the observer as unknown and reconcile its fences; do not manufacture cancellation or restart the deadline on every read.

**Confidence:** high. **October 8 attribution:** newly introduced in integration #1242 (`b1f3a7701`, component #1227); later serialization changes in #1255 do not address this path.

### F2 — Historical repair evidence becomes a veto over current repair

**Location:** `control/src/vonk_control/run_switch_journal_repair.py:648`; evidence contracts `control/src/vonk_control/run_switch_journal_contract.py:63`; table `control/src/vonk_control/journal_models.py:43`.

**Today:** even after current evidence proves a correction, an existing repair-history row must validate and equal the newly built evidence (apart from its timestamp). Damaged history or a different current witness returns `DEFERRED` instead of applying the correction. The implementation adds an append-only table, original/corrected document digests, complete original documents, native witness records, repair-purpose/disposition vocabulary, and separate end records. This is more than a retry clock: historical record consistency controls continuation.

**Conflict:** local records must not block; decide by current behavior/evidence; not an auditing tool. Current verified witnesses should decide whether a measurement can be corrected, independently of an old diagnostic record. The normal two-minute deadline may eventually end this attempt, but does not make the history veto appropriate.

**Required behavior:** repair from current verified evidence, preserving request identity and authority. Treat unusable historical evidence as diagnostic residue. Keep only state needed for execution/cancellation/retry; no append-only evidence equality gate.

**Confidence:** high. **October 8 attribution:** newly introduced by #1242 (`b1f3a7701`, component #1227).

### F3 — A supposedly newer receipt permanently prevents fresh publication

**Location:** `control/src/vonk_control/runtime_image_preparation/storage.py:161`; classifier `control/src/vonk_control/runtime_image_preparation/contracts.py:293`; discard path `storage.py:462`.

**Today:** an unknown/non-integer schema version or an unfamiliar extra field makes a local receipt “possibly newer.” The scan leaves it untouched. `commit` then raises `RECEIPT_CONTRACT_NEWER` even with newly observed image bytes and a current valid replacement receipt. A random damaged extra field is enough to reproduce the predicate. Every fresh preparation for that archive encounters the same veto until someone changes the file or deployment.

**Conflict:** local state is a miss/unknown that self-heals, new requests supersede older state, and one current contract. This is effectively a mixed-version compatibility barrier, not an ingress security check.

**Required behavior:** exclude the unreadable receipt from reuse and regenerate the current receipt under the existing per-artifact lock from verified bytes/current inputs. Fence concurrent writers by ownership, not by speculation about JSON fields.

**Confidence:** high. **October 8 attribution:** retained and moved by #1276 (`64a51be0b`); the veto already exists in the pre-day `55b42bf6e` version. This is a surviving defect, not a newly introduced split regression.

### F4 — Local recorded-size disagreement is still a security refusal

**Location:** `control/src/vonk_control/runtime_image_preparation/storage.py:218`; contrast `storage.py:313`; consumer `storage.py:510` (`_archive_is_present`).

**Today:** `existing_archive` raises `RuntimeImagePreparationRefused` when stored bytes have a different size from a supplied receipt's recorded size. `find_verified`/`find_build` reach that check through `_archive_is_present`; the latter converts unknowns and cache absence into misses, but rethrows this refusal. Meanwhile `build_archive_available` handles the same recorded-size mismatch as a miss.

**Conflict:** fail closed only at real security edges; damaged local receipt state must not become refusal. This size comparison establishes that the receipt cannot prove reuse; it does not establish that newly ingested bytes failed their digest.

**Required behavior:** decline reuse, reconcile the stored image and regenerate metadata or reprepare. Keep refusal for a digest/identity mismatch at fresh ingress, and never launch unverified bytes.

**Confidence:** high. **October 8 attribution:** retained/moved by #1276 (`64a51be0b`); present before October 8. The new `_archive_is_present` unknown handling leaves this refusal intact.

### F5 — One unrelated unreadable receipt can prevent preparing a missing image

**Location:** `control/src/vonk_control/runtime_image_preparation/storage.py:433` and 447; `find_verified` at 356.

**Today:** the full receipt scan remembers a read-unknown for any receipt whose stored image exists and raises it at the end. When requested image A has no receipt, an unreadable receipt for unrelated B prevents the resolver from returning a miss for A. #1276 adds an exact-archive filter for `find_build`, but `find_verified` still scans without one. Neither re-observing B nor the requested operation can recreate A while this path is treated as unresolved lookup uncertainty.

**Conflict:** non-blocking, fault isolation, and self-healing. A damaged index entry with no proven relation to A must not become A's preparation dependency.

**Required behavior:** skip unrelated unprovable records for reuse; allow exact requested preparation. Scope observation to the requested content identity. Actual denied authority remains a separate security decision.

**Confidence:** high. **October 8 attribution:** #1276 (`64a51be0b`) changes the deferred-scan handling and adds only the build filter; the broad deferred-scan design predates the day.

### F6 — Gateway mutation uncertainty loses the identity needed to recover

**Location:** `control/src/vonk_control/gateway_keys.py:445`, 449, 513 and 541; wrappers at 301 and 326.

**Today:** create generates its prospective secret only in memory. If `/key/generate` succeeds remotely but its response and follow-up `/key/info` observation fail, the wrapper returns `UnknownError` and loses that secret/request identity. A fresh create under the same name then hits “already exists.” For roll, a fault after deleting the original alias but before confirming rename returns unknown and loses the new secret. If rename did not happen, the next roll cannot find the original alias and returns scope unknown before examining the temporary alias. If it did happen, the usable secret was still lost to the caller.

**Conflict:** request-led, eventually consistent, reconcile lost responses before retry, and non-blocking. Returning an unknown value is insufficient when the normal path discards the information required to reconcile the effect.

**Required behavior:** retain exact mutation identity securely before effects and reconnect/reconcile the same create or roll through completion. A newer authorized operation must reconcile/adopt or safely replace the prior exact effect without requiring manual revoke to escape it.

**Confidence:** high. **October 8 attribution:** #1280 (`dc51e94f9`) introduces locally generated secrets and immediate read-after-loss recovery, but not durable recovery. The multi-step roll itself is inherited.

### F7 — Catalog review identity still uses publication commit

**Location:** `control/src/vonk_control/catalog_sync.py:249`; content comparison already available in `control/src/vonk_control/recipe_packages/contracts.py` (`_snapshot_content`).

**Today:** an `expected_commit` mismatch ends a reviewed sync with `CatalogSyncRequestInvalid`, even if the library bundle and selected recipe/model content are identical. #1281 changes the failure category while separately fixing `RecipePackageReader.prepare` to compare snapshot content instead of commit.

**Conflict:** content identity, not provenance; newer requests should not be refused because an identical publication has a different revision label.

**Required behavior:** bind reviewed catalog effects/content digests and compare those at acceptance. Require fresh review for changed content/effects; retain commit only as provenance.

**Confidence:** high. **October 8 attribution:** comparison inherited; #1281 (`bc2277675`) explicitly changes its exception from unsettled to invalid request while leaving the provenance predicate intact.

### F8 — Prepared-installation reuse still requires build and mapping provenance

**Location:** `control/src/vonk_control/recipe_operations/installation_preparation.py:187`.

**Today:** prepared candidates must have the same mapping row, mapping generation and recipe-build row, in addition to plan digest. An equivalent rebuild with identical image/build-input content but a new build row is excluded before its readable compiled plan is considered. This forces fresh preparation instead of testing content equivalence.

**Conflict:** content identity and cache reuse. These are provenance selectors in a reuse gate, not physical byte identity. Exact topology/launch semantics and request ownership still need checking.

**Required behavior:** discover candidates by content/build-input and executable plan/topology identity; separately validate current authority and ownership. Do not adopt another operation merely because its image matches.

**Confidence:** medium: static predicate is clear; downstream plan digest may already incorporate provenance, increasing the scope of the identity cleanup. **October 8 attribution:** retained/moved in #1255 (`f6c22d090`), not established as a newly introduced regression.

### F9 — Renewal canary retains a source-revision identity gate

**Location:** `tests/acceptance/test_spark_lifecycle.py:4392`; producer `.github/workflows/installer-publication.yml:1164`.

**Today:** the helper is refused if its manifest source SHA differs from the current checkout, even after binary checksum and the enumerated source-input checks prove the same helper content. #1253 correctly uses the reused candidate package's build digest, but leaves this independent revision requirement in the canary. An identical helper from another source revision cannot be reused.

**Conflict:** content identity, not provenance. Build digest, helper byte digest and actual input digests provide the relevant identity; source SHA alone adds no content distinction.

**Required behavior:** validate the helper's content/input closure and its compatibility with the exact candidate agent. Keep source SHA informational. Retain the checksum, path-safety and candidate-build checks.

**Confidence:** high. **October 8 attribution:** inherited check (blame: `fb7ce52fc`, October 7), still consumed by today's #1253 (`6cdc9d28d`) canary producer fix; not introduced on October 8.

### F10 — “Trust installed CA lifetime” remains bounded by duplicated policy

**Location:** `control/src/vonk_control/step_ca.py:162`; `control/src/vonk_control/ca_issuance_contract.py:50`; acceptance `tests/acceptance/test_spark_lifecycle.py:742` and 4453.

**Today:** #1254 replaces exact 30-day acceptance with an arbitrary 90-second to 30-day range in both provider construction and the issuance binding. A valid installed CA lifetime outside that range is still rejected before CA interaction. The canary separately permits exactly 30 days and asserts both observed lifetimes equal that constant, so it does not establish correctness for the newly accepted shorter configurations.

**Conflict:** trust the kit; fail closed at actual certificate/authentication edges rather than a duplicated local lifetime preference. The comment “only an unrepresentable value is refused” is inaccurate: e.g. 31 days is representable.

**Required behavior:** derive issuance/renewal scheduling from installed CA policy and actual signed certificate times, checking chain, authority, temporal validity and representability. Parameterize the canary from that policy instead of assuming 30 days.

**Confidence:** high for the remaining local range gate; medium for the acceptance gap (the present canary intentionally configures a 30-day fixture, so this is coverage scope, not proof that its normal run fails). **October 8 attribution:** provider/binding range introduced by #1254 (`5faa21104`); exact-lifetime canary checks inherited and retained in #1253/#1242.

### F11 — Capability construction timeout does not bound the constructor or free its owner

**Location:** `control/src/vonk_control/capabilities.py:61`, 71 and 228.

**Today:** recovery awaits `to_thread(attempt_construction)` for 35 seconds, then times out only the await. The constructor thread can continue holding `_lock` indefinitely. Subsequent attempts immediately return at the lock, and dependent operations keep receiving 503. `require_service` also calls construction synchronously, outside the registry's timeout. Therefore the advertised bounded recovery is conditional on every factory/initializer returning; the timeout itself cannot make a wedged constructor recover.

**Conflict:** bounded observation and no old wait blocking fresh operations. Nonblocking acquisition prevents thread pileup but does not release the capability's wedged execution owner.

**Required behavior:** bound blocking construction at its underlying I/O/process boundaries, or isolate it in an executor that can be terminated/fenced. Keep a visible construction deadline and fence late results. Request handling should observe capability state and trigger bounded recovery rather than run an unbounded constructor itself.

**Confidence:** medium: failure follows directly if a factory blocks, but no runtime hang was injected. **October 8 attribution:** new in #1256 (`0bab37c16`).

## Aligned behavior checked

- #1264 repairs damaged stored source archives/metadata using freshly verified ingress bytes; a fresh incoming digest mismatch remains refused. Availability no longer depends on the SQL source receipt existing.
- #1276 compares cached image content independently of build/adapter provenance, reuses exact build-input identities and converts missing archives to misses. Those gains coexist with F3–F5.
- #1281 treats an incomplete cached build receipt as a miss, reconstructs catalog lookup/artifact projections from current documents and ends uncertain sync attempts to release the active slot. It changes package preparation to snapshot-content comparison.
- #1246/#1247 persist in-place checkpoint collection mutations and recover a completed preflight's missing attempt count, allowing accepted installation dispatch.
- #1242 offline Stops retain exact deferred Stop authority, release foreground observation and preserve capacity uncertainty for unconfirmed ranks. Its journal repair normally persists a two-minute deadline and backs off without dispatching replacement work; F1/F2 concern damaged repair bookkeeping/history.
- #1271 bounds standalone clear observers and adopts observed stopped/withdrawn effects after a lost acknowledgement; it deliberately does not certify unreachable targets as stopped.
- #1229 continues accepted whole-fleet intent under platform maintenance authority after the original author's permission changes, while user request boundaries still authorize new intent.
- #1240 enforces actual host-memory protection, targets inspected immutable containers and keeps a fresh workload attempt possible. This protects physical capacity, not an estimate ban.
- #1268 keeps optional inventory probes bounded, returns absent/unknown evidence on failure and leaves mandatory inventory reporting running.
- #1280 releases publication file locks before supervisor acknowledgement, treats I/O absence as unknown and preserves gateway 401/403 as authentication refusals. F6 concerns recovering mutation identity after uncertainty.
- #1273 defers destructive GC/removal when authoritative references cannot be verified and closes the kernel reference-lock descriptor even if unlock fails. Retaining possibly referenced bytes is appropriate; an unreadable scan is not permission to delete them.
- The new capability status/reason fields in #1256 are consumed for service availability and retry visibility; not reported as record-keeping alone. The older `BookkeepingReason` enum predates this day. F2 identifies the newly added historical evidence that actually gates recovery.

## Per-PR coverage

“No additional finding” means none identified from this static diff review, not tested behavioral equivalence. Findings above explicitly distinguish introduction from retained behavior.

| PR | Commit | Diff focus and result |
|---|---|---|
| #1228 | `730694f4c` | Fixed-attempt build observation outside reading sessions; typed receipt plumbing. No additional finding. |
| #1234 | `f400f4a45` | Hermetic Git tests and explicit CI principle-history gate. No product recovery change identified. |
| #1235 | `72ba65822` | Lifecycle report uses current PR body; CI bookkeeping only. |
| #1229 | `b0d9e1ab8` | Platform-owned continuing roster reconciliation; aligned authorization separation. |
| #1232 | `716295cd2` | Availability package extraction plus removal-gate reconciliation and missing/cancelled child endings; not just a move. No additional finding. |
| #1237 | `d286bf7b4` | Proof workflows assert outcomes rather than test counts; no product finding. |
| #1238 | `6d03bd7d5` | Model-cache package extraction, typed inputs, bounded unknown observation; no additional finding. |
| #1241 | `5826e2489` | Retired proof-environment test guards; no product finding. |
| #1240 | `0570f7f06` | Host-memory guard, actual container observation and failure propagation; aligned. |
| #1242 | `b1f3a7701` | Integrated six components, expired renewal authentication and offline Stops; F1/F2. |
| #1243 | `4a5ce72a0` | Thread/test cleanup, CLI interrupt and generator environment setup; no additional finding. |
| #1244 | `72e68122b` | Guard allowances follow moved code; no product behavior change identified. |
| #1245 | `e349adaa9` | Journal contention test scheduling; does not address F1/F2. |
| #1246 | `476ef252a` | Runtime-install handoff and checkpoint serialization; aligned. |
| #1249 | `835fb39ed` | Stored-result reader extraction; no additional finding. |
| #1247 | `de6866675` | Preserve default collection mutations across storage; aligned. |
| #1248 | `73b4e9b0f` | Fleet-profile split, typed vocabulary/readers and residue handling; no additional finding. |
| #1250 | `be7bee8b1` | Repository guards independent of CI area selection; no product finding. |
| #1251 | `f3ad9aa70` | Executor extraction/file-size repair; no additional finding. |
| #1253 | `6cdc9d28d` | Reused candidate package build identity fixed; F9 and retained canary scope in F10. |
| #1252 | `288fc1f25` | Run/Switch split, canonical payload/result parsing, observation as residue; no additional finding. |
| #1254 | `5faa21104` | CA configured lifetime range; F10. |
| #1258 | `b58897869` | VM Cargo serialization and OOM classification in hooks; no product finding. |
| #1255 | `f6c22d090` | Recipe operation split plus typed producer/consumer conversion; retained reuse predicate F8. |
| #1256 | `0bab37c16` | Recovering capability construction and dependency isolation; F11. |
| #1257 | `8f7c6c49c` | Signed CLI release projection/update compatibility and platform rendering; no additional finding. |
| #1259 | `6f5559e07` | Rust executor/helper extraction, exports and current failure-code constants; no additional finding. |
| #1260 | `f87aee8ab` | Checkout-owned VM Cargo targets, bounded conservative cleanup; no product finding. |
| #1261 | `2755ac25e` | Build preview/acceptance re-observe unknowns within admission bounds; no additional finding. |
| #1262 | `de10ad133` | Time-budget regression-proof guards/tests; no product finding. |
| #1263 | `14cbc3594` | Runner steps serialized to preserve event context; no product finding. |
| #1268 | `999129b91` | Bounded optional inventory and NAS route selection; aligned. |
| #1264 | `c84e622ff` | Source/archive and derived receipt healing by content; aligned. |
| #1267 | `74866098d` | Static retry-proof scanner follows callbacks/interfaces; no product finding. |
| #1270 | `3db93296e` | Rust setup/protocol/builder extraction; no new recovery predicate identified. |
| #1271 | `022d372d7` | Bound stuck clears and recognize observed Stop completion; aligned. |
| #1269 | `da534532a` | Rust client/OCI extraction; current HTTP supersession code constant preserves value. No additional finding. |
| #1265 | `d2334a51c` | Planning failure typing and invalid cached builder identity becomes miss; no additional finding. |
| #1272 | `d1fe0e460` | Measured GPU telemetry, typed unavailable observations; no additional finding. |
| #1266 | `cced635f5` | Operation API/artifact jobs/models split; preserves mapper ordering and typed input-file serialization. No additional finding. |
| #1273 | `ba2c59c1d` | Destructive reference proof and lock cleanup; aligned retention boundary. |
| #1274 | `f0800d188` | Rust host-runtime/tests split; no changed identity/retry predicate identified. |
| #1275 | `b356f4cda` | CLI/render/client/fixture split; no new local-state refusal identified. |
| #1280 | `dc51e94f9` | Routes/gateway/presence unknown handling, lost-response observations; F6. |
| #1276 | `64a51be0b` | Runtime-image storage split and execution-plan error categories; F3–F5 survive. |
| #1281 | `bc2277675` | Catalog/package/cache bookkeeping cleanup and content snapshot comparison; F7. |

## Verification boundary

Only this Markdown report and `.sol-commit-msg` are authored outputs. Python/Rust/TypeScript lint, format and type checks are not applicable to these edits; none are claimed to have run or passed. Local tests were not run, as instructed. Git metadata is coordinator-owned and remained unmodified. Coverage ends at the supplied main revision; later October 8 merges require an incremental audit.
