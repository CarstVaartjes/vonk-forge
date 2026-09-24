# CLI implementation status

## Active integration checkpoint — 2026-09-24

This integration batch combines upstream `e31f3fa0` with the current
CLI owners. Upstream's typed build-failure cause and retry policy are preserved
without restoring blocking build observation. The API, removal and update
consumers now derive retryability from the same predicate. The previous
`d24473c9` CI run exposed a timing-sensitive failure-evidence assertion: a
worker slice may expire before scanning. The durable receipt survives and a
later slice collects it; the deterministic regression passes locally. The
combined Controller fast suite passes 2,488 tests with three skips. Generated
clients reproduce without drift and the coordination scanner has zero reviewed
sites. The final affected boundary suite passes 217 tests, including real
PostgreSQL child recovery, update/removal consumers and managed image storage.
Only clean archive absence is retryable; permission and integrity failures
remain terminal. The full type gate has one reviewed exception and no unlisted
errors; pinned lint/format and regenerated supply evidence pass. This batch is
not yet qualified by full CI.

The final W19 audit also found a connected-journey evidence gap: the existing
authoring, load/reconnect and endpoint scenarios use different profiles and
fixtures. A new single-profile journey is in progress through the actual
Profile, application, Run/Switch and publication owners. Its deterministic
agent receipts qualify owner identity and recovery, not runtime execution.
A real pinned CPU model/runtime fixture and an independent human walkthrough
remain required; neither is supplied by synthetic cache-ready assets.

The active candidate is `codex/cli-approved-integration` in
`/private/tmp/vonk-cli-approved-integration`. The latest verified source is
`ab25b1d77708ff998fc103baa4ea03bd359d728a`, including remote main `99fb22f2`,
exact removal reviews, cancellation/removal recovery, the U8 cleanup facilitator,
and current web-call-site receipt recovery. Its 23 applicable PR checks pass.
The earlier verified source checkpoint `c1a92c29` includes upstream `0123eeb4`, byte-bounded library pagination (`02ce6441`),
common build/profile admission locking (`b6be92fe`), durable model/recipe
removal, truthful memory commitments, installed recipe-removal qualification
and the full-CI corrections described below. This is not a merge, publication,
deployment, or physical acceptance claim. Other tasks' checkouts
and the earlier integration snapshot remain preserved.

Library pages now bound the complete JSON response, including facets and the
signed cursor, to 1 MiB. The requested page size survives continuation; an
exact boundary regression catches an oversized response. All 23 focused
pagination checks passed. Admission integration passed the affected recipe,
profile, install and Run/Switch checks, including actual PostgreSQL lock
contention and the current `plan_digest` wire field.

Model removal now persists exact intent and deletion ownership before effects,
revalidates its immutable plan, advances durable checkpoints under the storage
lock, and exposes bounded retries. Shared retained blobs are not fenced for
deletion. The API and CLI bind the exact model revision, and a lost response
reconnects to the original accepted operation. The combined model/API/reference,
installed CLI and admission PostgreSQL suite passed 97 tests. Three additional
PostgreSQL worker tests passed: bounded row-lock retry, progress past held or
not-due owners, and process death after unlink before SQL completion with
reclaimed bytes accounted once on restart. Tests use disposable storage.

Memory accounting retains accepted peak commitments and represents uncertain
resident attribution as an explicit range tied to the exact run and inventory.
Starting/running state does not prove bytes are resident. Estimates remain
warnings; actual capacity, reservations and declared reserves remain enforced.
The integrated planner, profile, Run/Switch and rank-launch checks passed 94
tests, and 15 CLI presentation tests passed. The source-build helper now
subtracts the worst-case outstanding resident-memory range; its regression
failed before the fix. All 28 integrated build-memory/materialization/planner
checks passed after that correction.

Generated clients have been refreshed for the model-removal, recipe-removal
and memory contracts. After updating current consumers, the complete Controller
fast gate passed **2,447 tests, three skips**. The independent root fast lane
passed **1,083 tests, 12 skips and 45 subtests**. An earlier unfiltered root run
was stopped after reaching Linux-only installer checks on macOS; it is not
qualification evidence for that lane. Local TLS-dependent CLI checks passed
with the required fixture access and exact pinned recipe checkout.

The final connected PostgreSQL slice passed six cases, including actual
recipe-plus-model-child settlement with shared blobs, image-worker process
death, and finalization contention for both removal owners. Both finalization
regressions reproduced their old failure before correction. The broader
removal/API/reference slice passed 85 tests before those final cases. The
installed model-removal test passes against the actual wheel/TLS/API/PG/storage
boundary. The recipe CLI's lost-response case reproduced the raw-POST defect;
its shared submission helper now passes all 95 CLI module tests. The installed
recipe-removal test also passes against a real wheel, HTTPS peer, PostgreSQL,
API and managed storage: a lost accepted response and a new CLI process both
recover the same owner, and only the worker's completed effects report success.

The first full PR CI run also exercised Linux and PostgreSQL lanes beyond the
local fast tier. It exposed a missing Zsh dependency, strict request-UUID
fixtures, old removal-as-cancellation assumptions, a subprocess preparer that
omitted the publication fence callback, and stale post-stop inventory fixtures.
Those consumers are corrected without relaxing authority or reference checks.
A production regression also escaped nested `AdmissionLockBusy` as a generic
503; the shared admission boundary now rolls back and reports its existing
conflict response, and the same request can retry after the held lock clears.
Real PostgreSQL and Rust/Python wire reproductions pass after correction.
The combined Linux acceptance gate now passes at
`c1a92c29190c9e35ce1fd45079042c05a9a06023`:
[full CI](https://github.com/CarstVaartjes/vonk-forge/actions/runs/35987876492),
[ARM64 capsule recovery](https://github.com/CarstVaartjes/vonk-forge/actions/runs/35987875429),
and [ARM64 signed helper](https://github.com/CarstVaartjes/vonk-forge/actions/runs/35987875459).
All six complete repository/Controller shards, wire contracts, Rust integration,
Compose, generated clients and supply-chain checks passed. Publication jobs were
skipped as expected for a PR; these results do not establish an accepted release.

The six opt-in disposable walkthrough smoke scenarios also pass together at
that revision: first connection/discovery/authoring, whole-Fleet review,
observation/reconnect, cancellation, results, and sequential upgrade. The six
interactive counterparts were intentionally skipped in smoke mode. This is
installed-wheel/HTTPS/PostgreSQL facilitator evidence, not a human scorecard.
The U2 image is still a verified empty fixture and does not prove engine startup
or a runnable model.

The web has 164 passing tests and a successful production build. The full
Python type gate reports one existing reviewed exception and no new errors.
Wire generation, pinned lint and the coordination scanner pass; the scanner
has zero reviewed sites. It does not replace the recorded PostgreSQL tests.

The follow-up review increment now implements W17's Controller-owned impact
projection and digest-bound consent. Model and recipe reviews expose exact
assets, verified/partial/unknown storage observations, retained shared owners,
active work and blockers. Acceptance rejects changed effects and same-key
replay recovers stored intent before mutable catalog resolution. Failed
reference scans remain visible blockers and can recover after the fault clears.
The CLI preserves typed review failures and refuses blocked work before consent.
Generated API clients and existing web consumers are updated together. The
existing web callers validate initial receipts against submitted intent and
retain the exact request key after a lost or timed-out response, reconciling
through a read-only lookup before any same-key retry. All 173 web tests and the
production build pass on the integrated follow-up.

Local integrated validation for that follow-up passes **2,477 Controller fast
tests, three skips**, plus **1,087 standalone fast tests, 12 skips and 45
subtests**. The 14 local-listener cases in the standalone lane required the
normal sandbox exception for loopback HTTP/HTTPS. The subsequent CLI error-boundary
fix passed all 95 standalone CLI tests and the focused Controller consumer set.
Thirteen connected installed CLI/PostgreSQL/storage cases pass, including
same-key recovery, shared references, scan-failure recovery, changed owner
refusal, process death, finalization contention and pending cancellation versus
removal. The separate U8 cleanup smoke also passes through the actual installed
wheel and owners; its interactive counterpart is deliberately skipped. It
shows queued and partial states before terminal effects and retains the shared
object. These automated checks do not fill in a human scorecard.

The complete follow-up CI now passes at
`ab25b1d77708ff998fc103baa4ea03bd359d728a`:
[full CI](https://github.com/CarstVaartjes/vonk-forge/actions/runs/35996952642),
[ARM64 capsule recovery](https://github.com/CarstVaartjes/vonk-forge/actions/runs/35996952218),
and [ARM64 signed helper](https://github.com/CarstVaartjes/vonk-forge/actions/runs/35996952272).
All six full repository/Controller shards, wire contracts, Rust integration,
Compose, generated clients, web and supply-chain checks pass. Generated clients
also reproduce without local drift, the full type gate has no unlisted errors,
and the coordination scanner retains zero reviewed sites. These results qualify
source and the recorded disposable fixtures, not a published release.

The remaining handoff work is U2's real runnable candidate, the same-profile
author-to-endpoint journey, and the independent operator walkthrough. The
schema-changing source PR also needs an explicit
operator merge decision. The user will run every model; that remains separate
physical evidence. No usability study, live cache removal, Controller deployment,
or model campaign is claimed here. Component worktrees remain preserved while
the integrated PR is unmerged.

## Current authorization and integration base — 2026-09-24

The user selected **warnings for uncertain memory estimates** and explicitly
approved admission-locking and crash-safe cache-removal implementation. Actual
capacity, reservation, declared reserve, authorization and exact-plan limits
remain enforced. Earlier unanswered-policy and insufficient-authorization notes
below are historical; they no longer block this repository work. The user also
plans to run every model. Model acceptance does not replace the independent CLI
usability scorecard, which remains unrun.

The initial preservation checkpoint established `codex/cli-approved-integration`
at `/private/tmp/vonk-cli-approved-integration`, combining preserved snapshot
`338354bb4c1e6b6913e86d32909538e836ba0c0e` with fetched `origin/main` at
`93c3746db4fe0af5b20e6c37a14be5e99e5c3669`. Only generated supply-chain files
conflicted; they were regenerated from the combined inputs. The original
`codex/cli-integration` worktree remains preserved. Branch
`codex/cli-preserved-338354bb` protects the immutable snapshot. A filesystem
comparison found all 1,813 snapshot files present in the original worktree;
its only subsequent change was the implementation-plan status correction.

The baseline descriptions below identify earlier checkpoints. New fixes will
use the explicitly coordinated refreshed integration base, with separate
worktree ownership and connected validation before integration.

The earlier isolated `codex/cli-integration` candidate is retained as history.
The active branch is named in the current checkpoint above. The initial `codex/cli-operator-experience` checkout and its unrelated dirty work
remain preserved. The full objective remains the
[twenty-package implementation plan](cli-operator-implementation.md).
This record distinguishes implemented pieces from completed packages; nothing
below claims deployment, hardware acceptance, or complete web parity.

The user authorized implementation and routine decisions, including necessary
fresh-schema changes, and confirmed there is no active production environment.
Existing unrelated `.tmp/` files and `docs/handover-spark-canary.md` are preserved.

## Package state

The rows distinguish repository/installed evidence from the final operator
handoff. The current continuation implements W17's pre-consent removal review,
qualifies W12's combined cancellation/removal race and adds W19's cleanup
facilitator. Their local combined checks and full PR CI pass at `ab25b1d7`.
W09's admission and durable asset-ownership implementation and this
continuation have passing combined CI evidence at `ab25b1d7`. W19 also retains a genuine runnable U2 candidate and an independent
human scorecard. Passing an automated scenario does not fill that scorecard.

| Package | State | Current evidence / remaining work |
| --- | --- | --- |
| W00 | Current inventory and reproducible baseline recorded | Current command/owner and web-boundary maps below; snapshot, environment, commands and logs are recorded in the current baseline. The historical 75-test summary lacks its original invocation and is not independent qualification evidence. Node-profile is implemented under its node owner. |
| W01 | Repository and installed process criteria proven | Contextual exits, human streams, JSON, explicit profile selection, finite timing, no-input, Ctrl-C and pipe handling. Consent now covers the delivered consequential workflows; the independent operator walkthrough remains W19. |
| W02 | Repository and installed presentation criteria proven | Explicit Fleet/Library/Profile/review/application/job/error views; complete identifiers, truthful unknown/zero values, adaptive widths, ASCII-safe text, and append-only resource snapshots. Source tests cover the delivered views; installed terminal checks qualify the common entry point. Artifact-specific views now have entrypoint and installed-process coverage. W02 calls for targeted width fixtures, not an exhaustive installed view-by-width cross-product. Artifact jobs now use their parsed subcommand to show exact job identity, file/path information, empty-result state and the correct reconnect command. |
| W03 | Repository and installed criteria proven | Offline orientation/help/version, parser-derived Bash/Zsh completion, connection check, nonblocking rejection of nonregular credential files. Operator walkthrough remains W19. |
| W04 | Repository and installed criteria proven | Exclusive private grant file, original request identity, owner-authorized status/revoke, and lost-response/disk-failure recovery. Installed re-enrollment now checks exact identity and consequence before consent, decline/EOF/no-input without mutation, and canonical target/private grant delivery. Enrollment/consumption authority remains server-owned. |
| W05 | Source/owner and installed selection criteria proven | Complete bounded selection, canonical identity priority, typed ambiguity candidates, catalog-change cursor refusal, read-only owner-derived fleet-fit/cache assessments, and grouped distributed placement display. |
| W06 | Repository and installed observation criteria proven | Exact profile ID pinning; profile request/application selectors; noun-owned model/recipe/fleet-job progress; bounded sleep/request budget; distinct timeout/interruption documents and reconnect commands. Later workflow receipts, timeout/newer-application pinning, process restart, exact logs and resume now have connected and installed coverage. |
| W07 | Repository and installed authoring criteria proven | Canonical definition read, preserving edits/configure, installed/running intent, private export and bounded import; stale and concurrent writes refused. |
| W08 | Source/storage-owner and installed workflow criteria proven | Original-key recovery, HTTPS deadlines, durable update parents, process restart adoption, and real storage recovery are verified. Busy model/image writers release execution slots and reschedule without consuming transfer retries; eligible work remains reachable. Installed Find-and-prepare and combined gates pass; the unassisted journey remains W19. |
| W09 | Repository and installed admission/ownership criteria proven at `ab25b1d7` | W09d evidence below adds one effect projection, PostgreSQL workload/capacity fences, physical-pool memory accounting, durable disk/port/memory claims and child handoff. Exact preparation builds inherit parent memory, preserve declared reserves and reconnect before mutable capacity checks. Build cancellation now belongs to the exact attempt, preserving valid requests and verified images; new intent can proceed after cleanup without reviving cancellation. Installed intent no longer succeeds with an empty queue. Fresh post-stop checks, exact rebuilt-image identity and declared runtime reserve/pool propagation have recorded evidence. Retained-memory policy, common lock order and durable shared reference/removal ownership now have combined PostgreSQL/process and full-CI evidence. W17 adds the remaining operator-facing pre-consent removal review. |
| W10 | Repository and installed review criteria proven | Interactive review, explicit scripted digest/consent, capacity display and bounded original-key recovery are integrated. Typed stale refusal triggers one current review without automatic resubmission. Representative installed whole-fleet review now covers affected and idle Sparks, exact asset reuse, replacement effects, interruption and per-node capacity reasons. Shared admission has combined evidence at `c1a92c29`. |
| W11 | Repository and installed criteria proven | Durable model cancellation, exact-key recovery, publication fencing, shared transfer/verification settlement and process recovery pass connected and installed tests plus current combined gates. |
| W12 | Owner settlement and connected cancellation/removal criteria proven | Recipe cancellation commits exact model-child intent before signalling and waits for active writers; restart and shared-consumer PostgreSQL checks pass. The connected multi-recipe cancellation case now proves that a restarted parent does not admit later children. The connected update-parent/shared-consumer case preserves exact partial bytes and completes the unrelated consumer. The real PostgreSQL/storage cancellation/removal race now passes locally: pending cancellation blocks removal, a restarted parent settles its child, a fresh review changes digest, and subsequent cleanup retains shared bytes. |
| W13 | Repository and connected cancellation criteria proven | Explicit cancellation retains claims until issued-effect reconciliation; typed progress is its authority. One due cancellation is observed separately from ordinary advancement, preserving parked recovery. Exact-key receipt recovery and installed cancellation/Activity visibility pass; partial multi-target stop, real worker process death and newer-intent ownership now pass connected PostgreSQL tests. The newer-intent case now settles the exact late agent receipt without reviving the old application or changing the replacement ordinal. |
| W14 | Repository and installed criteria proven | Operation/job/audit history has owner-bound filters and keyset pagination. Resume checks complete target intent and revocation; cancellation projects exact owner/effects and uses one pending-state predicate for display/filtering. Existing bounded retry/retirement remains intact. Connected and installed resume/Activity checks and current combined gates pass. |
| W15 | Repository and installed criteria proven | Profile-scoped endpoint projection binds loaded assignment, exact run and route generation. Connected PostgreSQL qualification covers installed-only, foreign-profile alias refusal, expiry and generation replacement through the real profile intent owner. Installed output verifies expiry and the credential-free usage example; withdrawal is observed through the installed client. |
| W16 | Repository and installed criteria proven | Draft/upload/submit/detail/cancel/verified-download and request recovery pass CLI/API, real byte-route and installed checks. Exact named output identity, atomic verified publication and partial recovery are covered; current combined gates pass. Optional-output recipes now accept a canonical empty manifest; required-output minima are enforced both during result consumption and successful response validation. Installed human and JSON checks distinguish queued/unavailable, empty success and required-output failure, and show verified local files. Definitive refusals now offer read-only inspection or a new review; ambiguous and already-accepted requests retain exact recovery guidance. |
| W17 | Durable removal, pre-consent impact review and maintenance criteria proven | Upgrade/removal consent, one-at-a-time maintenance, exact-key replay and durable following pass focused checks. Stored recipe-removal replays now bind exact target, actor and model-retention choice with strict receipt validation. Concurrent first-submission ownership and durable pre-effect intent now pass connected and installed checks. The Controller now exposes exact pre-consent impact, current clients bind its digest, changed scope is refused, and installed replay and cleanup checks pass locally. |
| W18 | Own repository and installed criteria proven; dependent delivery remains open | Real entry-point, shell, signed updater, terminal contexts and installed runbook/parser checks pass. Actual wheel/TLS/PostgreSQL tests cover delivered workflows. Combined source, generated contract, wire, build and supply-chain checks now pass. W19 remains required for the full handoff. |
| W19 | In progress | Installed load, authoring, consent and closed-output acceptance exercise actual services/PostgreSQL; endpoint, connection, artifact-job, recipe cancellation and profile cancellation/Activity journeys also pass; interrupted update-batch, Find-and-prepare, sequential-upgrade and exact-resume acceptance pass. Current combined source/installed gates pass. The connected cancellation/removal case and U8 cleanup smoke now pass. A real runnable U2 candidate and the independent operator walkthrough still block handoff. No deployed or physical claim. |

## Historical checkpoints

The following entries preserve earlier source snapshots and findings. Their
remaining-work statements are historical; the active checkpoint and package
table above own current status.

The six GPT-6 Luna agents at maximum reasoning are reused for bounded work.
Memory-kind, enrollment refusal, lifecycle consumer, packaging, re-enrollment,
whole-fleet review, partial/process/newer-intent profile cancellation,
shared-consumer update cancellation, endpoint human output and the walkthrough
protocol are integrated and checked. The disposable U1/U3 and observer-only U5
facilitators, verified U2 cache-readiness setup and actual U8 JSON pipeline now
pass. Definitive-refusal guidance is qualified. U4, U6, U7 and the U8 upgrade
facilitators are integrated with passing connected smoke evidence. The runnable
U2 prerequisite and U8 cleanup remain open. W09 policy, lock-order and removal
remain separately open. This audit does not add scope.
Integrate incremental feature patches only, preserving current upstream and the
other agents' changes. The base checkpoint for the latest facilitator integration is
`9ca56aff7b2e49944c88bcc23b2a7a3eea53b7f3`; the U6 cancellation and U8 upgrade
increments are recorded below. The local current snapshot pointer is
`/private/tmp/vonk-cli-current-snapshot.txt`.
Actual-output examples retain their exact earlier source `872d639a`. Active facilitator
increments are based on `bf323989`; earlier increments used `324d5c48` plus the
`7f5b74bb` facilitator prerequisite. Older
patches may still name `b5fb8354`, `fff2634b`, `383712fc`, `57bea464`, `802bab02` or `a25f3f17`. These snapshots record source,
not completion or deployment evidence.

The integration candidate is on `codex/cli-integration`. Snapshot `a1ce4439`
preserves the original shared CLI tree, followed by a pending genuine merge
with `origin/main` (`9e2e166b`). All textual conflicts are now resolved, including
the regenerated lock and supply-chain files. The merge is not committed yet.
The shared source checkout and unrelated work remain preserved.

### U6 cancellation facilitator integrated — 2026-09-24

Patch `/private/tmp/vonk-cli-u6-facilitator.patch`, based on `bf323989`, adds
the disposable cancellation launcher. The parent added its
[facilitator guide](cli-cancellation-walkthrough.md). The registered API and
PostgreSQL owners expose an actual blocked recipe preparation action, completed
stop, issued start and unissued later effect. Cancellation survives worker
process death, retains exact identities and reconciles the outstanding agent
receipt. A fresh installed CLI read verifies terminal cancellation and effect
history; the real managed model cache retains reusable verified bytes.

The agent's PTY run issued cancellation, kept the shell open through worker
restart and queried terminal progress from that shell. Independent read-only
review found no remaining blocking mismatch. Both cache and HTTP client close
on normal completion and setup exceptions. Execution and image prerequisites
remain explicit deterministic fixtures; no physical runtime/model claim is made.

Parent integration validation with U8 upgrade, existing installed sequential
upgrade and JSON pipeline: **4 passed, 2 interactive-mode skips in 26.60s**.
Both new modules pass pinned Ruff lint/format and integration diff checks.
The full Python type check passes with one existing reviewed exception and no
unlisted errors.
This does not close the independent human scorecard, U2's runnable-candidate
prerequisite or W09/W17 removal/admission gates.

### U8 upgrade facilitator integrated — 2026-09-24

The scoped patch `/private/tmp/vonk-u8-upgrade-facilitator.patch`, based on
`9ca56aff`, adds the disposable upgrade launcher and
[facilitator guide](cli-upgrade-walkthrough.md). It reuses the real registered
API, PostgreSQL upgrade/agent-job owners, installed wheel and loopback HTTPS.
The private shell resolves the temporary wheel first, isolates HOME and disables
history. External current-package publication and source loading use explicit
fixtures; no publisher-signature, package-installation or physical claim is made.

Parent integration validation: **3 passed, 1 interactive-mode skip in 16.74s**
for the new smoke, existing installed sequential upgrade and JSON pipeline.
An additional parent PTY run submitted an upgrade from the live shell, read the
exact operation as `waiting-for-operator`, then exited: **1 passed in 55.09s**.
The post-shell check proves one first-node child and no second-node dispatch;
teardown verifies temporary resources are removed. Focused Ruff check/format,
the integration diff check and the full Python type check pass (one existing
reviewed exception, no unlisted errors). These are automated fixture results, not
independent participant acceptance. The full U8 card remains unchanged; cleanup
still depends on W09/W17 and the human scorecard remains unrun.

### Removal pre-effect intent failure is now executable — 2026-09-24

One isolated test-only W09e regression is available at
`/private/tmp/vonk-w09e-pre-effect-intent.patch`, based on `9ca56aff`. It invokes
the actual recipe-image removal service against disposable PostgreSQL and
its own temporary archive/receipt. At the first managed unlink, a separate
database session requires committed, nonterminal intent for the exact request.
Current code fails because no removal owner exists at that boundary. The guard
raises before the filesystem effect and verifies both files remain intact.
Result: **1 failed in 3.42s**; log:
`/private/tmp/vonk-w09e-pre-effect-intent-failure.txt`.

The regression is intentionally separate from the qualified candidate, is not
marked xfail, and changes no production code. It proves the missing pre-effect
owner boundary only; cross-process arbitration, accepted-reference fencing,
post-unlink death, model-child recovery and the final removal protocol remain
open. It does not replace those original requirements or retry the rejected
remover implementation.

### Remaining admission failure reproduced on current source — 2026-09-24

The existing W09d held-node regression was rerun against current source
`9ca56aff` and disposable PostgreSQL. It failed at the intended assertion:
lifecycle start did not return while an `AgentNode FOR UPDATE` holder remained
active; after the holder released it returned a running `recipe.start` instead
of the expected prompt domain refusal. **1 failed in 5.00s**. Evidence:
`/private/tmp/vonk-admission-current-reproduction.log`; original test delta:
`/private/tmp/vonk-admission-busy-review.patch`. The temporary test copy was
removed after execution; production source and the candidate test suite were
unchanged. The prior production-edit approval boundary was not retried.

This is concrete current evidence for that bounded contention defect, not proof
of the entire cross-writer lock-order or rollback contract. W09d remains open.
The green fast-suite record below excludes this PostgreSQL lane and must not
be used as evidence that admission is complete.

Read-only W09e inspection also confirms that removal still unlinks storage
before its durable removal Job is inserted, with model-child removal after a
succeeded parent. Existing sequential replay and busy-owner tests cannot prove
cross-process first-submission ownership, deletion/reference fencing, or crash
recovery. Those original acceptance cases remain required; no replacement
criterion or production edit was introduced.

The U2 local-image inventory found a cached arm64 vLLM runtime with the required
interface label, but no exact manifest or compatible local model was qualified.
Its config ID is not a manifest digest. Both stalled read-only Skopeo inspections
were terminated through their exact sessions (exit 130). No image was pulled,
built, copied or run; the runnable-candidate prerequisite remains open.

### Combined qualification after the six-agent batch — 2026-09-24

The integrated candidate passed the complete Controller fast suite:
**2,420 passed, 3 skipped in 99.66s**. The standalone root environment passed
**1,038 tests, 12 skipped and 45 subtests in 39.98s**. Logs are
`/private/tmp/vonk-cli-control-guidance-final.log` and
`/private/tmp/vonk-cli-root-guidance-final.log`. The production web build passed
(`/private/tmp/vonk-cli-web-guidance-final.log`). Full Python types retain one
reviewed exception and no unlisted errors; Ruff passed, all **689** files were
formatted, coordination boundaries held at **0** reviewed sites, and diff
whitespace checks passed. The curated supply map regenerated and verified with
no digest change. Focused installed tests and actual-output capture are recorded
below. The U6 draft was excluded from all integration claims.

This qualifies the reviewed batch, not the full objective: W09, W12's removal
dependency, W17 and W19 remain open. No merge, publication, deployment, physical
Spark execution or independent human acceptance occurred.

### Earlier U6 draft was not qualified — 2026-09-24

The isolated U6 draft was not integrated: no smoke test had run, exact build
identity was guessed instead of propagated from the setup owner, and the
claimed issued/unissued effect combination was not demonstrated. The draft
remains at `/private/tmp/vonk-cli-u6-facilitator` for inspection; its existence
is not implementation evidence. Repeating the already-qualified U2 cache
blocker would not satisfy U6. Preserve the original U6 card and build a bounded
fixture around exact owner identities and demonstrated effect states before
scheduling a participant. Existing cancellation qualification is unchanged.

### Whole-Fleet review and result facilitators — 2026-09-24

The separate [U4 facilitator](cli-u4-whole-fleet-facilitator.md) presents the
complete affected/idle review, changes saved intent through the registered API
before admission, and confirms that a stale digest creates no application or
execution changes. A new explicit consent admits only the refreshed digest and
leaves execution queued. Parent corrected its workspace lifecycle to remove
wheel, credentials and files through a private TemporaryDirectory, instead of
relying on pytest's retained temporary paths. Integrated smoke passed **1 test,
1 interactive case skipped in 8.01s**.

The separate [U7 facilitator](cli-results-walkthrough.md) exposes a Profile-owned
published route and three artifact outcomes: unavailable, verified file, and
successful empty result. Its deterministic executor consumes a real issued
agent claim and submits a canonical result through the real result owner;
artifact storage/publication and installed download checks remain active. Two
disposable databases isolate fixture identities behind one local app. Parent
reused the common credential-redaction helper and qualified the integrated
smoke: **1 test passed, 1 interactive case skipped in 12.02s**. The agent also
checked normal interactive exit and cleanup. Neither run is an independent
participant result or a physical Spark claim. W19 remains open.

### Corrected recovery guidance and actual handoff examples — 2026-09-24

Source checkpoint: `872d639aa48da2aabbe5d67676a15ed21a548da6`. The central CLI
error builder uses the existing ambiguity rule and known submission acceptance:
definitive artifact refusal offers exact job detail, stale profile refusal
offers a fresh read-only review, and accepted/uncertain operations retain their
reconciliation guidance. The failure-first wrong-key regression reproduced the
old command before correction. Parent review removed an unused override argument
so callers cannot independently redefine the acceptance decision.

Parent qualification: **136 source CLI tests passed in 5.37s**; all **7 installed
capture scenarios passed in 26.08s**, retaining **32 actual process observations**.
The [handoff examples](../examples/cli-operator/README.md) include full capture,
canonical JSON examples and actual human review/progress/error/result output.
They explicitly distinguish deterministic fixtures, no-effect idle receipts,
expected refusals, and unperformed human/deployment/hardware acceptance.
W16's own criteria are qualified again; W19 remains open.

Full Ruff and Python types passed (one existing reviewed exception, no unlisted
errors); changed-file formatting passed. The regenerated and verified supply
map remains `7b71776c6ac65297bb347258aa0bf35756b230924a0976d7c44a072f411c31dc`.
The source snapshot preserves the pending merge, branch and working index.

### Verified cache-readiness fixture and real JSON pipeline — 2026-09-24

The U2 fixture now prepares exact model bytes through the catalog-backed
ModelCacheService, and converts and inspects a local OCI archive with Skopeo
before publication through the existing image-availability and storage owners.
The installed CLI sees a later-page candidate with ready fit/cache/readiness,
plus a separate missing-asset candidate whose advertised preparation queues
real durable work. The empty OCI fixture does not establish engine startup;
the original runnable-candidate prerequisite remains open. No physical or human
acceptance is claimed.

The [U8 read-only pipeline](cli-operator-u8-readonly.md) now has an installed
regression with a real downstream process, closed input, no-input mode, both
exit statuses, one JSON document, empty error streams and unchanged operation
counts. It grants no cleanup authorization. Parent qualification of U1/partial
U2/U3, U5 and this pipeline passed **3 tests, 2 interactive cases skipped in
20.87s**. The pipeline also passed independently before integration in **6.70s**.
Pinned Ruff passed for both fixture increments and the capture helper.

### Actual transcript capture exposed refused-request guidance — 2026-09-24

The [capture helper and procedure](cli-output-capture.md) ran seven existing
installed acceptance scenarios against
source `bf323989`, preserving 31 actual process observations with credentials
excluded. Review of the real wrong-key artifact-submit refusal found a concrete
recovery defect: HTTP 409 reports that the job belongs to another submit request
and marks the error nonretryable, but `reconcile.operation` repeats the rejected
new-key submit. Human output labels that same command `Next`. This does not
perform another mutation automatically, but it is misleading recovery guidance.

A bounded correction is in isolated implementation. Known jobs should offer
read-only inspection after definitive refusal; same-key mutation replay remains
appropriate only when acceptance is ambiguous. W16 is reopened until the
connected regression and both output forms pass. The capture will be regenerated
from the corrected source before it becomes the handoff sample bundle. No
invented layout or failed response is being presented as a passing example.

### Artifact corrections and walkthrough qualification — 2026-09-24

Source checkpoint: `bf32398908fa4d87b32eb33589f75e63f698c125`. The temporary
index snapshot preserves the branch, pending merge and working index and
excludes environment symlinks.

Integrated the explicit artifact subcommand renderer and contract-valid empty
results. Human views show the artifact job ID, state/reason, inputs/results,
verified local files and exact `recipe job detail ID --follow` reconnect.
Successful empty results are distinct from unavailable results. The owner uses
the existing compiled output-slot validator both when consuming a terminal
result and when projecting success, preserving required-output refusal without
maintaining a second rule. Native producer/protocol inspection required no Rust
change. Schema regeneration left the full and packaged API schemas unchanged.

Parent checks from the integrated candidate:

- Artifact service/API plus installed wheel: **65 passed in 23.37s**. Parent
  extended installed checks to include human queued/detail/download output,
  verified file reuse, successful empty results and required-output failure.
- Source artifact entrypoint/renderer: **42 passed in 0.92s**, after the original
  wrong identity/reconnect had been reproduced before the fix.
- Combined U1/U3, partial U2 and U5 smoke plus existing installed-load helper:
  **4 passed, 2 interactive cases skipped in 23.81s**. U5's request wrapper was
  subsequently corrected to match TestClient's `url` parameter name for typing;
  its final focused rerun passed **1 test, 1 interactive case skipped in 7.59s**.
  No baseline exception was added.
- Full Controller fast suite: **2,420 passed, 3 skipped in 126.47s**,
  `/private/tmp/vonk-cli-control-artifact-final.log`.
- Standalone root fast suite: **1,037 passed, 12 skipped, 45 subtests in 38.01s**,
  `/private/tmp/vonk-cli-root-artifact-network-final.log`. The initial sandboxed
  run had 14 local-server bind permission failures; this complete rerun used
  loopback networking. Those failures were not treated as product regressions.

Full Python types pass with one existing reviewed exception and no unlisted
errors. Pinned Ruff lint and formatting pass (681 files); the web production
build and coordination scanner pass. Supply-chain regeneration and verification
pass with manifest digest
`7b71776c6ac65297bb347258aa0bf35756b230924a0976d7c44a072f411c31dc`.
The fast suites exclude their lane cases; the installed/PostgreSQL results
above are separate evidence, not physical Spark or deployment acceptance.

The discovery fixture proves later-page fit, exact missing model/image blockers
and one durable queued prepare request. It does not yet provide the original
U2 card's usable later-page candidate; that requirement remains unchanged.
Managed fixture files now live under the launcher's temporary cleanup root.
The separate [U5 setup](cli-observer-walkthrough.md) uses real queued Profile
owners and installed SIGINT/fresh-process observation; it shares U1's private
session environment helper. No human scorecard has been completed. W09, W12's
removal dependency, W17 and W19 remain open.

### Artifact acceptance gaps reopened — 2026-09-24

Current source inspection found unconditional nonempty-output checks in
`ArtifactJobService.consume_agent_result`, `ArtifactJobResponse` and CLI
artifact download, while the wire manifest allows an empty file list and
compiled output slots have explicit minimum counts. The isolated correction
must prove optional-output success and preserve required-output refusal through
the real result consumer and installed CLI. This is not permission to insert a
synthetic succeeded row or weaken manifest/identity validation.

Parent reproduction also sent the current download receipt to the command's
actual renderer dispatch (`recipe`, action `job`). It printed only
`Operation: unavailable`, `Request: unavailable`, `State: succeeded` and
`Progress: succeeded | progress unavailable`; the exact job ID and downloaded
file/path were absent. Dedicated command-aware artifact views are now in
isolated implementation. W02/W16 are reopened until connected fixes and
meaningful source/installed checks pass; earlier package counts are historical.

### Disposable U1/U3 walkthrough setup — 2026-09-24

Integrated the opt-in facilitator from `vonk-w19-disposable-facilitator.patch`.
It builds an independent CLI wheel and connects it through loopback HTTPS to
registered routes with disposable PostgreSQL service owners. Smoke covers
first connection, Fleet read and installed-only Profile edit/export/import;
Job, application and run counts remain unchanged. Credentials and exports are
private files. No Profile-load, Fleet-remove or upgrade providers are supplied.
The shell uses ordinary host networking; no network sandbox is claimed.

Parent verification in the integrated candidate:

```bash
VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes \
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-implementation-cache \
VONK_WALKTHROUGH_MODE=smoke \
/private/tmp/vonk-cli-integration-env/bin/python -m pytest -q \
  control/tests/test_cli_operator_walkthrough.py \
  control/tests/test_profile_load_installed_cli.py
```

**3 passed, 1 skipped in 14.36s**, including existing installed load helper
regressions. Full Python types pass with one reviewed exception and no unlisted
errors; pinned Ruff passes. Agent verification also exercised a bounded shell
stub exiting 17 and fixture cleanup; that is not an actual human walkthrough
or terminal-signal qualification. Documented normal shutdown is `exit`/Ctrl-D.
Local temporary assets are removed, and PostgreSQL teardown is scoped to the
fixture's own database/container. Forced process death is not a supported stop.

The [facilitator protocol](cli-operator-walkthrough.md) has launch instructions.
U2 and U4–U8 scenario setup and the independent human scorecard remain open.
The U2 real-owner discovery extension is in isolated implementation. This
increment does not close W19 or the W09/W17 boundaries.

### Combined receipt-contract qualification — 2026-09-24

Source snapshot: `324d5c4875916c305511a3e1824ab2410b00b645` (temporary index;
branch, working index and pending merge preserved). This includes the receipt
fix, regenerated contracts/clients, final W13 settlement assertion and prior
integrated acceptance increments. It excludes environment symlinks.

From the integrated candidate with `UV_CACHE_DIR=/private/tmp/vonk-forge-cli-implementation-cache`
and `VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes`:

| Invocation | Result and log |
| --- | --- |
| `/private/tmp/vonk-cli-integration-env/bin/python -m pytest -q control/tests -m 'not lane' -n auto --dist loadfile` | **2,420 passed, 3 skipped in 98.88s**; `/private/tmp/vonk-cli-control-receipt-final.log` |
| `uv run --offline --python 3.14 --frozen --with pytest==9.1.1 --with pytest-xdist==3.8.0 --with-editable /opt/vonk-forge-recipes/contracts pytest -q tests -m 'not lane' -n auto` | **1,029 passed, 12 skipped, 45 subtests in 40.81s**; `/private/tmp/vonk-cli-root-receipt-final.log` |

Full lint/format, Python types (one reviewed exception), web build, regenerated
client checks, supply-chain verification and coordination scan pass. These fast
runs exclude their lane tests; they are not complete PostgreSQL/Linux, deployed
or physical qualification. The targeted connected/installed evidence remains
recorded separately. W09, the W12 removal dependency, W17's removal protocol,
and W19 remain open. The disposable U1/U3 human-session launcher is now integrated and separately
verified above; the walkthrough itself has not run.

### Removal replay and receipt identity correction — 2026-09-24

Integrated the bounded non-destructive validation correction. A strict current
removal intent is stored on the existing Job, with its issuer, request key,
revision and payload digest checked against the Job envelope. An identical
accepted request replays before mutable catalog resolution. A changed selector,
actor or model-retention choice is refused; missing/malformed current intent
and inconsistent stored results fail closed without legacy inference. Stored
integer values cannot masquerade as boolean choices. The API projects required
`with_model` from that accepted stored result, and the CLI validates it before
following the operation.

The original three changed-intent regressions failed against snapshot `2047a4cf`.
Parent focused verification after integration passes **125 Controller tests,
1 lane case deselected in 7.90s**, and **107 source CLI/generated-contract tests
in 5.02s**. These are not installed-process tests. All API schemas and generated
Python/TypeScript clients were regenerated together. Full Python types pass
with one reviewed exception and no unlisted errors; pinned Ruff lint/format
passes (678 files); web production build and supply-chain regeneration/check
pass. The current curated manifest digest is
`5c1a7847febe3ca88c696d3e57aff58ddd4acce347ced51b606bfd2af4d45b70`.

This change does not alter unlink, cancellation, SQL membership removal,
artifact locking or effect sequencing. First concurrent submissions are not
arbitrated by a replay read, and the existing remover still persists its Job
after effects. Crash-safe pre-effect intent, shared lifetime protection and
resumable removal remain unfinished. No previously rejected destructive-remover
implementation was retried, and this correction does not close W17.

### Late cancellation receipt preserves newer intent — 2026-09-24

The W13 newer-load case now receives the original issued stop's exact late
cancellation result, confirms its cancellation directive, and observes the
AgentOperation settle. The superseded application remains cancelled; the new
application remains queued and retains every target's newer workload ordinal.
Parent targeted verification passes **1 test, 9 deselected in 4.71s** in
`test_fleet_profile_cancel.py`; pinned Ruff lint/format pass. The preceding full
module passed ten tests before this assertion extension. Together these close
the identified W13 partial-stop, process-death and newer-intent acceptance gaps,
without making any claim that the remaining W09 admission invariants are done.

### Integrated acceptance source checkpoint — 2026-09-24

Snapshot `7f5b74bbf999b3752c0511008bd7be5b70e3fa27` preserves the integrated
review rendering fix, installed re-enrollment and endpoint checks, partial and
shared update cancellation, profile cancellation process tests, and full U1–U8
walkthrough protocol. It was created with a temporary Git index and the existing
HEAD/MERGE_HEAD parents. The working index, branch, pending merge and original
shared checkout remain unchanged. Environment symlinks are excluded.

This is a source checkpoint, not a release or completion claim. Its targeted
qualification is recorded at the immediately preceding package checkpoints;
the broad source baseline remains explicitly tied to `2047a4cf`. Receipt replay
validation and final newer-intent cleanup assertions are still in isolated
work. The independent operator walkthrough and W09/W17 completion remain open.

### Profile cancellation process and partial-effect acceptance — 2026-09-24

Integrated three real PostgreSQL cases for partial two-target stopping, worker
process death while cancellation is pending, and newer profile intent arriving
during pending cancellation. The process case starts a separate worker that
reconstructs the production owners, exits through `os._exit(23)`, then starts a
fresh worker after the exact authenticated agent result arrives. Pending intent
and reservations survive, and the original cleanup identity settles without
a duplicate stop job. Parent full-module verification passes **10 tests in
15.03s** in `control/tests/test_fleet_profile_cancel.py`.

Review checked the native stop-result contract before accepting the partial
effect assertion: Rust emits `cancelled` only after exact host STOP and local
cleanup complete; the Controller therefore records the corresponding node as
stopped. The test supplies that canonical result, not hardware evidence.
The newer-load case proves that the latest node ordinal belongs to the new
request and the original issued stop retains its owner. Its final lower-child
cleanup settlement is being added before treating this remaining boundary as
fully qualified. No production defect was reproduced by this increment.

### Shared update cancellation preserves unrelated work — 2026-09-24

Two admitted recipe-update children and an unrelated accepted request now
exercise the same real `ModelCacheService` child through PostgreSQL. Cancelling
the update settles its two children while leaving the unrelated request and
shared model operation active. Parent review strengthened the regression to
read the actual partial file before cancellation and compare the exact bytes
afterward; positive progress counters alone would not prove preservation.
The surviving request then resumes that same child, verifies the final bytes
and completes image preparation.

The integrated `control/tests/test_recipe_update_batches.py` module passes
**24 tests in 10.40s** with the populated task cache and isolated Controller
Python against disposable PostgreSQL. Pinned Ruff corrected import order and
formatting is clean. No production change was required. W12's separate cache
removal race still depends on the unfinished W09/W17 removal protocol.

### Current removal replay failure evidence — 2026-09-24

A test-only regression was rerun against source snapshot `2047a4cf` after
rejecting its original obsolete `a1ce4439` base as current evidence. Each of
three changed-intent replays (selector, actor, and `with_model`) returns the
old successful receipt instead of refusing the reused key: **3 failed in
1.07s**, all at the expected refusal assertion. Inspection of the integrated
`remove_selector` confirms the same unconditional stored-result return.

The corrected review patch is
`/private/tmp/vonk-w17-recipe-removal-intent-2047.patch`. It uses only temporary
SQLite catalog rows, with no image authorizations or managed artifact files.
This proves the deterministic request-key contract defect; it supplies no
concurrency, deletion-lifetime or PostgreSQL-locking qualification. Pinned Ruff
and diff checks pass. The deliberately failing patch is not integrated, and no
destructive remover change has been retried.

### Whole-fleet review capacity reasons — 2026-09-24

A real PostgreSQL preview supplied a per-node port blocker that the CLI omitted:
the old run holds port 8000, while the accepted replacement fits after the
reviewed stop. Parent failure-first qualification installed the current wheel
and failed at the missing Spark-specific capacity reason (**1 failed in 6.71s**).
The rendering fix now prints per-node blockers and warnings alongside the
existing resource figures and Spark identity.

With the fix, the representative installed review and existing installed load
module pass **3 tests in 12.56s**. They use a real Controller preview/owner and
HTTPS client; the review scenario supplies deterministic artifact availability
and does not claim physical artifact or hardware execution. The displayed review
includes the complete two-node group, idle Spark, exact reused model/image,
stop/start effects, current blocker, fit after stopping and interruption warning.
Declining consent issues no load request. Renderer/profile-load source checks
pass **34 tests in 0.39s**.

Full pinned Ruff lint/format passes (676 files); Python types pass with one
reviewed exception and no unlisted errors; the web production build and
supply-chain verifier pass. No generated contract change was needed. W09's
admission-policy, lock-order and removal dependencies remain open.

### Installed re-enrollment and endpoint presentation — 2026-09-24

The re-enrollment acceptance uses the independently installed CLI, a PTY,
HTTPS and the real PostgreSQL owner. It observes the complete display name,
canonical node ID and replacement consequence before sending consent, and
checks that only the read has occurred at that point. Decline, EOF and
`--no-input` create no grant; confirmation targets the canonical node and writes
the secret only to the private output file. Parent verification of
`control/tests/test_enrollment_reenroll_installed_cli.py` passes **4 tests in
11.35s**.

The endpoint installed journey now derives expiry and configuration-example
expectations from the registered API's real owner projection. The complete
`control/tests/test_cli_first_connection_endpoints_installed.py` passes
**3 tests in 10.69s** after this addition, including the connected owner matrix.
Both parent invocations use the isolated Controller Python, populated task uv
cache, sibling recipe-library environment, disposable PostgreSQL and local
HTTPS listeners described in the baseline. Pinned Ruff lint/format pass.

Whole-fleet review acceptance exposed a separate W10 presentation defect:
per-node capacity blockers are not rendered even though the preview supplies
them. The current case shows a live run's port reservation blocking current
capacity, with capacity fitting after the reviewed stop. A focused rendering
correction is in progress; these W04/W15 results do not close that defect.

### Connected update cancellation and endpoint ownership — 2026-09-24

Integrated two test-only increments after reviewing their owner boundaries.
The multi-recipe update test admits the first child, cancels the parent,
reconstructs the service and reconciles twice: the later child never receives
a job, while the parent and issued child settle cancellation. The endpoint
test uses PostgreSQL, the registered route and `FleetProfileService.endpoint_intent`
to check installed-only state, foreign-profile alias refusal, route generation
replacement and expiry. Neither increment required a production change.

Parent validation from the integrated candidate, using the isolated Controller
environment and disposable OrbStack PostgreSQL:

```bash
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-implementation-cache \
VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes \
/private/tmp/vonk-cli-integration-env/bin/python -m pytest -q \
  control/tests/test_recipe_update_batches.py \
  control/tests/test_cli_first_connection_endpoints_installed.py \
  -k 'not installed_cli'
```

Result: **24 passed, 2 deselected in 10.68s**. Pinned Ruff 0.16.1 lint and
format checks and `git diff --check` pass. The excluded installed CLI cases
are not claimed by this invocation. A subsequent parent invocation of the
same endpoint module with `-k installed_cli` passed **2 tests, 1 deselected in
9.88s**, exercising the independently installed wheel and actual HTTPS API.
Full Python types pass with one reviewed exception and no unlisted errors.
W12's cache-removal race remains a
W09/W17 dependency; these tests do not qualify the unfinished remover.

### W09d current busy-node failure evidence — 2026-09-24

A test-only regression on snapshot `2047a4cf` confirms that lifecycle start
still waits on a held PostgreSQL AgentNode row. An execution hook establishes
that start reached the row-lock query; it does not return during the holder
window. Once the unchanged holder releases its lock, start accepts a
`recipe.start` operation instead of returning the required prompt domain
refusal. No node state or capability mutation is used to manufacture a later
refusal. The regression fails at the intended early-return assertion.

This is a confirmed implementation gap, not a missing test alone. The test-only
patch is `/private/tmp/vonk-admission-busy-review.patch`; captured failure is
`/private/tmp/vonk-admission-busy-review-failure.txt` (**1 failed in 4.16s**).
Its lint/format/diff checks pass. It remains outside the integrated candidate
because the production lock-order correction is still behind the previously
recorded automatic-approval rejection. No blocked production edit was retried.
The passing combined suites do not qualify this unimplemented invariant.

### Reproducible current qualification baseline — 2026-09-24

Source snapshot: `2047a4cfc5e58c89c8ae08d1b68b16e714ac08a2`. Host: Darwin
arm64, Python 3.14.7, uv 0.12.9. The isolated Controller environment uses the
rebuilt protocol wheel and an editable candidate CLI, matching source-suite
requirements. Installed CLI tests separately build/install a wheel. Disposable
HTTP/TLS listeners are enabled; PostgreSQL/process tests use their stated lane.
The original 75-test/7.74s pre-edit summary is retained as history, but its exact
invocation/environment has not been recovered and is not treated as a complete
evidence record.

All commands below run from `/private/tmp/vonk-cli-integration` with:

```bash
export UV_CACHE_DIR=/private/tmp/vonk-forge-cli-implementation-cache
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes
```

| Exact current command | Result and retained log |
| --- | --- |
| `/private/tmp/vonk-cli-integration-env/bin/python -m pytest -q control/tests -m 'not lane' -n auto --dist loadfile` | 2,412 passed, 3 skipped, 86.38s; `/private/tmp/vonk-cli-control-kind-final.log` |
| `uv run --offline --python 3.14 --frozen --with pytest==9.1.1 --with pytest-xdist==3.8.0 --with-editable /opt/vonk-forge-recipes/contracts pytest -q tests -m 'not lane' -n auto` | 1,028 passed, 12 skipped, 45 subtests, 42.10s; `/private/tmp/vonk-cli-root-packaged-kind.log` |
| `/private/tmp/vonk-cli-integration-env/bin/python -m pytest -q control/tests/test_artifact_job_installed_cli.py control/tests/test_profile_load_installed_cli.py control/tests/test_cli_runbook_parser_installed.py` | 4 passed, 28.51s; `/private/tmp/vonk-cli-kind-installed.log` |

These commands prove their named source/installed boundaries. They do not run
the complete PostgreSQL/Linux lane, an independent operator walkthrough,
publication, Controller deployment, or physical Spark acceptance. Specific
connected/process/Linux evidence remains recorded at its owning checkpoint.

### Package-specific completion audit — 2026-09-24

Read-only review confirms W11, W14, W16 and W18's own repository/installed
criteria have current evidence. Passing them does not close their outstanding
delivery dependencies or W19 usability acceptance. W10 still needs a
representative installed whole-fleet review to demonstrate visible effect
presentation. W12 needs cancellation between child admissions in a multi-recipe
update. W13 needs partial multi-target stop, process death during pending
cancellation, and newer-intent overlap cases. W15's projection race tests use
a supplied intent provider; the missing cases must exercise the real profile
intent owner rather than duplicate those tests. These are existing plan
criteria, not new features. Bounded connected tests are being added in isolated
worktrees; production fixes will follow only if those tests establish a defect.

W17 removal and W19's independent operator walkthrough remain incomplete.
The destructive removers still have replay/intent/lifetime gaps; passing CLI
receipt tests cannot substitute for fixing their owning services. W09's common
lock-order edit and resumable deletion remain behind their recorded approval
boundary. The retained-memory policy question remains unanswered.

### Typed runtime memory pool and rebuilt package — 2026-09-24

Compiled placement and signed start/job requests now require the canonical
memory kind. Controller compilation, normal start, artifact jobs and distributed
recovery carry the accepted value; Python and Rust reject mismatches with the
compiled plan. On separate-memory hardware, host demand checks host capacity,
accelerator demand checks accelerator capacity, and unified demand checks both.
Shared-memory hardware continues to use its single host-backed pool. The
previous minimum-of-both check falsely refused legitimate host-only and
accelerator-only plans; failure-first native tests cover both directions.

Integration retained the newer canonical test parsing and added the required
kind to the install-admission fixture. Run/Switch reuses the canonical type;
RunNodePlan's annotation preserves that validated type instead of widening it
to an arbitrary string. Recovery tests use the canonical payload parser and
compare the accepted kind through the actual child, replacing a duplicated
literal field-list assertion.

Parent focused Controller checks pass **221 tests in 22.49s**, with seven lane
cases deselected; the recovery producer check passes **1 test in 4.70s**.
Full Python types pass with one reviewed exception and no unlisted errors.
Agent Linux Rust package suites pass (one existing ignored test), and its
Python protocol selection passes **423 tests**. The two previously macOS-limited
Python/Rust probes were then run in OrbStack against the integrated candidate,
mounted read-only: **2 passed in 0.89s**, including both wire directions.

The protocol wheel was rebuilt from current source. `uv lock --upgrade-package
vonk-agent-protocol --offline` refreshed only its lock hash to
`519484690b626f03e27efabad787ad1040a7d527404f2dd4faa3e2d84c40d116`;
a plain lock/refresh invocation had correctly been checked and did not update
that hash. The isolated environment now imports the actual protocol wheel.
OpenAPI/Python/TypeScript clients and the supply-chain artifacts were regenerated.
Wire verification, supply-chain verification and public-image inputs pass; the
web production build passes. Standalone root checks pass **1,028 tests, with
12 skips and 45 subtests, in 42.10s**.

The first combined Controller run after sync found eight CLI-schema failures
because sync replaced the editable CLI with a pre-regeneration packaged copy.
Installed and source schema hashes differed. Restoring the documented editable
root test setup while retaining the actual protocol wheel makes all **91 tests
in the affected modules pass in 10.99s**. One further recovery assertion used
a wrapper property that does not exist; it now reads the canonical typed
payload. The final combined Controller fast suite passes **2,412 tests, with 3 skips,
in 86.38s**. Freshly installed artifact/profile-load/runbook checks pass
**4 tests in 28.51s** against actual PostgreSQL/TLS services where applicable.
Full Python types again pass with the single reviewed exception. Logs:
`/private/tmp/vonk-cli-control-kind-final.log`,
`/private/tmp/vonk-cli-kind-installed.log`, and
`/private/tmp/vonk-cli-kind-final-types.log`.
This does not close retained-memory policy, common lock-order, removal, or
operator walkthrough gates.

### Pre-integration consistency gates — 2026-09-24

After the enrollment and lifecycle corrections, full pinned Ruff lint passes,
all **674 Python files** pass formatting, the full Python type gate has one
reviewed exception and no unlisted errors, and the coordination scan reports
**zero reviewed exceptions**. Diff checks pass. This is the current candidate
before the pending memory-kind increment, not its final package qualification.

### Retained-memory policy needs a product decision — 2026-09-24

Read-only native/Controller review confirms that aggregate inventory and signed
run liveness do not provide an atomic attributed memory snapshot. Reading
aggregate free and owner usage into one authenticated message does not prevent
allocation/free races between kernel reads. Podman's host-memory limit is not
a separate accelerator-memory quota. The current runtime therefore cannot
prove both exact fit without false refusal and strict physical non-overcommit
for retained accelerator workloads.

For total 100/free 70/retained peak 15/new demand 60, a retained run using 10
would leave 65 bytes after its remaining growth, while a run using 0 leaves
55. The current aggregate evidence cannot safely distinguish them. The earlier
measurement prototype remains excluded. A user question is pending: present
uncertain estimates as warnings while retaining actual capacity checks, or
retain conservative refusal of uncertain cases. No answer is inferred from
elapsed time, and no package is closed by this finding. Typed memory-kind and
floor propagation remain independently necessary and continue.

### Enrollment refusal preserves received HTTP evidence — 2026-09-24

Enrollment and the generic submission path now share one known-4xx classifier.
A malformed or oversized 422 response remains an explicit issuance refusal,
without a GET that could attach an older pending grant. Malformed 200 and 503
responses still reconcile the original request identity. Integration preserves
the newer generic submission error/retry handling; the older agent patch's
context was not substituted for it. Parent regressions use the real
`ControlClient` and raise `HTTPError` for HTTP failures, matching the opener's
production behavior.

Parent enrollment, profile-load, cache-submission and Controller CLI checks:
**146 passed in 4.72s**. Pinned Ruff lint/format and diff checks pass. Log:
`/private/tmp/vonk-cli-enrollment-4xx-parent.log`. The installed-wheel
PostgreSQL/TLS enrollment module also passes **3 tests in 19.02s**, including
process-death recovery; log:
`/private/tmp/vonk-cli-enrollment-4xx-installed.log`. The full Python type gate
passes with one reviewed exception and no unlisted errors after integration.

### Lifecycle harness follows the accepted identity — 2026-09-24

The acceptance harness now polls the exact submitted profile application and
refuses a different returned identity, instead of following numbered-profile
latest progress. Its four stale test callers now supply the required node ID;
its synthetic receipt uses the current required request key and progress shape.
The corrected tests exposed diagnostic starvation: a large early section could
consume the entire output budget and hide worker/journal evidence. Allocation
now reserves space for each remaining section within the existing 8,400-character
budget. Test length bounds derive from that budget plus the exact error prefix.

Parent module acceptance: **49 passed in 1.15s**. Log:
`/private/tmp/vonk-cli-lifecycle-runner-parent.log`. This is harness behavior,
not a claim that physical Spark acceptance ran. The full Python type gate
also passes with one reviewed exception and no unlisted errors (before the
subsequent enrollment classifier increment).

### Combined Controller floor-consumer correction — 2026-09-24

The combined fast Controller run reported **2,396 passed, 3 skipped and 16
failed in 80.85s**. All failures shared one test producer: install-admission's
compiled-plan fixture omitted the now-required memory floor. It now parses the
canonical recipe and passes the mapped role's declared reserve into the compiled
placement. No compatibility default was added to production. The complete
corrected module passes **22 tests in 2.08s**; scoped lint and formatting pass.
Logs: `/private/tmp/vonk-cli-control-latest-combined.log` and
`/private/tmp/vonk-cli-install-floor-fixture.log`. A final combined run remains
pending the memory-kind producer/consumer increment.

The existing installed runbook/parser gate also passes (**1 test**), validating
the current command examples against the built candidate wheel without
dispatching mutations. No stale digest-only artifact URL appears in the runbook.

### Standalone root qualification refresh — 2026-09-24

The root tree passes in its standalone CI-shaped environment: **1,024 passed,
11 skipped, 45 subtests passed in 22.59s**. Local HTTP/TLS fixtures ran with
listener access. Log: `/private/tmp/vonk-cli-root-standalone-latest.log`.
A separate control-environment run exposed four stale lifecycle harness test
callers that standalone execution skips; their current-contract correction is
in progress. Its local-listener permission errors are environment failures,
not substituted passing evidence. This checkpoint predates the memory-kind
increment and does not close final packaging or combined Controller gates.
The full candidate web suite also passes: **163 tests across 23 files**, using
`npm test -- --run`; no source or lockfile changes were made for that run.

### Installed timeout retains the original application — 2026-09-24

A newer same-profile application is admitted through the registered save,
preview and load routes on a second test Spark while the installed observer is
following the original application. The first read resolves the original ID;
all later reads use its exact application endpoint. At the two-second deadline,
the CLI emits one JSON `timed_out` result and the original reconnect command,
with no cancellation or mutation of the original owner. The profile's latest
progress now points to the newer application, proving the test would catch a
regression to polling latest progress. The longer deadline allows real database
admission in the initial-read hook before exact-ID polling begins.

Parent PostgreSQL/TLS acceptance: **2 passed in 11.09s**, including the existing
SIGINT case. Log: `/private/tmp/vonk-cli-observe-timeout-parent.log`.

### Installed observer across Controller process restart — 2026-09-24

The parent ran the installed-wheel observer against a real HTTPS Uvicorn
Controller process and PostgreSQL. After the first process was killed, a second
process reused the database, URL, socket and certificate. Its request trace
proves that the existing observer read the same application from the new
process. SIGINT then ended local observation with exit 130 and an interrupted
result; a fresh installed request-key lookup reconnected to that same owner.
The database retained its queued state, digests, progress and timestamps, with
one load POST and no cancellation or replacement. This tests API-process
recovery; the deterministic execution adapter is not physical Spark evidence.

Parent acceptance: **1 passed in 21.55s**. Scoped lint, formatting and diff
checks pass. The full Python type gate also passes with one reviewed exception
and no unlisted errors. Log: `/private/tmp/vonk-cli-controller-restart-parent.log`.

### Exact artifact output identity integration — 2026-09-24

Artifact downloads now select the exact job, output name and digest. The old
digest-only route is removed. Two legal output rows sharing bytes but declaring
different names/media types remain independently addressable. The registered
route, service lookup, operation map, web preview/helper, CLI, qualification
consumer and transfer fixtures now use this one current identity. The shared
service test retains the previously integrated nonzero runtime-reserve assertion
while exercising both same-digest outputs; neither change was discarded.

The CLI probes output names in a private temporary directory on the destination
filesystem before transfer, so case aliases are refused on case-insensitive
storage while case-sensitive destinations can retain both names. Temporary
probes are removed; no output download begins after a collision refusal.

Parent checks so far: **62 service/API tests passed in 7.33s**, **26 focused
web tests passed in 2.81s**. Full OpenAPI and Python/TypeScript client regeneration
completed against the combined memory-floor and output-identity source. The
CLI/transport/generated-client selection passes **70 tests in 1.51s**, and the
actual installed-wheel/PostgreSQL/TLS artifact journey passes **1 test in 8.10s**,
including corrupt-output refusal and verified publication through the new route.
The production web build and full Python type gate pass (one reviewed exception,
no unlisted errors). Full lint, formatting across 672 files, and diff checks pass.
Linux case-sensitive filename acceptance also passes: the isolated agent ran
the actual alias test in a cached Python 3.14 OrbStack Linux/ARM64 container
with read-only source mounts and networking disabled (**1 passed, 19 deselected**).
A filesystem probe confirmed that `A.png` and `a.png` are distinct there. The
parent real HTTPS transport selection passes **12 tests in 8.29s**. Logs:
`/private/tmp/vonk-cli-artifact-name-parent.log`,
`/private/tmp/vonk-cli-artifact-name-web.log`, and
`/private/tmp/vonk-cli-artifact-name-generate.log`.

The checkpoint single-scan experiment was rejected: rename changes ctime, so
reusing its prior hash across that identity transition could hide an intervening
write. No prototype was integrated. The existing verifier retains the second
scan as an integrity check; eliminating it would require a separate staging
layout change. This performance follow-up does not waive required checkpoint
cleanup/reference coordination.

### Declared runtime reserve integration — 2026-09-24

The combined Controller fast suite passed **2,412 tests, 3 skipped** in 143.29s
before the memory-floor integration. It includes the checkpoint/FIFO repairs,
Run/Switch image references and earlier shared-controller changes. Log:
`/private/tmp/vonk-cli-control-checkpoint-combined.log`.

The candidate now carries the accepted memory reserve through canonical compiled
placement, start/job requests, recovery producers, signed payload validation and
Rust execution. The agent no longer silently adds a fixed 4 GB. The reserve
remains separate from the workload's OCI memory limit. Exact mismatch between
signed request and compiled placement is refused. The Linux boundary test proves
120 GB demand plus 2 GB reserve passes at exactly 122 GB free and fails one KiB
below, as well as the original 123 GB counterexample.

The isolated agent passed full Rust agent/protocol tests, 72 connected wire
cases, and its focused Controller checks. Parent integration passes **306
Controller cases** (7 lane cases deselected, 28.10s), the generated Rust wire
check, and the full Python type gate with one reviewed exception and no unlisted
errors. Test typing was corrected by reading stored plans through their canonical
parser and validating compiled payloads, without widening the baseline. The
changed assertions passed again (**10 passed, 1 skipped, 148 deselected**, plus
one explicit artifact producer case). Logs:
`/private/tmp/vonk-cli-memory-floor-parent.log`,
`/private/tmp/vonk-cli-memory-floor-wire.log`, and
`/private/tmp/vonk-cli-memory-floor-types-corrected.log`.

The local verification environment now imports the candidate protocol source.
The bundled protocol wheel, combined OpenAPI clients and final supply-chain
manifest still require regeneration before package qualification. The independent
host/GPU pool-selection audit, retained-workload accounting and physical Spark
acceptance remain open; this patch proves reserve propagation only.

The HTTP-refusal tests were also corrected to raise `HTTPError` like a real
urllib opener; both CLI/artifact and transport modules pass again (**49 tests
in 1.16s**). This correction restores full type validation and does not weaken
malformed-4xx refusal behavior.

### Enrollment hard death and checkpoint review — 2026-09-24

The installed enrollment test now holds the TLS response after the registered
Controller route commits its grant, then SIGKILLs the real installed CLI. The
fsynced mode-0600 receipt retains the original request identity without the
one-time secret. A fresh CLI reads the same pending grant; exactly one grant
POST occurred. All three enrollment cases and the existing accepted-load
closed-pipe regression pass together: **4 passed in 14.07s**. This verifies
client process death; the separately assigned actual Controller HTTP-process
restart case remains open. Log: `/private/tmp/vonk-cli-enroll-death-parent.log`.

The checkpoint corrective increment preserves the named source-cache lock,
accepts the maximum canonical 584-byte image reference, and distinguishes
nonregular records from out-of-budget records with observed/allowed bytes.
Two missing type narrowings in tests are fixed. Parent checks pass **64 fast
cases** (2 lane cases deselected, 2.88s) and the full Python type gate with one
reviewed exception and no unlisted errors. Logs:
`/private/tmp/vonk-cli-checkpoint-review-tests.log` and
`/private/tmp/vonk-cli-checkpoint-review-types.log`.

Review found an additional concrete wait hazard: opening a checkpoint FIFO can
block before its type check because the open lacks nonblocking mode. A bounded
subprocess regression and fix are now integrated. The agent reproduced the
pre-fix FIFO hang with a one-second subprocess deadline; nonblocking open now
reaches the explicit nonregular-file refusal. Parent checkpoint boundary tests
pass **4 cases in 1.27s** (61 deselected), including FIFO, directory, byte limit
and maximum canonical image reference. Log:
`/private/tmp/vonk-cli-checkpoint-fifo-parent.log`.
Repeated hashing is still unresolved: a new export is hashed by transport and again at commit,
and creating a hardlink invalidates the verifier's stat-based cache. Do not
claim the current patch eliminates all repeated hashing or closes cleanup.

### Artifact HTTP refusal preservation — 2026-09-24

The CLI now treats malformed or oversized 4xx artifact responses as definitive
refusals, using the actual HTTP status retained by the transport. Byte-upload
response validation also preserves that status. Malformed successful responses
and 5xx failures remain ambiguous and follow existing exact-key recovery.
The agent confirmed the malformed/oversized 409 regression before fixing it.
Parent verification: **49 CLI/artifact/transport tests passed in 0.95s**;
changed-file lint and diff checks pass. Log:
`/private/tmp/vonk-cli-http-refusal-parent.log`.

The output download contract still needs exact filename plus digest selection:
valid outputs may share bytes but have different names/media types. Destination
filesystem-name collision protection is also under implementation. These remain
W16 requirements; this refusal fix does not close the whole artifact workflow.

### Acceptance audit corrections — 2026-09-24

Direct source inspection confirms `test_profile_load_closed_pipe.py` already
uses the installed wheel, real PostgreSQL/TLS, an accepted load with a lost
response, exit 141, and a fresh-process reconnect to the original request. Its
prior parent run passed in 7.14s; the read-only audit's claim that this was only
a source-entrypoint test was incorrect. No duplicate test was added. Installed
observation timeout/newer-application pinning and hard-death enrollment delivery
are being addressed separately. Actual Controller HTTP-process restart and the
independent operator walkthrough remain distinct open requirements.

The existing profile endpoint-intent regression now also transitions a completed
application with no current run to `withdrawn`, retaining its immutable assignment
and returning no run identity. It passes (**1 passed, 49 deselected in 0.84s**);
changed-file lint/format and diff checks pass. This covers the missing-run edge
without claiming any current serving endpoint.

A read-only removal audit confirms the current recipe handler resolves mutable
selectors before replay and does not bind `with_model` or actor in that replay.
It writes its terminal Job after unlink and can report success before model
children finish. The implementation plan now specifies the exact stored-intent,
projection and crash/replay regressions that must land with W09e's resumable
remover. No destructive owner changes were made by this audit.

### Published-image export checkpoint integration — 2026-09-24

The candidate now records a strict storage-owned checkpoint for a verified
registry export under the existing source lock. Its identity binds the immutable
registry reference, platform image/configuration, architecture/interface and
archive bytes. The source lock is released before publication ownership checks;
final publication retains the checkpoint via a hardlink. Recovery verifies a
receiptless final archive against exact checkpoint provenance. Process-death
coverage exercises both completed checkpoint and final-link-before-receipt
windows. Current owner cancellation still prevents publication.

Parent verification passes **62 fast tests** (2 lane cases deselected, 7.24s)
and **13 PostgreSQL/process tests** (7.58s) across published-image recovery,
Run/Switch references and OS locks. Coordination reports zero reviewed sites;
changed-file lint passes. The full type gate found four missing union narrowings
in two new tests; correction is pending with a small review follow-up for source
cache ownership and explicit checkpoint size diagnostics. Logs:
`/private/tmp/vonk-cli-checkpoint-parent.log`,
`/private/tmp/vonk-cli-checkpoint-pg.log`, and
`/private/tmp/vonk-cli-checkpoint-types.log`.

The retained link does not duplicate archive blocks, but keeps those blocks
allocated after removing the published name. Checkpoint garbage collection must
join the existing reference/removal owner; it is not implemented by this patch.
Do not infer complete removal coordination or unlimited recoverability from
these focused crash-boundary results.

The W02 JSON preservation regression now also includes a 2,048-item collection
alongside long text. The parsed output preserves the entire document (**1
passed, 32 deselected in 0.89s**), guarding against human-display clipping leaking
into JSON output. This extends the existing test, not a new redundant test path.

### Combined Python style gate — 2026-09-24

The full candidate passes pinned Ruff lint. The full format check initially
identified twelve integrated files with formatting drift; the formatter changed
only those files. Lint and format then pass across all **672 Python files**, and
`git diff --check` passes. This is a source-style gate, not new behavioral,
publication, or hardware evidence. Pending agent integrations must rerun the
appropriate combined gates.

### Installed fleet removal consent — 2026-09-24

The new installed-wheel test uses local TLS, the actual registered fleet routes,
`FleetProjection`, and PostgreSQL-backed `EnrollmentService`. Both JSON and
no-input invocations without `--yes` exit 2 without a removal request or changes
to either node's enrollment. Confirmed removal resolves `Atlas` to its canonical
node ID, posts once with the route's bodyless contract, and retires/revokes only
that node and certificate. The peer remains unchanged.

Parent integration verification: **1 passed in 7.56s**; pinned Ruff formatting
and lint pass. The full Python type gate passes with one reviewed exception
and no unlisted errors. Log: `/private/tmp/vonk-cli-fleet-remove-installed-parent.log`.
This qualifies CLI consent and exact enrollment removal in a disposable
Controller. It neither exercises live fleet removal nor closes cache artifact
removal coordination or owner-bound removal replay.

### Exact web receipts and remaining consent audit — 2026-09-24

The existing artifact web caller now preserves definitive HTTP refusals instead
of accepting an older receipt after denial. Mutation and recovery receipts must
match the requested job and recipe run. Cancellation during creation or submit
never starts a fresh replay; an already accepted draft is cancelled by its exact
identity. An unrelated historical cancelling job no longer blocks new work.
This is compatibility repair for the shared CLI/API contract, not a web redesign.

The two-file follow-up was integrated atop the original request-key patch.
The complete web suite passes **162 tests across 23 files in 4.69s**; the
production build and `git diff --check` pass. Logs:
`/private/tmp/vonk-web-receipt-followup-suite.log` and
`/private/tmp/vonk-web-receipt-followup-build.log`.

A separate read-only W01 audit found no concrete consent bypass in enrollment,
re-enrollment/revocation, profile load/cancel/save/import, model/recipe/artifact
cancellation, explicit artifact submission, or `update --apply`. Four existing
focused fake-client cases passed in the agent checkout. Profile definition edits
and imports do not load workloads; the signed updater's explicit apply remains
its authorization. This audit does not close the remaining owner-bound removal
protocol or replace final combined qualification.

### Build cancellation after process death — 2026-09-24

The integrated `test_build_cancellation_process_recovery.py` exercises actual
PostgreSQL, managed storage, cancellation admission and a reconstructed worker.
The first process exits immediately after committing cancellation and exact
cleanup ownership. A fresh process finds the same cleanup child and retains
capacity until its exact receipt arrives. Late success cannot publish cancelled
output; replayed old success and cleanup cannot release a fresh request's claims.
The new regression and existing cancellation recovery module pass together:
**12 passed in 11.55s** in the integration candidate.

This supplies the E5e process-restart evidence. Agent cleanup results remain
deterministic test inputs, so it does not establish physical Spark acceptance.
Common admission lock order, removal coordination, publication cancellation,
memory accounting and the full combined gates remain open. The preceding
combined Python type check passed with one reviewed exception and no unlisted
errors; it preceded this test-only integration.

The integrated runtime authorization regression also passes against PostgreSQL
(**1 passed in 4.13s**). It uses the real application and signed viewer/operator
tokens: missing authentication and viewer permissions refuse artifact create,
submit and cancel before durable owner effects. An operator then succeeds with
the same request keys. This closes the runtime authorization test gap; the
existing web caller's request-key repair and final W16 gates remain open.

### Image publication guard integration — 2026-09-24

The candidate now guards exact archive publication with a nonblocking managed
storage lock. The availability owner records a canonical, attempt-bound
reference before publication, and cancellation recovery reacquires the same
guard before releasing that reference. Cached and source-build publication use
the same callback boundary. Contention retains the durable Skopeo blob cache
and waits without consuming transfer retries.

The four focused fast modules pass (**96 passed, 16 lane cases deselected**,
2.60s), including the prior late-cancellation regression. The coordination
scanner passes with zero reviewed sites. The broader PostgreSQL/process pair
reported **13 passed, 1 failed**: replacing an expired claim after publication
leaves its prior attempt's reference attached, preventing the new owner from
adopting verified bytes. This is a real recovery gap under repair, so the new
publication boundary is not fully qualified. Three type-narrowing issues found
by the combined gate were corrected; the full Python type gate then passed
with one existing reviewed exception and no unlisted errors. Changed-file
Ruff/format checks and `git diff --check` pass.

The takeover defect was then corrected: under the exact archive publication
guard, a current claim can transfer an older reference only when operation,
recipe revision, archive/image digests and byte count all match. Other identity
mismatches remain refusals. The full PostgreSQL/process pair now passes
**14 tests in 6.27s**, including the formerly failing receipt boundary and an
assertion that reference ownership moves before authorization. The combined
Python type gate and changed-file lint/format checks pass after integration.

Abrupt death can also leave an anonymous temporary export file. Durable blobs
remain reusable, but this increment does not prove recovery or cleanup of that
temporary file. Run/Switch reference binding and removal coordination remain
separate unfinished work.

### Conditional memory admission integration — 2026-09-24

Run/Switch review no longer feeds a stopped workload's peak reservation into
the capacity planner as measured released memory. When only known memory
capacity blocks a placement, each insufficient target/pool must have an exact
reviewed active stop claim and demand must fit physical total capacity. The
review then carries a canonical conditional check with the exact stop IDs,
without claiming after-stop headroom. The CLI explains that fresh capacity
must pass after these authorized stops, before preparation or start. The
existing exact-stop inventory gate and lifecycle admission remain in force.

The seven-file increment was merged against its original base, preserving the
current stale-review handling, published-image recovery and post-stop gates.
OpenAPI and Python/TypeScript clients were regenerated together. Parent
verification passed **124 fast tests** (17 lane cases deselected), **17
PostgreSQL cases** (124 fast cases deselected), and **50 resource/claim/build/
post-stop cases** in separate runs. The web production build passed.
Python types pass with the one reviewed exception; coordination checks report
zero reviewed sites. OpenAPI/client checks pass (**20 tests**). Formatting and
lint pass after preserving and formatting the merged current-source changes.

W09d C/D remain open: retained workloads can still be charged both their actual
physical use and their full peak promise. Neither sequential telemetry nor
unverified memory attribution is integrated. The next design audit must identify
real enforcement and ownership capable of admitting fitting work without
overcommit. The conditional check is truthful replacement sequencing; it does
not resolve this retained-workload accounting requirement.

### Installed observer interrupt and reconnect — 2026-09-24

`test_profile_follow_interrupt_installed.py` sends a real SIGINT to the built
wheel's executable after it has observed a pending PostgreSQL-backed
application. It verifies exit 130, one JSON interruption receipt with the exact
request/application identity and reconnect command, then reads the same owner
from a fresh process. Every observer request is GET and the durable owner state
is unchanged. Parent verification: **1 passed in 6.79s**. An initial test used
an empty profile, which correctly finished before interruption; the fixture was
changed to an actual pending application. No product defect was reproduced.
This is process-behavior evidence, not the still-unperformed operator walkthrough.

The subsequent full Controller fast tier passed **2,404 tests, 3 skipped** in
106.19s with the combined conditional-memory and publication-takeover source.
Log: `/private/tmp/vonk-cli-control-conditional-checkpoint.log`. This replaces
the earlier failing fast checkpoint for these integrated changes; it precedes
the pending Run/Switch publication-reference and existing web request-key
increments. PostgreSQL/process results remain the separately named runs above.

The standalone root/CLI environment also passed **1,005 tests, 11 skipped,
45 subtests** in 22.88s. Log:
`/private/tmp/vonk-cli-root-conditional-checkpoint.log`. It uses the standalone
recipe-contract dependency environment, rather than relying on Controller
dependencies to mask missing CLI packaging requirements.

### Run/Switch image publication references — 2026-09-24

The current Run/Switch producer now records an exact canonical image reference
in its owning job progress before managed publication. The short, nonblocking
transaction checks current target/workload ownership, phase checkpoint,
cancellation, approved recipe/image/build identities and accepted profile
intent while the exact archive publication guard is held. Reference scanning
validates this owner-bound record; availability's separate current-attempt
reference path is preserved. The normal phase writeback reloads durable progress
and retains the reference instead of overwriting it with the earlier snapshot.

Parent verification passed **13 PostgreSQL/execution-path tests in 7.78s**,
covering prepublication intent, cancellation, supersession, a removal fence and
retry after contention. Complete OpenAPI and Python/TypeScript clients were
regenerated; web build, Python types (one reviewed exception), generated-client
round trips, lint and coordination checks pass. A follow-up audit found that
explicit operation retry copies the prior job's reference owner identities;
its regression and correction are still in progress. This publication seam is
therefore integrated but not yet fully qualified. Removal and orphan-stage
recovery remain open, as do the maintenance-consent and receipt-binding fixes
identified by the current CLI audit.

The explicit retry follow-up is now integrated: validate the previous canonical
reference, reopen the exact archive reference gate in the new admission
transaction, and rebind operation/request/actor/workload ordinal to the new
job while preserving artifact identity. The three connected modules pass
**16 tests in 8.13s**. Coverage includes the reproduced stale-owner retry,
phase-writeback preservation, interruption followed by a reconstructed service,
and cancellation after publication. Combined Python types and coordination
checks pass; removal and storage-stage recovery remain separate open gates.

### Fleet upgrade consent — 2026-09-24

`fleet upgrade` now requires explicit consent within the same command. A
terminal prompts for the selected scope; noninteractive/JSON use requires
`--yes`. Single-Spark friendly selectors resolve to the canonical identity
before submission. `--all` preserves the existing all-current-Sparks request
intent, and the accepted receipt binds the actual target list. Original-key
recovery guidance includes the required consent flag.

The combined CLI/process group passes **111 tests in 7.86s**. Installed
upgrade, resume and runbook-parser acceptance pass **3 tests in 10.10s**;
the installed upgrade case now first proves refusal without `--yes`, no API
call and no durable job, before testing authorized submission and response
loss. Python types, changed-file lint/format and coordination checks pass.
Fleet removal confirmation and cache-removal identity/impact remain separate
unfinished parts of W17.

The fleet-removal and cache-receipt CLI increment is now integrated. Fleet
removal resolves and validates a stable Spark ID before confirmation, sends
the mutation to that exact ID and checks the receipt's action/identity.
Model/recipe removal validates the canonical response and submitted action,
selector and request key before following its operation. Parent CLI/process
verification passes **119 tests in 7.39s**, and the full Python type gate passes
with one reviewed exception. Current runbook and agent-guide noninteractive
upgrade examples include `--yes`. Reviewed removal impact and backend
actor/selector/model-retention replay binding remain open; this CLI fix does
not establish the missing owner contract.

After both consent increments, the standalone CLI/root fast suite passes
**1,016 tests, 11 skipped, 45 subtests** in 22.81s. Log:
`/private/tmp/vonk-cli-root-consent-checkpoint.log`. This is the current root
checkpoint; storage-checkpoint and signed-memory-floor changes are still in
isolated agent work and are not covered by this result.

### Existing artifact web caller contract repair — 2026-09-24

The existing artifact workspace and client now pass caller-owned create,
submit and cancel request keys, use exact request lookup, retain keys across
ambiguous retries, and skip inputs already attached with matching file evidence.
Upload cancellation no longer swallows an uncertain Controller response. This
is the minimal existing caller integration for the shared CLI/API contract;
the broader web redesign remains deferred.

Parent verification: **13 targeted web tests passed**, and the production
TypeScript/Vite build passed. Review identified three follow-ups before this
slice can be qualified: reject mismatched job/run identities on every receipt;
preserve explicit authorization refusal rather than report an old receipt as
success; and do not create a new draft after local abort when the original
request lookup definitively reports not found. These corrections are in
progress. The current passing tests alone do not establish these properties.

The complete web component/client suite at this checkpoint also passes:
**150 tests across 23 files** in 10.40s. Log:
`/private/tmp/vonk-web-artifact-keys-suite.log`. This precedes the identified
receipt/denial/abort corrections and does not close those gaps.

### Runtime reserve propagation gap — 2026-09-24

The source audit found the Spark helper adds a literal 4 GB to each run/job's
reserved demand, while Controller admission derives the recipe's declared
reserve. The accepted plan records `memory_floor_bytes`, but current signed
start/job requests and compiled placement omit it. A 120 GB workload with a
declared 2 GB reserve and 123 GB free is admitted by the Controller and refused
by the helper. The implementation plan now names the producer/consumer and
boundary-test changes required to propagate the same accepted floor. This fix
is underway; it is distinct from unresolved retained-workload accounting and
does not establish hardware enforcement of memory budgets.

The removal audit also confirmed an owner-side replay gap: recipe removal
returns a same-key result without comparing selector, actor or model-retention
choice, and its stored payload does not contain `with_model`. W17 now records
the canonical intent/response change and regression required to close that
gap. The pending CLI-only fix checks the identity fields already supplied;
it cannot prove the missing choice. The destructive remover remains unchanged
and unqualified, with its earlier approval boundary still unresolved.

Combined recovery now carries the original approved runtime identity through
missing-source repair while deriving current model evidence; model or image
drift still refuses replacement. Read-only recovery evidence grants no active
capacity authority. The existing receipt refresh also preserves the exact
parent claim exclusion, so a child does not compete against its own reserved
disk. Settled capacity contention no longer remains displayed as a live blocker.
An unissued build removed from cache is retired with a canonical, re-plannable
request, while issued work retains cancellation/cleanup ownership.

Evidence for this candidate (not deployment or physical acceptance):
- Protocol tests: 340 passed before the feature integrations.
- Full Controller fast tier after W11 and current generated contracts: **2,371
  passed, 3 skipped**, 78.47 seconds. The earlier four packaging failures are
  resolved. This precedes the pending W10/W12/W13/W14 increments.
- Standalone root fast tier after W11: **961 passed, 11 skipped, 45 subtests
  passed**, 25.16 seconds, using the pinned root environment and recipe contracts.
- Combined source/published recovery and identity group: 29 passed; an added
  model-artifact drift case passed separately.
- Fleet, removal and owning API/service group: 155 passed, 1 lane case deselected.
- Resource/parked PostgreSQL group on verified OrbStack: 46 passed, 3 failed;
  fixes then passed all 4 disk-claim tests, including all three failing cases.
  The dedicated combined materialized-install/inherited-claim case then passed
  on PostgreSQL, preserving old installation headroom and the exact child claim.
- Integrated CLI/artifact/client group: 107 passed; after endpoint integration,
  the CLI/client/OpenAPI group passed 100 and Controller endpoint/packaging group
  passed 95 with 2 environment failures later resolved by the packaging rerun.
- Integrated W11 cancellation: 62 CLI/artifact tests and 78 Controller tests
  passed (one process lane deselected). The agent separately passed the actual
  PostgreSQL/process/storage case on OrbStack. Regenerated schemas and restored
  the candidate editable installation after an older installed wheel was found
  in its test environment.
- Standalone built wheel: 21 offline help/version/completion combinations at
  60/80/120 columns with `TERM=dumb` and no color; Bash/Zsh syntax and closed
  pipe contract passed. Packaged cancellation schema present; no Controller or
  Pydantic dependency. Interactive and full operator acceptance remain open.
- API/Python/TypeScript clients regenerated together. Root-pinned Ruff and the
  coordination scanner pass. W15 type issues are being corrected; web and Rust
  generation checks still need the final combined source.

The candidate's isolated pinned environment is
`/private/tmp/vonk-cli-integration-env`; `control/.venv` links only to that
candidate environment. Do not commit environment/node_modules links. Integration
decisions, logs and remaining checks are recorded in
`/private/tmp/vonk-cli-integration-notes.md`.

Automatic approval review refused adding a database `cancelling` constraint for
W13 because of fresh-schema reset risk. W13 proceeds through the existing typed
progress document as the single cancellation authority, projecting cancelling
from that state. No rejected schema edit is being retried or bypassed.

## Integration checkpoint — 2026-09-24

Latest bounded acceptance increments:

- Credential-file regressions exercise the actual connection-check path for a
  missing path, a symlink and overly broad permissions. Each refuses before
  networking and keeps token contents out of output. The integrated process
  module passes 33 cases (3.20 seconds); pinned Ruff/format checks pass.
- Profile endpoint qualification now changes the real published generation
  during bundle verification while retaining the same alias and run. The
  projection refuses stale evidence; a fresh read returns the replacement
  generation. The existing route test passes (0.69 seconds). An unchanged
  republish deliberately reuses its generation, so the test changes readiness
  evidence to cause an actual replacement; no production defect was found.
- Memory work remains open. Sequential aggregate/owner reads cannot justify
  subtracting measured owner bytes: free=96 with owner use=4 can be followed by
  owner use=10, wrongly granting 96 alongside a peak claim of 10. The pending
  replacement path therefore binds exact stops and requires a fresh post-stop
  fit before dispatch, without claiming released bytes. The separate retained
  workload requirement still needs closure: free=70 with peak claim=15 and
  actual use=10 has 65 bytes of growth-adjusted headroom, while subtracting the
  full claim yields only 55. Neither an unsafe credit nor this false refusal
  closes W09d; the measurement prototype is not integrated acceptance evidence.

- Artifact submission receipt reads now validate the canonical owner-payload
  digest, exact artifact ownership and request UUID before producing a view.
  Corrupt or foreign owner state raises an explicit domain error instead of an
  attribute/validation failure or an apparently valid receipt. Five corruption
  regressions and the normal installed lost-response/replay journey pass in the
  parent service/API/installed group (63 cases, 14.99 seconds). Pinned Ruff passes.

- The Controller fast tier after artifact request identity and installed
  qualification ran 2,390 passing cases, three skips and two failures
  (83.46 seconds). One failure was the stale-plan review test's former generic
  blocked expectation: it now checks stale consent separately from a current
  blocked digest, and all nine module cases pass. The late verified image
  cancellation-retention failure remains open with the publication-lock/recovery
  work. This is not a clean full-suite gate. The log is
  `/private/tmp/vonk-cli-control-artifact-checkpoint.log`.

- The installed-wheel runbook regression passes (one case, 2.70 seconds). It
  discovers the current executable shell examples directly from the runbook,
  substitutes valid identity placeholders, and parses them with the wheel's
  isolated Python without dispatching commands or accessing the Controller.
  This upgrades the earlier source-only check of 75 examples. Pinned Ruff and
  formatting checks pass; no production behavior changed in this increment.

- Installed fleet resume passed against the actual registered route, upgrade
  owner, PostgreSQL and TLS (one case, 7.09 seconds). Two failed attempts expose
  the owner's resume action; a role downgrade at submission is refused without
  changing the job or attempts, and an administrator resumes the original job
  with its request, revision, digest and history intact. The injected clock
  crosses the actual 960-second recovery fence; no physical package is installed.
- Installed terminal qualification passed at 60, 80 and 120 columns (three
  cases, 10.00 seconds) with a long Unicode profile name, `TERM=dumb`, `NO_COLOR`,
  and redirected JSON. The real wheel reads the PostgreSQL-owned profile via
  TLS; output stays within the terminal width and JSON preserves the exact name.
  Repository-pinned Ruff and formatting checks pass after an unused fixture
  variable was marked intentionally unused. The independent walkthrough is open.

- Artifact submission receipts expose the original durable submit request key.
  Same-key retries recover one operation; a different key is refused, and the
  CLI checks the request identity after direct responses and lost-response
  lookups. Run-filtered job lists refuse a different run. Regenerated contracts
  pass 42 CLI/client tests and 58 service/API/installed PostgreSQL tests
  (13.54 seconds), including lost response, same-key replay and changed-key
  refusal. The web type/build gate also passes. Broader final gates remain open.

- Fleet resume validates its accepted job identity. Log observation resolves
  names once, pins the stable Spark ID for every response and reconnect command,
  and returns retained `follow:false` snapshots immediately. Agent regressions
  failed before the fixes; parent CLI and profile-load modules passed 95 cases.
  The process module passed 30 cases after restoring the candidate's editable
  root package (an older wheel had been loaded by subprocesses). These results
  are distinct from the preceding broader root-suite checkpoint.

- The current root fast tier passed 980 cases and 45 subtests; 14 local HTTP/TLS
  cases initially could not bind their test sockets in the sandbox and all 14
  passed on the permission-enabled retry (2.73 seconds). Eleven lane/platform
  cases were skipped. This checkpoint predates pending identity and memory
  increments. All 75 executable runbook examples also parse against current
  source after substituting concrete UUID/digest placeholders; no mutations
  were executed by this documentation check.

- Stale load admission now returns the exact `profile.stale_plan` refusal for
  changed reviewed inputs, including the final transaction fences. The CLI
  reads and displays one current review, preserves the refusal, and never
  resubmits automatically. Unrelated conflicts do not trigger a refresh. The
  integrated CLI module passed 20 tests; actual PostgreSQL API/race and installed
  CLI/TLS acceptance passed 17 tests (21.54 seconds). Full pinned Ruff passes;
  final combined qualification remains open.

- Installed Find-and-prepare acceptance passes against the candidate wheel, TLS,
  actual catalog/cache services and PostgreSQL (one case, 7.68 seconds). It finds
  a later-page model, exposes the precise repair action after verified files are
  lost, accepts preparation and reconnects by the original request key. Source
  transfer and publication are outside this case.
- W06 review found remaining unchecked resume receipts, alias-based log polling,
  retained-log termination, and artifact-job run/request identity. These fixes
  are assigned and remain open until integrated regressions pass.

- Published-image missing-cache recovery now preserves the exact approved
  platform image/layout identity and reports the registry digest separately.
  Unknown coverage remains unknown; explicit preparation intent invokes the
  shared production callback without inventing measured bytes. Changed complete
  output is refused before target copy/install. The parent focused service group
  passed 177 cases (12.97 seconds); clients were regenerated, types pass with
  one reviewed exception, and 44 rendering/client checks pass. Final combined
  qualification remains open. The accepted-profile changed-output case was
  subsequently moved onto real PostgreSQL and passed (3.72 seconds); preparation
  cannot dispatch a target when the restored output differs from approved intent.
- Installed model cancellation now passes against the integrated wheel and
  actual PostgreSQL/cache owner: an accepted cancel response is lost, exact-key
  lookup recovers it, a worker dies with verified staged bytes, and the restarted
  owner settles cancellation without publishing or losing the partial file.
  A fresh installed process reconnects by the original operation key. The case
  passed in the parent combined run. The maintenance fixture was corrected
  separately and its installed acceptance now also passes (6.55 seconds):
  an accepted upgrade response is lost, same-key recovery returns one job,
  first-target failure stays visible and prevents dispatch to the second Spark.
  External release selection is deterministic; no package was installed on hardware.
- Fleet progress now rejects a different job ID on either its first response or
  subsequent poll. Both regressions first returned incorrect success; the
  integrated CLI/process group now passes 99 cases (7.32 seconds).
- Installed profile cancellation now also covers an issued Run/Switch child:
  the worker's response is delayed after real child commit, the CLI cancels and
  reconnects while effects remain pending, and a reconstructed owner reconciles
  the late response to the same cancelled child without duplicate dispatch.
  Resource claims remain held while reconciliation is pending. Both installed
  profile cases passed on PostgreSQL (9.59 seconds). Child creation and service
  reconciliation are real; the executor is deterministic, not a Spark run.
- Installed recipe-update batch recovery passed against the integrated wheel,
  TLS and PostgreSQL (one case, 8.59 seconds). Killing the submitting CLI after
  acceptance and the worker after child commit preserves the original complete
  cache scope and adopts each child once. Recovery reconstructs the actual
  durable service; it is not a deployed Controller restart or Spark run.
- Explicit profile/application observation now verifies membership and pins the
  application identity across every poll. The agent reproduced both previous
  incorrect successes, then passed 67 CLI and 30 process cases; parent combined
  CLI/process checks passed 97 cases in 7.12 seconds.
- Profile cancellation is now visible in Activity with its exact request key,
  actor and effect reconciliation. Display and SQL filtering share the same
  pending-state predicate. The integrated API/profile group passed 47 cases and
  CLI-render/OpenAPI group passed 34; three PostgreSQL recipe/profile cases passed
  in 8.92 seconds. A separate installed CLI/TLS/PostgreSQL journey lost an accepted
  cancellation response, recovered its receipt and reconnected through Activity
  until settlement (one case, 7.53 seconds). These cases cancel before dispatch;
  they do not establish physical Spark cancellation.
- Non-destructive artifact reference gates are integrated across reference
  producers. A stale ORM-session read regression is covered by the agent's
  PostgreSQL tests. Parent combined qualification is running. The existing
  destructive remover does not yet participate, so W09e/W17 remain open and no
  safe removal or garbage-collection completion is claimed.
  The shared Run/Switch image preparer also authorizes its receipt after
  storage commit and currently has no exact operation-owned reference before
  that commit for a previously unknown output. This producer handoff must join
  the same lifetime protocol before W09e can close; a passing availability
  cancellation check alone will not close it.
- Combined Controller fast tier at snapshot `802bab02`: 2,387 passed, three
  skipped and one outdated scheduler-double failure (105.67 seconds). The double
  now implements the required cancellation reconciliation seam; its whole module
  passes ten cases with two lane skips. The standalone root/CLI fast tier passed
  983 cases, 11 skips and 45 subtests (27.01 seconds), including socket-dependent
  checks with local networking available. These precede pending memory/lifetime/
  Activity integrations and are not the final combined gate.
- Installed first connection and profile endpoint publication/withdrawal now
  pass together against the current packaged schema (two cases, 9.62 seconds).
  Withdrawal is validated through the authoritative typed response, which
  permits omission of an unused optional endpoint; no production change was
  needed for that fixture correction.
- Distinct-build replacement exposed two selection defects. Fresh Run/Switch
  review preferred the old installed build over the newer completed receipt;
  after acceptance, changing build recency could make profile dispatch incorrectly
  become a no-op. Fresh selection now chooses the current completed receipt,
  while accepted dispatch and recovery compare the exact reviewed image. Both
  running-replacement cases passed (16.79 seconds); the subsequent accepted-pin
  regression passed (11.12 seconds). The affected profile/RunSwitch fast group
  passed 124 cases, and profile/recovery checks after the dispatch fix passed
  65 cases. Ten process-recovery cases also passed during this increment; final
  combined validation remains pending.
- Installed artifact-job acceptance is integrated: a dropped committed submit
  response recovers the same job/request; corrupt output is not published locally,
  and verified retry succeeds. The PostgreSQL/TLS/wheel case passed (9.68 seconds).
  The result producer is simulated through the service boundary; this is not
  agent or Spark execution evidence. Shared TLS changes also passed four existing
  enrollment/load cases.
- Recipe/model cancellation now shares W11's durable owner path. The parent
  waits while its child writer holds the artifact lock, a new request cannot
  attach to that fenced child, and process death/restart settles the original
  intent without deleting partial bytes. The combined real PostgreSQL seam and
  shared-build consumer group passed sixteen cases (20.46 seconds). The three
  older fixtures now provide canonical durable child records; their full module
  passed 36 cases with one lane case deselected. Type narrowing is repaired and
  the whole candidate type gate again has only the existing reviewed exception.
- Exact profile-cancellation receipt lookup is integrated and generated into
  the complete API/clients. Current role and original actor authorize recovery;
  lost responses retain the application and cancellation key. Three service/API
  cases and 86 CLI/cache-submission cases passed. Activity projection and installed recovery now pass the subsequent checkpoint below.
- Independently installed enrollment passed two real TLS/PostgreSQL cases
  (9.31 seconds), including a committed grant whose response is lost. The grant
  stays private, issuance occurs once, and a new process observes/revokes the
  original identity without printing the secret. Certificate issuance and
  physical Spark enrollment are outside these checks.
- Installed CLI load/authoring/consent: six PostgreSQL/TLS cases passed in
  16.58 seconds. Preserving edits and concurrent-write refusal, redirected input,
  EOF and Ctrl-C are exercised through the independently installed executable.
- Closing stdout after an accepted load and dropping its response exits 141;
  a new process reconnects with the original key to the single application.
  The PostgreSQL case passed in 7.14 seconds.
- A late parked-application race reproduced cancellation being overtaken by
  ordinary parked observation. Pending cancellation now stays with its dedicated
  observer; twenty related fast cases passed with two lane cases deselected.
  These fairness tests do not claim PostgreSQL concurrency qualification.
- A running-image replacement review binds the current receipt, stops the exact
  old run, and a separate worker process installs and publishes the replacement
  while retaining both cached archives. The PostgreSQL/storage case passed in
  13.83 seconds. Spark effects and physical inventory are deterministic fixtures;
  distinct build-record and concurrent lifetime cases remain open.
- All ten existing build-process recovery cases passed after the shared harness
  changes (82.50 seconds). Python types still report one reviewed exception and
  no unlisted errors. Full final combined gates remain pending.

The candidate now includes W10, W11, W13, W14, W15, W16 and the fleet-maintenance
part of W17, with W12 partially integrated. Measured memory, artifact lifetime, cancellation fairness,
remaining lock-order work and installed end-to-end workflows remain active.
No package closure, deployment or hardware acceptance is implied.

- Installation admission now maps contention on all its dependency locks to
  the existing retryable `InstallAdmissionBusy` boundary. Held mapping/node/build
  tests first failed with raw SQL errors, then all eight PostgreSQL disk/claim
  tests passed. Multi-node lock sets are ordered; the full writer audit remains.
- Run/Switch waits for physical inventory collected after the exact stopped
  run, retaining the admitted agent-clock uncertainty. The PostgreSQL regression
  first queued a replacement using old inventory; it now survives service
  restart, refuses the exact skew boundary, resumes on fresh evidence, and
  retains the original child identity. Build adoption precedes this check.
  The one- and two-node PostgreSQL cases now both pass (5.44 seconds); updating
  only one node keeps a two-node replacement waiting. Broader preparation/process
  cases and measured-use accounting remain.
- W14 integration retained newer upstream launch-budget/retirement logic.
  Added failing cases for another target's newer intent and node revocation;
  both now remove/refuse resume. Combined Activity/API/agent tests: 148 passed.
- W10/W14 combined CLI group: 86 passed. W15 route/API group: 39 passed,
  5 skipped. W13 profile/API group: 56 passed, 3 lane cases deselected;
  the isolated W13 real PostgreSQL late-start race also passed.
- Python type gate now passes with exactly one existing reviewed exception and
  no new errors. Pinned Ruff and the coordination scanner pass. Combined broad
  Controller run reported 2,378 passed, 3 skipped and two integration regressions.
  Both were repaired, and their two modules then passed all 66 tests; a final
  combined rerun subsequently passed 2,377 tests with three skips; its three
  packaging subprocess failures all passed when rerun with the required writable
  task-specific cache (3 passed in 3.61 seconds). The standalone root run passed
  959 tests with 11 skips
  and 45 subtests; its 14 socket-restricted cases subsequently all passed with
  local networking enabled. The TypeScript production build passed.
- Automatic review rejected the separate run-admission production lock-order
  edit as insufficiently authorized. That agent is producing a test-only
  regression and read-only dependency audit; the rejected edit was not retried.

W12 integration retained the newer model cancellation settlement boundary and
Activity projections. The independent patch passed 83 PostgreSQL-backed tests
and 46 CLI cases; the candidate's combined CLI file now passes 59 cases. Current
recipe/API/batch checks passed 39 cases with five lane skips. Generated clients
were refreshed. The W11/W12 transactional model-child seam remains open, and
Python types now pass with the one existing reviewed exception and no new
errors; these are not final combined gates. A failure-first PostgreSQL race also exposed
build cancellation losing the parent identity before its child link was saved.
The corrected lookup passes all 15 shared-consumer tests, including both linked
and unlinked children (16.87 seconds).

Further integration evidence: W13's bounded cancellation observer retains the
newer parked-observation and cache-recovery deferral paths; 19 related fast tests
pass (two lane cases deselected). The independently built CLI's real TLS/API/
PostgreSQL review, refusal and lost-answer scenarios plus profile cancellation
pass together: five tests, 10.50 seconds. A refused interactive load no longer
prints reconnect guidance before a submission exists. W09f review now exposes
archive SHA, size, architecture, runtime interface and build identity alongside
the image digest; a stale interactive review displays the changed archive and
submits nothing. The combined process/review/render group passes all 60 tests.

Cancellation recovery follow-up: a failure-first CLI case proved recipe
cancellation's uncertain-response guidance used the cancellation key to look up
the original preparation request. Guidance now retains the exact operation ID,
cancellation key and reason. The 26 cache-submission/profile-cancel checks pass
(one lane deselected), including another administrator's refused key replay and
the original actor's refused replay after revocation. Python types passed after
the fairness and installed-harness integration; the final full gate remains.

W18 documentation/packaging checkpoint: the refreshed runbook includes explicit
model/recipe/profile cancellation, Activity/resume, endpoint discovery, and the
sequential upgrade boundary. All 75 shell examples parse with the independently
installed wheel (UUID placeholders replaced, no mutations executed). The wheel
passes 27 offline entry-point checks across 60/80/120 columns and has no
Controller/Pydantic dependency. Bash/Zsh completion scripts pass shell syntax.
An actual closed pipe reproduced help exiting 120 during interpreter shutdown;
help now flushes inside the CLI's pipe boundary. Both top-level and subcommand
help in the rebuilt wheel exit 141 without stderr; all 30 process tests pass.
Connected and final combined qualification remain open.

## Current surface and owners

Reads use authenticated Controller routes; mutations use the owning route's
`auth.MUTATION_ROLES` policy. Local flags never supply authority. Request and
response structures are validated from the generated current OpenAPI.

| CLI operation | Current transport / owner | Identity, side effect and observation |
| --- | --- | --- |
| `fleet`, `fleet detail` | `GET /api/fleet`, `/api/fleet/{selector}`; FleetProjection | Read exact node/roster projection; filtered bounded watch |
| `fleet node-profile` | Roster selection, then `GET /api/fleet/{id}` | Read only; focused identity/lifecycle/labels in human output, complete canonical detail in JSON |
| `fleet rename` | `POST /api/fleet/{selector}/rename` | Exact enrolled node; changes friendly name |
| `fleet enroll`, `re-enroll` | `POST /api/fleet/enroll`, `/api/fleet/{selector}/re-enroll` | Required private output; caller UUID4 is durable grant identity; re-enrollment resolves and confirms the exact node |
| `fleet enrollment status`, `revoke` | `GET /api/fleet/enrollments/{id}`, `POST .../{id}/revoke` | Administrator and original issuer only; status contains no secret; revoke refuses a consumed grant |
| `fleet remove` | `POST /api/fleet/{selector}/remove` | Node revocation/removal; exact resolved identity and explicit scripted/interactive consent; receipt identity checked |
| `fleet upgrade` | `POST /api/fleet/upgrade`; AgentUpgradeService | One-at-a-time signed upgrade; stable UUID request replay reconnects to the original job and follows its exact ID |
| `fleet progress JOB_ID` | `GET /api/jobs/{job_id}` | Exact job snapshot; `--follow` awaits its outcome |
| `fleet loginfo` | Resolve friendly selector once, then `GET /api/fleet/{node_id}/loginfo`; fleet log owner | Exact node checked on every response; bounded follow/reconnect by stable ID; retained snapshots return immediately; no SSH |
| `fleet activity` | `GET /api/operations`; durable operation projection | Authenticated read; owner-derived state/actions and complete identities; keyset cursor retains request/state/target filters |
| `fleet resume JOB_ID` | Exact `GET /api/jobs/{id}`, then `POST /api/jobs/{id}/resume`; owning recovery service | Operator/administrator; advertised action and current authority rechecked; explicit consent; returned ID must match; follow separately with `fleet progress` |
| `model`, `recipe` | `GET /api/model`, `/api/recipe` | Controller cache/operation views; bounded watch |
| `model library`, `recipe library` | Singular noun `/library`; LibraryProjection | Page/cursor and task facets; public catalog differs from cached availability |
| `model detail`, `recipe detail` | Singular noun `/{selector}` | Exact selector/detail; technical option and bounded watch |
| `model download`, `recipe download` | Singular noun `/{selector}/download` | Schema-2 request key; follows noun operation or detaches |
| `recipe update SELECTOR` / `--all` | `POST /api/recipe/update`, recipe request/operation reads | Durable frozen scope; follows by default; original-key recovery and explicit empty success |
| `model cancel ID`, `recipe cancel ID` | `POST /api/{noun}/operations/{id}/cancel`, exact operation/cancellation receipt lookup; cache owner | Operator/administrator plus owner rules; consent and original cancellation key/reason; accepted cancellation may retain pending effects and usable assets |
| `recipe job list --run` | `GET /api/recipe/runs/{run_id}/artifact-jobs`; ArtifactJobService | Authenticated read; every returned job must belong to the requested run |
| `recipe job create` | Run-scoped artifact-job POST plus exact request lookup; ArtifactJobService | Operator/administrator; durable create key and exact declared inputs; reserves/uploads/finalizes a draft, never submits execution |
| `recipe job upload` | Job GET, input PUT, `/finalize` POST; managed input owner | Operator/administrator; exact draft/binding; reuse completed verified files and upload missing files |
| `recipe job submit` | Job `/submit` POST, job GET for uncertain receipt | Operator/administrator; original durable submit key and operation must match; changed key refused |
| `recipe job detail` | `GET /api/artifact-jobs/{id}` | Authenticated exact-job read; optional bounded follow |
| `recipe job cancel` | Job `/cancel` POST and exact receipt read | Operator/administrator; explicit consent/key/reason; cancellation remains distinct from output cleanup |
| `recipe job download` | Job `/result` metadata and `/results/{name}/{sha256}` bytes | Authenticated read plus local file writes; bounded manifest/path/digest validation before publishing result files |
| `model remove`, `recipe remove` | Singular noun `/{selector}/remove` | Explicit cache eviction; recipe dependency choice; not cancel-only |
| `model progress ID`, `recipe progress ID` | Singular noun `/operations/{operation_id}` | Read succeeds independently of remote state; follow awaits terminal result |
| `model progress --request-key`, `recipe progress --request-key` | Singular noun `/requests/{request_key}`, then the exact operation route | Original issuer plus current read access; key resolves once; no authority or ID visibility change |
| `profile`, `profile list` | `GET /api/profile/{number}`, `/api/profile` | Stable numbered profiles; reads may default visibly to 1 |
| `profile name`, `add`, `remove`, `configure` | `GET /api/profile/{number}/definition`, then numbered `PUT` | Complete saved definition; explicit selection; observed revision; omissions preserve intent |
| `profile export`, `import` | Definition GET / numbered PUT | Export contains authoring fields only; private exclusive file or stdout; bounded import requires an explicit revision and never loads |
| `profile load --dry-run` | `POST /api/profile/{number}/preview` | Read-only review; blocked preview exits 2 |
| `profile load` | `POST /api/profile/{number}/load` | Required reviewed digest and request key; current authority and original-request replay; remaining workload/resource/storage coordination in W09 |
| `profile progress` | Numbered latest/request lookup, then `/api/profile/applications/{id}` | Resolves latest once; follows exact identity; direct application checks profile membership |
| `profile cancel APPLICATION_ID` | `POST /api/profile/applications/{id}/cancel` and `/cancellations/{key}`; FleetProfileService | Explicit profile membership, operator/administrator and original intent; exact cancellation receipt; pending effects remain visible |
| `profile endpoint [ALIAS]` | `GET /api/profile/{number}/endpoints`; profile endpoint projection over published routes | Authenticated current membership and publication; alias narrows scope; exact run/generation/expiry retained, no cache inference |
| `--check-connection` | Local token/origin checks, then `GET /api/fleet` | Read only; no configuration or credential issuance |
| No command, help/version, `completion` | Local parser/build metadata | No credentials, remote lookup, or update check |
| `update`, `update --apply` | Signed installer publication; CLI updater | Checks or installs the CLI wheel, independently of Controller/fleet upgrade |

The live parser owns flag names and defaults; `vonkctl <command> --help` and
parser-generated completion expose them offline. The installed runbook check
verifies the documented examples against that parser without dispatching work.
The table maps behavior and authority, rather than maintaining another parser.

Web methods in `control/web/src/api/client.ts` map to the CLI as follows:

| Web operator task | CLI mapping / explicit boundary |
| --- | --- |
| Fleet state, profile list/detail/save/preview/load/progress | Fleet and Profile commands above; browser session/CSRF differs from bearer authentication |
| Model/recipe library, status, detail, prepare, remove, update and progress | Matching singular Model/Recipe commands; same owning contracts |
| Activity and resumable job detail | `fleet activity`, `fleet progress`, `fleet resume`; CLI has no separate raw audit-export or generic operation-detail command |
| Artifact draft, inputs, submit, cancel and results | Seven `recipe job` leaves above; existing web callers now pass stable request-key headers and reconcile receipts. Exact receipt identity, explicit denial and abort-recovery checks pass, including the full 163-test web suite. |
| Published profile endpoints | `profile endpoint`; current web API wrapper has no equivalent profile-scoped method, so no completed web parity is claimed |
| Browser sign-in/out and CLI token download | Explicit bootstrap dependency, not CLI session/password administration |

The CLI implementation does not claim the later web redesign is delivered.

`AgentNodeProfile` and `FleetProjection._node_profiles` own the node identity
and lifecycle projection. They do not represent a whole-fleet workload profile.
`fleet node-profile` now exposes that node-owned projection after exact
selection, without restoring `fleet profile` or using workload-profile endpoints.

## Verification log

### W09f new image approval checkpoint — 2026-09-23

Working-tree evidence on HEAD `38c2018e5ca69c49356d8b19722f8265759ecc08`.
This qualifies explicit reapproval after a changed source rebuild for a first
installation and an installed-only replacement. W09/W10 remain open.

The three existing changed-output subprocess cases now continue after the old
review is correctly refused. A new review and request must reserve installation
capacity, produce a new installation, start the approved image and publish its
route. Assertions inspect both the stored compiled plan and the actual typed
`recipe.start` agent payload, retain the old installation unchanged, and verify
handoff of the original accepted disk-claim IDs to the new installation.

Before the fix, the two installed cases accepted the new request without a disk
claim. Profile state and Run/Switch now share
`installation_matches_runtime_image`: a mutable build ID and installed state
are insufficient for reuse. The stored mapping, generation, recipe, plan and
every compiled rank must agree with the selected build/image/archive/size.

That correction exposed a second real failure: a larger replacement archive
was refused at installation because review still used the recipe's smaller
estimate. Review now uses the observed image/model sizes and the same disk
envelope arithmetic as installation, including rollback space and the configured
reserve. The SQL-only admission recheck consumes the reviewed preparation sizes;
it performs no new storage I/O. Review promises a full allocation with headroom;
installation may reduce that promise for exact target-local reuse, and cannot
enlarge it after acceptance.

Verification:

- All three changed-output reapproval cases passed in 32.64s. The broader
  connected group passed 99 tests, including all ten process cases, and found
  one old profile fixture with an empty installation plan. The fast tier passed
  2,275 tests with 3 skips and found the same fixture failure. These counts
  overlap; neither initial group was wholly green.
- The reuse fixtures now contain canonical stored installation plans. The
  complete profile file then passed all 48 tests in 7.85s, including its two
  PostgreSQL cases. No remaining observed failure is being deferred.
- Root Ruff, seven touched-file format checks, Python types (one existing
  reviewed exception), coordination boundaries, web production/type build and
  generated Rust wire check passed. `git diff --check` passed. The curated
  manifest was regenerated and verified with digest
  `f931cc4648f273c8162c22e3adae066d6ee4a4babc8283a36536d5822943d1d8`.

The process tests use real PostgreSQL, process restart and managed image files,
with simulated Spark/model-transfer/publication adapters. There is no new
deployment or physical qualification claim. Running-workload replacement,
published-image storage recovery, distinct build-selection identity, reference
lifetime and simultaneous replacement still require their own gates. The
parallel admission audit is checking those boundaries and newer upstream fixes.

### W09f restored-image binding checkpoint — 2026-09-23

Working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`, branch
`codex/cli-operator-experience`. W09/W10 and the complete implementation remain
open; this closes the reproduced same-image installation-reuse failure and
adds refusal of a changed rebuild under the old review.

The new process cases reproduced four failures before the fix. Same-image
restoration lost the accepted installation and failed for a missing disk claim.
A first installation silently accepted a different rebuilt image. Changed-image
and changed-archive installed cases reached the incidental disk-claim refusal
instead of enforcing the original image identity.

The profile's existing approved preparation decision now supplies the image
identity. Its authority, exact assignment and current workload ordinal are
validated before use. For automatic recovery, the existing intent validator
checks the direct reference and digest of the original review; the retry's
remaining-work plan is not a new approval. The same lookup supplies temporary
build-memory inheritance. No new registry, table or public contract was added.

Run/Switch preserves an installed source-build plan during repair only when its
selected build and every compiled rank match the accepted image. It restores
the Controller archive without queuing a new installation. Completed build
receipts and runtime-image preparation are compared with the original image,
archive, size and build identity before advancement. Full prepared receipts
also check architecture and runtime interface. A mismatch reports
`profile.runtime-image-changed`, names the changed fields, and asks for a new
review; no new runtime starts and the existing installation plan is retained.

This comparison also exposed incorrect preparation metadata: the projection
used the serving adapter (for example `openai`) as the runtime interface.
Preparation now uses the same `RUNTIME_INTERFACE` owned by the runtime compiler,
so approved identity agrees with compiled plans and managed image receipts.
No alternate reader accepts the old, incorrect value.

The four new process regressions passed after these corrections. The first
connected profile/capacity/build/cancellation/Run-Switch group passed **160 tests**
in 222.96s. The initial fast tier found one automatic-retry regression among
2,275 passing tests: reading the retry's unavailable preparation lost the
original image. The direct original-review lookup fixed it, and all **eight
cache-recovery tests** passed.

Final qualification:

- The combined PostgreSQL ownership/process/cancellation and cache-recovery
  group passed **96 tests** in 204.62s after the original-review fix, including
  all ten subprocess cases. The Controller fast tier passed **2,276 tests**,
  **3 skipped**, in 90.02s. Counts overlap with the earlier runs.
- Root Ruff, all four touched-file format checks, Python types (one existing
  reviewed exception), coordination boundaries (zero reviewed sites), the web
  production/type build and generated Rust wire check passed. `git diff --check`
  passed and **30 local file links** in the plan/status documents resolve.
- The curated manifest was regenerated after the production changes. Offline
  verification passed with digest
  `3f343abebc6336d14450d29c0532cddcf4d04532fbe7c96f5e191b39278c96c7`.
  No commit, publication, deployment or physical acceptance occurred.

Remaining W09f work includes accepting and executing a **new review** after a
changed image: replacement installation, correct capacity, any running workload
and route effects, already-cached replacement and published-image recovery need
their own connected gates. Root review/reference lifetime and simultaneous
replacement remain open. The process tests use real PostgreSQL, process death
and managed image files, with simulated Spark/model-transfer/publisher evidence;
they are not native builder, deployment or physical Spark acceptance.

### W09d profile build-to-runtime process checkpoint — 2026-09-23

Working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`, branch
`codex/cli-operator-experience`. This adds process-boundary evidence for E6;
W09/W10 and the full CLI implementation remain open.

`test_profile_build_process_recovery.py` starts from an accepted profile that
needs its first installation, removes the actual Controller archive and receipt,
and runs a separate worker process against PostgreSQL. The worker exits with
`os._exit` either after flushing a child transaction but before commit, or after
the lifecycle commit but before its parent checkpoint. Both boundaries are
covered for build and runtime start. Two more cases complete the committed
child while no parent worker is running, then reconstruct all worker services.

All **six cases passed** in 40.39s. An uncommitted child, agent order and claim
handoff roll back together. A committed child retains its identity and is
adopted, including an already completed child. One synchronous Controller worker
loop reaches exactly one successful build, installation and start, a running
recipe with a published route, and a successful profile. The original profile
memory claim IDs survive and become active run claims; temporary build claims
are released. A subprocess timeout catches a parent that blocks the only loop
needed to advance its child.

The test uses real SQL transactions, process termination, source/archive files,
managed image receipts, the production image-preparation adapter, lifecycle
admission and the route service. Spark execution, preflight findings, prepared
model coverage, target-copy results and the route publisher remain deterministic
test adapters. This is not complete production composition, actual model
transfer, native build, deployment or physical Spark acceptance. It does not
close simultaneous cancellation/admission, the F writer audit, or all E6 commit
boundaries.

The initial installed-reuse variant exposed a separate open admission finding:
after acceptance without a new disk claim, cache loss can turn reuse into a
runtime-plan preparation phase. Rebuilding the same image then reaches
`profile disk claim is missing or changed`. The failure log is
`/private/tmp/vonk-profile-build-process-preparation.log`; reproduce by using
`initially_installed=True` in the process fixture. The passing six-case matrix
uses `initially_installed=False` and does not resolve or conceal that finding.
W09d/W09f must establish reuse of the exact restored installation, or an explicit
new review when effects or capacity change. Creating a claim after acceptance
or silently binding a different rebuilt image is not an acceptable fix.
The later restored-image binding checkpoint above resolves the same-image
failure and adds exact changed-output refusal; it leaves new-review execution
and the remaining admission gates open.

Earlier harness failures were incomplete fixture boundaries (missing probe
answers or unbound synthetic image evidence), not reproduced production bugs.
They are kept distinct from the installed-reuse finding. Connected ownership
validation passed **78 tests** in 109.27s, covering profile/build memory,
installed execution, disk/port/runtime handoffs, build fences and shared
cancellation. The six process cases ran separately. The engine was verified
as `orbstack` / `OrbStack`.

Root Ruff, both touched-file formatting checks, Python types (one existing
reviewed exception), coordination boundaries (zero reviewed sites), the web
production/type build, generated Rust wire check and `git diff --check` passed.
All **29 local file links** in the plan/status documents resolve. Offline
supply-chain verification passed with unchanged manifest digest
`315fb61e44bf1265820481ab06cf1f9e1ead979b8d587d6cf860ddb9e3465bad`.
This checkpoint changes test fixtures, process tests and planning documents;
it adds no production behavior or public contract. The prior Controller fast
tier remains separate earlier evidence and was not rerun for this test-only
slice. No commit, publication, deployment or physical acceptance occurred.

### W09d shared-build cancellation and unbound adoption checkpoint — 2026-09-23

Working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`, branch
`codex/cli-operator-experience`. This checkpoint concerns repository behavior;
it does not close W09/W10, the public cancellation commands, deployment or
physical acceptance.

Build jobs now carry typed producer intent in their existing payload. A build
requested independently remains owned by that request when a Run/Switch
consumer detaches. A build created solely for accepted parents becomes eligible
for cancellation after its last current consumer leaves. Run/Switch detachment
and build cancellation use the same build boundary as dependency acceptance;
contention rolls back the attempted detachment. Issued work retains capacity
until its exact cleanup receipt settles it. No consumer registry, table or
public wire contract was added.

The lifecycle reconciler rotates through its existing batch boundary. Malformed
ownership refuses cancellation of that build, while subsequent passes still
reach eligible unrelated cleanup. Service reconstruction preserves independent
producer intent. PostgreSQL barriers prove that a new availability consumer
cannot join during last-consumer cancellation, and that pending issued cleanup
refuses acceptance until settlement. These checks extend the original five
reproduced failures to eight shared-cancellation cases. The shared group plus
the Run/Switch fence cases passed **21 tests**; the first connected group passed
**201 tests** before the additional adoption change below.

An additional production regression exposed an unresolved-consumer defect:
after one availability parent started a build, a second parent without a
selected builder requested new capacity before discovering that exact work.
Saturated or stale inventory left it waiting despite the active execution.
Both cases failed before the fix; the unchanged fresh-inventory case passed.
The production composition now resolves matching active executable inputs,
checks the accepted builder and recipe identity, and publishes the dependency
under the existing cancellation fence before new-build admission. Cached and
active reuse bind the canonical input intent to the recorded builder through
one digest helper. Existing child observation still runs before resolution.

The one-slot scheduler completes both parents from one real build under fresh,
saturated and stale inventory. Missing builder identity and changed recipe
content refuse adoption without changing the original execution or its claims.
The adoption/consumer group passed **22 tests**. Older synthetic composition
fixtures now provide the current typed resolution and accepted intent; all
**12 production-composition tests** passed. There is no permissive runtime
fallback for those incomplete fixtures. Type checks also caught a dictionary
expansion that could supply a string to the new boolean detachment option;
the tests now pass the exact named identity fields.

Final validation:

- The combined cancellation, profile, Run/Switch, availability, production
  composition and build group passed **217 tests** in 137.29s. The Controller
  fast tier passed **2,276 tests**, **3 skipped**, in 104.59s. Counts overlap.
  The initial fast run's two incomplete-fixture failures remain in its log;
  the corrected fixtures and the final complete run passed.
- Root Ruff, touched-file formatting, Python types (one existing reviewed
  exception; no unlisted errors), coordination boundaries (zero reviewed
  sites), and `git diff --check` passed. Web production/types and generated
  Rust wire checks also passed; this slice changed neither public contract.
  All verification process handles are terminal.
- Supply-chain generation and verification passed with manifest digest
  `315fb61e44bf1265820481ab06cf1f9e1ead979b8d587d6cf860ddb9e3465bad`.
  Both updated plan/status documents passed their local-link checks.

Logs: `/private/tmp/vonk-shared-build-cancel-{red-final,focused,expanded,
connected,types,types-final,web,wire}.log`,
`/private/tmp/vonk-unbound-build-adoption-{red,focused,connected,fixtures,
types}.log`, and `/private/tmp/vonk-shared-build-qualified-{fast,fast-final,
connected,types,supply-generate,supply-verify}.log`. Counts from overlapping
groups must not be added.

Logical parent detachment is distinct from physical settlement. W12/W13 still
need to expose shared continuation and exclusive cleanup truthfully. The
remaining preview/admission identity audit, complete profile-to-runtime
process-death/one-slot acceptance, non-build writer/common lock-order audit,
W09d measured-use/post-stop evidence and W09e–W09g remain open. Object
reconstruction and a live scheduler test do not replace process-death evidence.

### W09d Run/Switch build fence checkpoint — 2026-09-23

Working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`, branch
`codex/cli-operator-experience`. This is another implemented ownership boundary,
not completion of W09/W10, shared cancellation, deployment or physical acceptance.

The production container-build phase now carries a current-parent guard into
the actual lifecycle build transaction. It validates the accepted actor and
request, canonical plan, workload ordinal, planned phase and exact child
checkpoint. Scope nodes (including an external builder) are locked in stable
order, then the parent; acquisition is nonblocking. These locks remain held
until child acceptance commits. Cached/active-child recovery and the return
from build dispatch also check the same parent.

Run/Switch uses that predicate again before accepting build progress, child
success/failure or a dispatch checkpoint. A cancellation/newer intent that
arrives during an external observation wins; the old callback leaves its state
unchanged. A fresh reconciliation can still observe an already-issued child
under the current cancellation request. This preserves the existing safe
observation behavior while parent detachment and last-consumer cancellation
remain unfinished. The predicate does not authorize another build merely
because an old executor returned.

New PostgreSQL regressions cover cancellation and newer cleanup intent before
admission, after child commit, and after the phase executor returns; late
running/failed/succeeded child observations; and a barrier proving both parent
and node ownership are retained through child commit. The four original race
cases failed before the fix and passed after it. The final new group has
**13 cases**, included in the connected qualification below. The first fixture
attempt was blocked by an unrelated profile port claim; it now creates an
independent Run/Switch request through the real service.

Older direct executor tests now supply an actual accepted parent, its actor and
ordinal. The production build-receipt test moved from a fabricated partial
plan into the PostgreSQL lane with that real parent. Its adoption and malformed
receipt assertions remain. Initial connected failures from those unfenced
fixtures are retained in the logs; the final qualification passed.

Final validation:

- The connected Run/Switch, profile, availability and build group passed
  **140 tests** in 117.76s. The Controller fast tier passed **2,276 tests**,
  **3 skipped**, in 84.58s. The fast count decreased by one because the
  production build-receipt test moved to PostgreSQL. Counts overlap.
- Root Ruff, formatting of the four touched Python files, Python types (one
  existing reviewed exception; no unlisted errors), web production/types,
  generated Rust wire, coordination boundaries (zero reviewed sites), and
  `git diff --check` passed. All verification process handles are terminal.
- Supply-chain generation and verification passed with manifest digest
  `9d91577bd3bdc90b07edaee53b49cb224d7b70afc5ac002de6f302979415b665`.

Logs: `/private/tmp/vonk-run-build-fence-{red,red-final,focused,connected,
connected-final,fixtures,qualified,fast,types,types-final,types-complete,web,
wire,supply-generate,supply-verify}.log`. `red-final` is the reproduced
cancellation/checkpoint defect; `connected`/`connected-final` retain the fixture
corrections. Final types also validate explicit narrowing of optional test
results/application IDs; no baseline exception was added.

Next: distinguish independently requested producers from adopted shared builds,
serialize parent detachment and last-consumer cancellation, and exercise new
consumers arriving during cleanup. Unresolved adoption, the full
profile-to-runtime process-death pipeline, W09d C/D/F and W09e–W09g remain open.
The full non-build writer and common SQL lock-order audit is still required.

### W09d profile build dependency checkpoint — 2026-09-23

Working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`; the branch remains
`codex/cli-operator-experience`. This checkpoint does not qualify that commit
alone or close W09/W10, W12/W13, deployment, or physical acceptance.

Profile acceptance now takes the existing build dependency fence for changing
assignments with an exact build reference, in sorted build order. A missing
reference, conflicting identity, busy owner or pending cancellation refuses
acceptance before the application and its claims commit. This also covers an
image that is ready when accepted but needs cache recovery before dispatch.
Published images without a build reference do not acquire a fictitious build
dependency.

Cancellation derives pre-dispatch demand from accepted profile plans and their
current workload ordinal. Malformed matching evidence refuses cancellation;
newer workload intent releases obsolete demand. Once the exact Run/Switch child
commits, its phase and cancellation own the dependency. Dispatch and discovery
use the same deterministic child-key helper, so a lost profile checkpoint does
not create a gap or restore demand after that child was cancelled. No new
registry, database field, API route or wire model was introduced.

The regressions reproduced **3 failures, 1 pass** before the fix: cancellation
ignored a queued profile, invalid matching progress did not refuse cancellation,
and profile acceptance passed a held build row. An earlier fixture run also
exposed insufficient independent build capacity; the test now supplies that
capacity instead of borrowing the profile's claim. The first focused group
passed **9 tests**. The expanded connected group passed **59 tests** in 65.86s,
including actual PostgreSQL locks, acceptance held through commit, cancelled
child discovery after checkpoint loss, profile memory inheritance and recovery.

Broader validation found three tests sharing a published-image fixture with an
invented `build-1` reference. That fixture now uses the published-image source
without a build ID. A separate refusal regression preserves coverage of an
explicit missing build; production does not silently discard that reference.

Final validation:

- The lifecycle/profile/API group passed **147 tests** in 73.09s. The Controller
  fast tier passed **2,277 tests**, **3 skipped**, in 85.39s. These overlap with
  the connected group above and are not additive counts. Existing dependency
  warnings remain in the logs.
- Root Ruff, formatting of the five touched Python files, Python types (one
  existing reviewed exception; no unlisted errors), web production/types,
  generated Rust wire, coordination boundaries (zero reviewed sites), and
  `git diff --check` passed. All verification processes reached terminal status.
- Supply-chain generation and verification passed with manifest digest
  `d302eebcf2d1a8d5cdea7083e1887f695b7312d5da452a62828c6f8d46c4a8b4`.

Logs: `/private/tmp/vonk-profile-build-consumers-{red,red-final,focused,
connected,qualified,qualified-final,fixtures,fast,fast-final,types,types-final,
types-complete,web,wire,supply-generate,supply-verify}.log`. The initial type run
found a test mapping that needed narrowing before unpacking; the final type
check passed. `qualified`/`fast` retain the fixture failures; the final runs
above passed.

Next: fence Run/Switch build admission and post-dispatch writes against current
parent intent, then implement independent producer ownership and serialized
parent detachment/last-consumer cancellation. Unresolved adoption, late-result
cleanup races and the full profile-to-runtime process-death pipeline remain
open. W09d C/D/F and W09e–W09g retain their separate gates.

### W09d production builder claim checkpoint — 2026-09-23

Working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`. The previous goal turn made progress
by implementing and verifying the availability-service callback fence. This
turn extends that exact ownership predicate through the production builder and
the lifecycle transaction that admits its child. Existing staged/unstaged and
unrelated work is preserved. W09 and the full W00–W19 objective remain active.

- `RecipeImageBuilder` now requires the actual execution claim, including its
  owner and attempt. Recovery, candidate selection, final plan persistence,
  dependency binding and the child checkpoint use the service's shared
  owner/attempt/lease/removal check. The old operation-ID-only parent helper,
  which accepted any active parent, was removed. Current tests obtain real
  scheduler claims instead of constructing an unfenced execution path.
- A previously selected builder now prepares its plan outside SQL and
  persists it only after checking the claim inside that transaction. This
  prevents late plan writes after takeover or removal as well as preventing a
  stale child checkpoint. Canonical runtime data is retained; no empty-runtime
  fallback was added for incomplete fixtures.
- Lifecycle build admission accepts a SQL-only parent guard. The production
  composition checks the exact accepted request/revision/builder/input binding
  and retains the parent row fence in the same transaction that creates the
  build job and its agent work. A check performed only before calling the
  lifecycle service would leave a dispatch race; this closes that gap.
  Independent build producers continue through the same lifecycle owner.
- A child that committed before takeover remains recoverable by its original
  request identity. Stale workers cannot checkpoint it into a newer claim.
  Cancellation before admission creates no child; cancellation after admission
  settles the unissued child in these regressions through the existing lifecycle
  cleanup owner.
  A subsequent explicit request can proceed after settled cancellation.

`test_availability_builder_fencing.py` adds **13 PostgreSQL cases**. Twelve
combine takeover or cancellation with source resolution, unresolved planning,
preselected-builder planning, dispatch, post-commit checkpoint and recovery of
a lost child checkpoint. They exercise actual production composition and
lifecycle services and then complete the surviving/new intent using a verified
managed archive and injected agent-result evidence. The thirteenth uses
independent sessions and a deterministic barrier to prove the parent claim
stays locked until child admission commits, then verifies cancellation settles
the unissued child normally. No physical build or Spark execution is claimed.

The initial takeover group reproduced six failures. The expanded baseline
reproduced **8 failures, 4 passes**, including cancellation between binding and
dispatch and a preselected plan write after removal. The first post-fix run
passed 11 cases and exposed a test fixture that lacked compiled runtime
expectations during final recovery; the fixture now retains the actual
production runtime. The complete new group passed **13 tests** in 15.59s.

Final validation:

- The connected build/availability group passed **90 tests** in 20.70s before
  adding the transaction-barrier case. The final lifecycle/profile-memory/API
  group passed **265 tests** in 110.67s. The Controller fast tier passed
  **2,276 tests**, **3 skipped**, in 80.39s. These overlapping runs are not an
  additive count. Existing dependency/schema warnings remain in their logs.
- Root Ruff lint and formatting of all seven touched Python files passed.
  Python types retain one reviewed exception with no unlisted errors. Web
  production/types, generated Rust wire, coordination boundaries (zero reviewed
  sites), and `git diff --check` passed. All verification process handles
  reached terminal status.
- Supply-chain generation and verification passed with manifest digest
  `0b24557ef3f9e52fb06a0041e1560b9eef0a5ab0e6d2807695a964b37fcf120a`.

Logs: `/private/tmp/vonk-builder-fence-{red,red-final,regression,connected,
atomic,qualified,fast,types,types-final,web,wire,supply-generate,
supply-verify}.log`. `red`/`red-final` retain the reproduced defects;
`regression` retains the fixture correction. The final gates above passed.

Next: finish the accepted profile/RunSwitch dependency-writer audit, including
profile intent before its child exists and RunSwitch build admission. Establish
independent producer versus shared consumer intent, serialize parent detachment
and last-consumer cancellation, and qualify the races with late results and
cleanup. Unresolved adoption and the full profile-to-runtime process-death
pipeline remain open, as do W09d C/D/F and W09e–W09g. The nonblocking parent
fence and transaction-barrier test do not constitute the complete lock-order
audit or public cancellation acceptance in W12/W13.

### W09d availability claim fencing checkpoint — 2026-09-23

Working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`. The previous goal turn made progress
by reconciling the plan and recording verified availability observation. This
turn implements the remaining availability-service callback fence. Existing
staged/unstaged edits and unrelated work are preserved. W09 and W00–W19 remain
open; the separate production builder dependency writers are still to close.

- A scheduler claim now carries the accepted execution attempt as well as its
  owner. The service checks one shared predicate for job kind, running state,
  exact attempt, owner, unexpired lease and absence of a removal fence. Delayed
  delivery cannot execute a newer attempt merely because a worker name was
  reused, and an expired lease cannot renew itself without a fresh claim.
- Progress, model-child observation, model waiting, resolved build identity,
  receipt acceptance, terminal success, failure and heartbeat renewal now use
  that same check. It locks and refreshes the operation row nonblockingly;
  stale or contended callbacks cannot mutate the current claim. The old
  unfenced removal read and unclaimed execution path were removed.
- Current-revision receipt authorization and the operation's image checkpoint
  now commit in the same fenced transaction. Verified filesystem bytes may
  remain reusable after a late executor finishes, but that executor cannot
  authorize the result or mark the current operation successful. The current
  claim can adopt those bytes through the normal preparation owner.
- Claim loss preserves its typed control outcome through image preparation's
  error boundary, so a rejected progress write does not become a transfer
  failure. SQL row contention returns without waiting under the artifact
  callback. Existing claim ownership and expiry remain visible for scheduler
  recovery; this does not assert that an already-running native transfer has
  stopped or that its process teardown is instantaneous.

`test_availability_claim_fencing.py` adds **nine PostgreSQL cases**. Six initial
regressions failed before the fix: stale image progress, model-child progress,
receipt authorization, final success, model waiting, and delayed claim delivery
with a reused worker name. Two more failed before the lease-expiry check:
renewal and execution after expiry. The ninth holds the owning row through an
independent session during a real preparation callback and proves bounded
return, no accepted authorization, and successful later execution. The cases
use real PostgreSQL, the availability service and managed filesystem storage;
the OCI transport is a fixture. They do not prove native Skopeo cancellation
or physical Spark behavior. The initial fixture-only failure used an overlong
document ID and was corrected before recording the six behavioral failures.

Validation after the fixes:

- The focused availability/production group passed **59 tests** in 14.04s.
  The wider ownership, cancellation, process-recovery, API and storage group
  passed **172 tests** in 69.73s. The Controller fast tier passed **2,276 tests**,
  **3 skipped**, in 82.49s. These are overlapping runs, not additive totals.
- Root Ruff lint and formatting of the four touched Python files passed.
  Python types retain one reviewed exception with no unlisted errors. The
  initial new fixture's protocol-parameter naming error was corrected rather
  than added to the baseline. Web production/types, generated Rust wire,
  coordination boundaries (zero reviewed sites), and `git diff --check` passed.
- OrbStack was verified as the intended engine. Supply-chain regeneration and
  verification passed with manifest digest
  `f3c80d75ce6800267b32392d15597f0896a88387c9e437426b66d18ec9e2dba2`.

Logs: `/private/tmp/vonk-availability-fence-{red,red-2,expiry-red,connected,
connected-2,qualified,fast,types,types-final,web,wire,supply-generate,
supply-verify}.log`. `red` is the fixture error; `red-2` and `expiry-red` retain
the reproduced defects. All test/type/wire process handles reached terminal
status; the final checks above passed.

Next: carry this exact claim through `RecipeImageBuilder` and the production
composition's recovery, candidate selection, dependency binding and child
checkpoint transactions. Those writers still accept only an operation ID and
active state. Reuse the service predicate and prove takeover/cancellation at
each commit boundary; do not add a second consumer registry. Then close the
remaining producer/consumer and last-consumer cancellation protocol, unresolved
adoption and full pipeline/process-death cases. W09d C/D/F and W09e–W09g retain
their separate open gates. This checkpoint closes the service callback gap,
not all accepted-dependency writers or public cancellation in W12/W13.

### W09d availability build observation checkpoint — 2026-09-23

Working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`; the uncommitted changes are part of
the tested implementation. This advances the availability portion of E4/E5f
and preserves the existing staged/unstaged work. W09 and the full W00–W19
objective remain open.

- Availability now stores a canonical private build dependency before
  dispatch. It recovers the accepted lifecycle job by the retained request key
  when the child-ID checkpoint is lost, before reevaluating mutable planning
  or capacity. Exact active children can be shared; no new ownership table or
  independent consumer registry was added.
- Child observation performs one read and returns pending work to the existing
  dependency scheduler. The parent releases its executor and claim, retains
  the child identity, and exposes the next attempt without consuming the build
  failure budget. Cancellation has an explicit terminal refusal. A settled
  failed child or missing archive permits a new execution identity; new
  explicit intent after settled removal does not replay the cancelled child.
- A verified prior image remains usable while a replacement build runs. Build
  resolution and runtime-receipt authorization now distinguish that retained
  artifact identity from replacement execution state. Managed storage and
  exact receipt/recipe identity checks still apply.
- A reproduced race showed an expired executor's failure resetting a newer
  claim and erasing its child dependency. Failure recording now checks both
  owner and attempt under the parent row lock. This is evidence for the failure
  writer; it does not establish that all progress/success writers are fenced.

`test_availability_build_observation.py` contains seven PostgreSQL cases:
lost child checkpoint and adoption before replanning; two shared parents
through one availability slot; recovery after failed child; recovery after
missing archive; new intent after cancellation/removal; verified-image reuse
during replacement; and late failure after a newer claim. The tests use the
actual production service/scheduler and managed filesystem receipts, with
results injected at the agent boundary. They do not execute a physical build.

The original blocking observer, replacement hiding a verified image, and stale
failure writer each failed before their fixes. An intermediate connected run
recorded **308 passed, 1 failed**; that remaining prior-image case led to the
resolver and receipt-owner fixes. After those changes, the focused connected
owner group passed **195 tests** in 52.67s and the Controller fast tier passed
**2,276 tests**, **3 skipped**, in 68.80s. These are distinct runs, not one
combined total. Dependency/schema warnings remain in the logs.

Root Ruff lint, formatting of the nine touched Python files, Python types
(one existing reviewed exception, no unlisted errors), web production/types,
generated Rust wire, and `git diff --check` passed. Local document links and
headings also resolve. The coordination scanner reports zero reviewed sites.
Supply-chain generation and verification passed with manifest digest
`562178ced29bcf069cf1c5cc4e6bc0980f2dab277e30eccb0d1006e034d581a8`.

Logs: `/private/tmp/vonk-build-observation-{red,reuse-red,fence-red,
connected-final,reuse-owner-red,qualified,fast-final,types-qualified,
web-final,supply-generate-final,supply-verify-final}.log`. Intermediate red
logs retain the failures; `qualified` and `fast-final` record the passing
post-fix runs. The implementation plan now reflects this narrower acceptance
and links the remaining work instead of continuing to describe a blocking
availability poll as current behavior.

Next: audit every accepted-dependency writer, including profile intent before
child materialization; distinguish independent producers from parent-only
shared work; serialize parent detachment and last-consumer cancellation;
complete unresolved-builder adoption and all callback fencing; then exercise
process death and the full profile → Run/Switch → build → runtime pipeline
under the common lock order. Measured memory/post-stop evidence, W09e asset
lifetime, W09f exact rebuilt-image consent and W09g combined admission remain
open. These results establish repository behavior at the named seams, without
claiming deployment, physical Spark acceptance or complete W12/W13 cancellation.

### W09d shared-build guard checkpoint — 2026-09-23

Working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`. The preceding goal turn made
progress by reconciling the implementation plan with current source and
specifying the next connected slice. This turn implements and verifies the
guard failure paths. The W00–W19 objective remains active; W09 is not closed.
Existing staged/unstaged and unrelated work is preserved.

- The initial consumer guards now have wider regression evidence. They derive
  current Run/Switch and availability consumers from accepted records, refuse
  direct cancellation while those consumers remain, and serialize accepted
  dependency publication on the build row. No independent consumer list or
  new ownership table was introduced.
- Three new regressions failed before the fix: removal against a held build
  row leaked a database error, and build-ownership/cleanup waits exhausted the
  automatic build-attempt budget. Removal now translates contention on either
  build or lifecycle-job ownership into a typed retryable refusal and rolls
  back its changes. Dependency waits keep their visible next attempt, release
  execution claims and do not consume transfer/build failure attempts.
- Two further PostgreSQL regressions failed before the fix. An existing build
  ID with different inputs was treated as an unresolved dependency; malformed
  cleanup evidence escaped as a generic validation error. Exact IDs are now
  checked against their existing owner's identity, and malformed cancellation
  evidence produces an explicit nonretryable ownership refusal. Neither case
  admits a new consumer or releases the current build's claims.
- Narrowed identity values before passing them to the guard, read Run/Switch
  retry position from its canonical result, and removed the new binding path's
  empty-runtime fallback. Updated production-composition fixtures to carry the
  selected build ID; the production guard has no fallback for incomplete test
  objects. The first broader baseline exposed these fixture/type gaps, and
  they are fixed rather than accepted in a baseline.
- The intermediate five-file group passed **72 tests**. After the final
  identity fixes, the connected profile/build/RunSwitch/availability/API group
  passed **328 tests** in 116.58s. The Controller fast tier passed **2,276 tests**,
  **3 skipped**, in 78.00s. Dependency warnings are retained in the logs.
- Root Ruff lint, formatting of all eight touched Python files, Python types
  (one existing reviewed exception, no unlisted errors), web production/types,
  generated Rust wire and `git diff --check` passed. The coordination scanner
  reports zero reviewed sites. OrbStack was verified as the intended engine.
  Supply-chain generation and verification passed with manifest digest
  `be90bd982729fe25adf96d2d0288d8d7e7e2d87be5e8eec52dffdf02eeb70678`.

Logs: `/private/tmp/vonk-build-consumers-{baseline,waits-red,identity-red,
regression,connected-final,fast,types-before,types,types-final,web,wire,
supply-generate,supply-verify}.log`. The baseline/red/type-intermediate logs
retain the reproduced failures; the final gates above passed. All associated
tool sessions reached terminal status.

Next: finish the accepted-dependency writer audit, including profile intent
before child materialization; distinguish independently produced builds from
parent-owned shared work; reconcile last-consumer cancellation; adopt the
original child before replanning; and replace availability's blocking build
poll with durable dependency observation. Prove the single-slot/restart/race
cases before closing E4/E5/E6 or exposing W12/W13. The remaining W09d measured
usage/post-stop/common-order work and W09e–W09g remain open. These repository
and PostgreSQL results do not establish deployed or physical Spark behavior.

### Execution-plan continuation refinement — 2026-09-23

Planning-only refinement against the dirty working tree on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`. Existing production changes and
staged content are preserved. This entry does not close a package or claim a
new implementation, deployment, or physical test result.

- Retained the twenty packages, six milestones, code/contract owners and
  acceptance gates. Added a scope rule: each Controller prerequisite must name
  its CLI outcome and observable failure, keeping this delivery bounded.
- Reconciled the immediate plan with the initial shared-consumer guards now
  present in the working tree. Existing accepted plans/runtime references
  supply consumer identity; direct cancellation refuses active consumers and
  acceptance/binding use a nonblocking build fence. These are partial guards,
  not complete parent cancellation or last-consumer safety.
- Inspected the existing `/private/tmp/vonk-build-consumers-connected.log`:
  it records 28 passing tests and 1,936 dependency warnings in 36.09s. This
  refinement did not rerun those tests. Their evidence is limited to the
  initial consumer, attempt-cancellation and profile-build-memory scenarios.
- Added an ordered six-step closure checklist: typed contention and dependency
  waits; every dependency writer; independent producer versus shared consumer;
  original-child adoption and new-intent identity; durable parent waits; and
  connected concurrency/worker qualification. The checklist names production
  seams, adverse cases and the evidence needed to close E4/E5/E6.
- Reopened the primary CLI Guidelines, GitHub CLI, Terraform, kubectl, Docker,
  Tailscale and AWS retry documentation. The existing comparison remains a
  design precedent, not a hands-on benchmark or proof of Vonk behavior. The
  installed-process and operator-walkthrough gates remain required.
- Documentation checks passed: all 22 local links/anchors resolve, W00–W19
  each occur once in package order, and the edited documents pass
  `git diff --check`. Application suites were not rerun for these prose edits.

### W09d build-cancellation checkpoint — 2026-09-23

Working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`. The preceding goal turn made progress
by refining the executable plan and identifying the cancellation boundary.
This turn implements its attempt-ownership prerequisite; the full W00–W19
objective remains active. Existing staged/unstaged and unrelated work remains
preserved. This is not a deployment or physical Spark claim.

- Eight PostgreSQL regressions failed before the change. They reproduced
  non-contract `cancelled`/`removal_fence` fields invalidating the canonical
  build request, recovery refusing a fresh request after cancellation, and
  cancelled replacement work marking the prior verified result failed. An
  initial test-module import error was corrected before retaining this red
  evidence; it was not treated as an ownership regression.
- `recipe_build_cancellation.py` makes the exact lifecycle job's existing
  typed result the cancellation owner. Direct cancellation and cache removal
  use it; reconciliation, cleanup and late-result readers no longer consult
  cancellation fields in `RecipeBuild.plan`. Planned work without an issued
  job remains a valid plan rather than acquiring fictitious execution history.
- New deliberate build requests can proceed after confirmed cancellation,
  using normal admission. Original cancelled requests retain their outcome
  even after another attempt succeeds, and generic retry cannot revive them.
  Issued cancellation holds capacity until the exact cleanup receipt arrives.
  Delayed old results and duplicate cleanup cannot release the new attempt's
  claims or overwrite its build state. Cleanup also verifies the original
  cancellation identity, actor, time and reason.
- Cancelling a replacement preserves the previous verified image. A further
  regression failed when pending cleanup unnecessarily blocked reuse of that
  image; only new execution now waits for cleanup. The case uses a real managed
  filesystem receipt and checks both retained bytes and untouched capacity.
- Removal locks affected build jobs nonblockingly before recording their
  cancellation. A held-row PostgreSQL regression first produced an unclassified
  driver error; the service now returns `recipe_image.removal_busy` and rolls
  the transaction back. The test releases the competing lock and proves a
  later removal and reconciliation succeed. This covers that row contention,
  not the complete reference/deletion or common lock-order protocol.
- The new file has **10 PostgreSQL cases**. The initial connected group passed
  **87 tests** in 10.43s. The broader profile/build/availability/RunSwitch group
  passed **278 tests** in 96.02s; the Controller fast tier passed **2,274 tests**,
  **3 skipped**, in 85.72s. After the final contention handling change, the
  build/cancellation/removal/API group passed **101 tests** in 13.53s. Existing
  dependency deprecation/schema warnings are retained in the logs.
- Root Ruff lint and formatting of the seven changed Python/script files
  passed. Python types retain one reviewed exception with no unlisted errors;
  web production/types and generated Rust-wire checks passed; the coordination
  scanner reports zero reviewed sites. The supply-chain fixture includes the
  new canonical cancellation owner and passed **113 standalone tests** in
  32.27s. Regenerated and verified curated manifest digest:
  `0fa76bf1b900e74a49af2811625ed025538e8a3ac7f504fea6bb43f99f409d3f`.

Logs are `/private/tmp/vonk-build-cancellation-{red,reuse-red,busy-red,connected,
regression,fast,final,types-final,web,wire,supply-tests,supply-generate,
supply-verify}.log`. The three red logs document reproduced failures, not final
passes. All associated tool sessions reached terminal status.

Remaining: E5c/E5d consumer registration and last-consumer serialization,
complete parent cancellation/supersession reconciliation, E4 replanning and
E5f/E6's actual single-slot worker completion. The current availability loop's
terminal-state handling and slot release still need that connected change.
W09d measured-use/post-stop evidence, the common writer order, W09e asset
lifetime, W09f exact rebuilt-image consent and W09g combined acceptance also
remain open. These tests exercise actual lifecycle projection and PostgreSQL,
with a recording agent queue; they do not establish authenticated hardware
execution or every process-death boundary.

### Implementation-plan refinement — 2026-09-23

Documentation refinement against the dirty working tree on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`; this is not a new implementation
checkpoint. Existing staged/unstaged changes remain intact. The complete
W00–W19 objective is still active and the package states above are unchanged.

- Added an explicit continuation order through remaining admission, review,
  cancellation, results, installed qualification and operator acceptance.
  Corrected older W09 wording that still listed exact build-memory inheritance
  and reserve preservation as wholly unimplemented.
- Expanded W09d E5 into six ordered implementation slices, with production
  owners, consumer-registration coordination, settled-cancellation replay,
  fresh-request behavior, verified-image preservation, late-effect fencing and
  one-slot worker acceptance. W12 now names E5 as its prerequisite, without
  introducing a reverse dependency on the later cancellation commands.
- Source inspection confirms that direct build cancellation and cache removal
  add non-contract fields to the strict canonical build request. Result and
  cleanup readers currently use those fields, and fresh build submission
  excludes cancelled receipts from its failed-build branch. The plan names
  the connected regression to reproduce these failures; none is claimed to
  have been fixed or newly tested in this planning pass.
- The same inspection found availability's build polling omits `cancelled`
  from its terminal set and retains a blocking poll loop. The plan requires
  durable dependency observation and real single-slot completion evidence.
  Existing capacity-contention tests do not prove that completion boundary.
- Rechecked the primary documentation for CLI Guidelines, GitHub formatting
  and run watch, Terraform plan, kubectl wait, Docker attach and AWS idempotent
  retries. The implementation plan's source matrix maps those patterns to
  concrete checks; this is a documentation comparison, not a user study or
  execution benchmark of those tools.
- Documentation local links, anchors, code fences and `git diff --check`
  passed. Root-pinned Ruff lint passed; Python types reported one existing
  reviewed exception and no unlisted errors; the web production/type build
  and `generate-agent-wire --check` passed. No behavioral suite was rerun for
  this documentation-only refinement.
- Repository-wide Ruff formatting reported nine files outside this edit:
  `.tmp/recipes-reasons.py`, `control/src/vonk_control/agent_jobs.py`,
  `control/src/vonk_control/metrics.py`, `control/tests/test_agent_jobs.py`,
  `control/tests/test_agent_upgrades.py`,
  `control/tests/test_recipe_execution_contract.py`,
  `control/tests/test_runtime_adapters.py`,
  `deploy/compose/tests/test_fresh_runtime_contract.py`, and
  `tests/test_tailscale_acceptance_tailnet.py`. They were left unchanged; the
  repository-wide formatting gate is not recorded as passing.

### W09d build-memory inheritance checkpoint — 2026-09-23

Working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`. The previous goal turn made progress
by refining the implementation plan and its source comparison; this turn
implements the next build-ownership boundary. The full W00–W19 goal remains
active. Existing staged/unstaged work and unrelated files are preserved.

- The connected profile → Run/Switch → actual build-admission regression now
  reproduces `run-switch.container-build-start-unavailable: builder memory
  capacity changed`. Earlier attempts stopped at incomplete source-bundle or
  preflight fixtures; those were corrected before treating the failure as
  evidence of the ownership defect.
- `profile_capacity.py` derives borrowing from the canonical accepted profile,
  exact build and builder, current preparation phase, assignment, alias,
  workload ordinal and deterministic child request. It validates the original
  promise rather than creating another authority or releasing capacity. An
  unrelated request cannot borrow the promise even while that parent is active.
  The ledger recognizes the exact active build and future runtime as sequential
  consumers, preserving the parent claim through build completion. Stale or
  invalid consumer evidence supplies no overlap credit.
- Builder admission preserves the largest declared reserve of overlapping
  profile/runtime consumers in the physical pool. Existing pool tests now
  create actual admitted runtimes with canonical plans instead of orphan
  reservation rows. Exact fit and one byte below are checked in shared and
  separate pools; this is reservation accounting, not measured-use attribution.
- The 15-case connected profile-build file covers changed amount/digest/pool,
  released claims, superseded intent, cancelled or advanced parents, unrelated
  request identity, the reserve boundary, completion without promise release,
  restart after child commit with zero newly available memory, and PostgreSQL
  claim contention. Contention rolls back and records `build.capacity_busy`
  with a next attempt; the original parent claim survives. The availability
  composition translates that same failure into its existing dependency wait,
  releasing its execution slot instead of classifying it as a permanent build
  failure. Its focused regression failed before that translation was added.
- Adoption review found that the active-child and completed-receipt branches
  returned before comparing reviewed build input. Two corrected regressions
  fail with the early guard removed and pass with it restored. Cached/active
  adoption now requires the exact build ID and input before bypassing capacity;
  reconnecting the original child still precedes mutable admission.
- The initial affected regression group passed **216 tests** in 111.51s and
  the Controller fast tier passed **2,274 tests**, **3 skipped**, in 77.01s.
  After the final identity guard, the connected build/RunSwitch/production-path
  group passed **79 tests** in 33.61s. Root Ruff, changed-file formatting, Python
  types (one existing reviewed exception), web production build, generated Rust
  wire check and the coordination scanner (zero reviewed sites) passed. No
  native producer or API schema changed in this checkpoint.
- Curated supply-chain generation and verification passed with file-map
  manifest digest
  `9ba90d943f4245db5c80aaed3c776c5208a05b15ed8ab67d52223e4c11a13716`.
  Evidence logs use `/private/tmp/vonk-build-inheritance-` with suffixes
  `red.log`, `availability-red.log`, `adoption-red.log`, `pool.log`,
  `regression.log`, `fast.log`, `adoption-final.log`, `types-final.log`,
  `lint.log`, `web.log`, `wire-check.log`, `coordination.log`,
  `supply-generate.log`, and `supply-verify.log`. Earlier fixture failures are
  retained in the intermediate `connected.log`, `expanded.log`, and
  `expanded-final.log` files and are not reported as passing evidence.

This does not close W09d E. Replanning before an exact persisted build is
available, shared-consumer cancellation and late-result ownership, and an actual
single-worker-slot acceptance case still need closure. The manual worker ticks
prove the connected service boundary, not worker-pool scheduling. Measured-use
and post-stop evidence remain in C/D, the common acquisition-order audit and
presentation remain in F, and W09e–g remain open. No publication, deployment or
physical Spark acceptance is claimed.

### W09d memory-ownership checkpoint — 2026-09-23

This is working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`. The preceding goal turn made progress
by closing the live image-packaging check and recording the verified supply
chain; this turn continues implementation. The full W00–W19 goal remains active.

- Three real PostgreSQL failures reproduced the absence of accepted profile
  memory claims for single-node, two-node and replacement work. Profile
  acceptance now writes stable parent-owned memory promises in its existing
  transaction, only for assignments needing a start. Retained runs keep their
  own claims. SQL restricts promised memory to the profile owner.
- The exact child validates assignment, alias, node set, demand kind, byte
  amount, physical pool, digest and current workload ordinal, then transfers
  the same claim alongside the new run/start job. Missing or changed claims
  cannot fall back to an independent allocation. The connected boundary test
  also exposed a nested preview that counted the parent's own memory; the
  profile exclusions now reach that read without broadening run acceptance.
- The shared ledger counts live work and its reviewed replacement as
  successive phases, using the larger demand in each physical pool. Live rows
  remain active until their stop receipts release them, while the replacement
  promise survives. The required `StopImpact.run_plan_digest` binds the runtime
  behind the reviewed stop; `plan_digest` still identifies the stop action.
  The same exact-run query supplies overlap and planned-stop reservation
  release. Complete OpenAPI and generated Python/TypeScript consumers were
  regenerated together.
- **69 connected tests passed** in 105.23s, including both topology sizes,
  original-row handoff, current-memory boundary refusal, changed amount/pool/
  digest, missing claims, superseded intent, stale/unrelated replacement
  claims, and restart adoption after available memory drops to zero. Existing
  port tests now select ports explicitly and the crash cases check both port
  and memory ownership. An additional memory-row contention case is included
  in the final regression group recorded below.
- The final connected group passed **282 tests** in 113.70s, including
  contention on the memory row, retry with the original promise, runtime/build
  admission, resource planning, lifecycle and persisted execution contracts.
  The full Controller fast tier initially found three incomplete assessment
  fixtures; their successful serving assessments now declare memory demand,
  kind, pool and reserve. The corrected tier passed **2,273 tests**, **3 skipped**,
  in 73.36s. No production fallback was added for incomplete evidence.
- Root Ruff, changed-file formatting, Python types (one existing reviewed
  exception), web production build, generated Rust wire check and the
  coordination scanner (zero reviewed sites) passed. The standalone suite's
  initial run could not bind its local HTTPS servers in the sandbox; that
  environment failure is retained separately from the permitted rerun.
  The permitted standalone rerun passed **939 tests**, **11 skipped**, and
  **45 subtests** in 25.39s. Curated supply-chain generation and verification
  passed with file-map manifest digest
  `19aa2632aaca675ed0f71e7256feadc7305cfab7e5dfeb4695deb9e3878c597c`.
  Logs use the prefix `/private/tmp/vonk-profile-memory-`: `red.log`,
  `connected.log`, `expanded.log`, `regression.log`, `fast.log`,
  `fast-final.log`, `root.log`, `root-final.log`, `clients.log`,
  `types-final.log`, `web.log`, `lint.log`, `coordination.log`,
  `wire-check.log`, `supply-generate.log`, and `supply-verify.log`.

This checkpoint does not close W09d. Builder lineage/shared dependants and
system-reserve preservation still need their connected admission implementation;
a preparation build may currently compete with its own parent's future claim.
Ledger overlap does not measure physical usage, prevent the existing
observed-use/reservation double charge, or prove a post-stop inventory reading
is newer than the stop. Those remain required C–F work, followed by W09e–g.
No published, deployed or physical Spark acceptance is claimed.

### W09d physical-memory-pool checkpoint — 2026-09-23

This is working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`. The full objective remains active;
W09 is not complete. No publication, deployment or physical execution is claimed.

- Three PostgreSQL/API failures and two native producer failures reproduced the
  absent pool relationship. Authenticated inventory now requires `memory_pool`
  (`shared` or `separate`). Canonical validation is used on inventory writes and
  reads; missing/unknown/null values and a shared pool without a GPU are refused.
  SQL carries the required constrained field. Equal byte counts do not cause
  inference of a shared pool. The agent reports GB10 host memory as shared even
  when GPU-attributed numeric counters are available; unsupported evidence still
  fails closed. Inventory and telemetry use one hardware classifier. This
  follows NVIDIA's [documented Spark memory model](https://docs.nvidia.com/dgx/dgx-spark/known-issues.html).
- Four PostgreSQL regressions reproduced ignored competing claims: a host-memory
  builder could coexist with a non-fitting runtime, and a builder ignored
  unified/accelerator runtime claims on a shared pool. `memory_reservations.py`
  now projects the ledger once and `resource_planning.py` owns the physical-pool
  rule for profile review, runtime acceptance and builder preview/acceptance.
  Shared pools count every overlapping memory kind. Separate pools retain
  independent limits; unified demand checks both rather than adding their
  unrelated reservations. Both constraints are rechecked after planned stops,
  including when the limiting pool changes.
- Runtime plans and typed profile reviews bind the physical relationship;
  changing it refuses the old reviewed digest even if both versions fit.
  CLI review distinguishes demand kind from physical pool and reports the
  available memory of the current limiting pool. The complete OpenAPI and
  generated Python/TypeScript clients carry the current fields.
- The required inventory contract is packaged as `vonk-agent-protocol` 3.0.0.
  Its wheel, locks, image inputs and packaging consumers replace the retired
  2.2.0 wheel. This is a coordinated current-contract and fresh-schema change;
  there is no compatibility reader or deployed database migration.
- **257 connected tests passed** in 24.48s, including actual PostgreSQL
  acceptance, stale preview refusal, exact boundary/one-byte shortage, shared
  and separate pools, review binding, existing disk/port handoff and runtime
  lifecycle. Native Linux producer checks passed **6 inventory tests** and
  **18 telemetry tests**. Rust Clippy passed for the agent and protocol with
  all targets. The complete Linux wire lane passed **675 tests**, **19 skipped**,
  in 26.96s, followed by its **1 publisher test** in 1.70s. This lane used the
  current recipe checkout `068930061f3b76a519ac3ffcc137ab7a9f067010`, explicitly
  different from CI's pin `71d83d36adb2d16fea59efdd9e092648b05fab66`; it is local
  Linux evidence, not a claim about the pinned CI run or physical hardware.
- Packaging the current source exposed old byte-bound fixtures that still
  assumed 64 KiB. They now exercise the authoritative `MAX_DOCUMENT_BYTES`
  bound; production limits were not changed. One CPU-only inventory fixture
  now correctly declares separate memory, and the review test distinguishes
  the two newly explicit memory labels. The corrected full Controller fast tier
  passed **2,273 tests**, **3 skipped**, in 78.20s. The standalone fast tier
  passed **939 tests**, **11 skipped**, and **45 subtests** in 49.59s; the final
  focused CLI/rendering regression passed **66 tests** in 3.93s.
- The root-context Controller image packaging check passed **1 test** in
  51.43s. It built the local test image and checked installation of the current
  contracts and protocol 3.0.0 from declared build inputs. This verifies local
  packaging; it does not establish published-image or deployed acceptance.
- Curated supply-chain evidence was regenerated and verified without errors.
  The resulting file-map manifest digest is
  `47ee5867a3a9eb69e14798d014cb55057a3fce65edecc63fe0ef5ec7b7aba69a`.
  This is `inventory/sbom/manifest.json`, not a signed release generation.
- Root Ruff, changed-file formatting, Python types (one existing reviewed
  exception), web production build, generated Rust wire check, Rust formatting,
  and the coordination scanner (zero reviewed sites) passed. Logs are under
  `/private/tmp/vonk-memory-pool-`: `api-red.log`, `native-red.log`,
  `admission-red.log`, `api-final.log`, `native-final.log`, `connected.log`,
  `linux-wire.log` (initial stale-test failures), `linux-final.log`,
  `clippy.log`, `control-fast.log` (initial fixture failures),
  `control-final.log`, `root-fast.log`, `cli.log`, `types-verified.log`,
  `lint-verified.log`, `web.log`, `wire-check.log`, `rust-format.log`,
  `coordination-final.log`, `image.log`, `supply-final-generate.log`,
  `supply-final-verify.log`, and `diff-check.log`.

Remaining W09d work is C–F in the implementation plan: durable parent memory
ownership, after-stop observed-usage accounting, exact child handoff, shared
builder inheritance and common acquisition order. These tests do not establish
those properties. A peak reservation is still not proof of physically freed
memory, and completed stops require current evidence before dispatch. W09e
artifact lifetime and W09f exact rebuilt-image recovery also remain open.

### W09d port-ownership checkpoint — 2026-09-23

This is working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`. The full objective remains active;
W09 is not complete. No publication, deployment or physical execution is claimed.

- Three PostgreSQL/API regressions first reproduced missing port ownership
  after profile acceptance. Profiles that need a runtime start now reserve the
  exact reviewed serving/rendezvous ports in the acceptance transaction.
  Independent runtime review and acceptance see these reservations, including
  a competing start whose own preview fitted before the profile was accepted.
- The existing reservation ledger has a `promised` state restricted to profile
  ports, plus a unique pending-owner index. The active-port uniqueness rule
  remains. A live run retains its active reservation while the accepted profile
  holds the replacement; releasing the old run cannot expose an unreserved gap.
  This changes the fresh SQL schema. The existing current-metadata migration
  creates it; no compatibility migration or deployed database reset was added.
- Child admission validates the exact application, assignment, alias, node/port
  set, reviewed digest and current workload ordinal under its reservation/node
  locks. It transfers the same port rows to the run in the transaction that
  creates that run and its start job. Changed claims or intent are refused
  before a run is created. Supersession/terminal parent transitions release
  only still-parent-owned claims; a handed-off run keeps its ports.
- A busy port writer rolls back admission and schedules the same operation's
  next attempt with `run.capacity_busy`. The existing wait owner handles both
  disk and run contention, without retaining a transaction or worker slot.
  Restart adopts the original start request before trying to admit its already
  owned ports again. Fleet counts active and pending reservations by physical
  port, so an overlapping replacement appears once.
- Eleven PostgreSQL cases cover single/two-Spark handoff, live replacement,
  supersession, a changed/released claim, newer intent, contention and lost
  start checkpoints. Restart is tested with an active or completed child.
  The completed child reconnects and advances to final verification; this
  fixture has no gateway publisher and correctly does not claim profile success.
  These are real SQL/checkpoint boundaries with typed fixture agent evidence,
  not physical Spark or process-kill acceptance.
- **178 connected tests passed** in 26.26s, including existing disk recovery,
  runtime admission, Fleet projection and fresh-Alembic PostgreSQL lifecycle.
  The broader fast run exposed an observer subclass still using the old private
  lifecycle signature; it now forwards the parent context to the real service.
  The final connected regression passed **73 tests** in 22.54s, including
  the independent stale-preview refusal and the corrected observer.
- The full Controller fast tier passed **2,272 tests**, **3 skipped**, in
  74.04s. Its final rerun first exposed order-dependent authorization-test
  pollution: a model-cache fixture inserted an obsolete route into the shared
  mutation policy. Running that fixture followed by the security matrix
  reproduced the failure. Removing the fixture's policy mutation preserves
  the production policy and the security completeness assertion; those five
  tests then passed together. The standalone fast tier passed **939 tests**,
  **11 skipped**, and **45 subtests** in 33.27s.
- Root Ruff, changed-file formatting, Python types (one existing reviewed
  exception), web production build, Rust wire check, coordination scanner
  (zero reviewed sites), and `git diff --check` passed. Evidence logs use
  `/private/tmp/vonk-profile-port-ownership-` with suffixes `red.log`,
  `connected.log`, `final-regression.log`, `control-final.log` (the pollution
  failure), `control-verified.log`, `root-fast.log`, `types-verified.log`,
  `lint-verified.log`, `format.log`, `web.log`, `wire-verified.log`,
  `coordination-verified.log`, and `diff-check.log`. The isolated pollution
  regression uses `/private/tmp/vonk-cli-authorization-isolation-` with
  `red.log` and `final.log`.
- Regenerated and verified the curated input manifest, digest
  `1682b287eb7d0c6bac808feb1bf3cc3e0883ff35d18ec7e569417d30837f64b6`.
  This is the supply-chain input map, not a release manifest.

Durable memory claims, after-stop memory accounting, builder inheritance and
the shared acquisition-order audit remain open in W09d. Port ownership does
not establish those properties, W09e artifact lifetime, or W09f recovery.
The next implementation sequence now names the missing physical-memory-pool
contract: the agent already distinguishes GB10's shared readings from dedicated
accelerator readings, but admission inventory carries only counts. Carry that
relationship through the canonical producer/store/consumer boundary before
joining host-memory builders and runtime claims. Do not infer it from equal
totals, and do not count a peak reservation as observed freed memory.

### W09d shared-memory checkpoint — 2026-09-23

This is working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`. The full objective remains active;
W09 is not complete. No publication, deployment or physical execution is claimed.

- Two real PostgreSQL/API boundary cases reproduced disagreement between
  profile review and runtime admission. With different host/GPU totals,
  review combined the smaller total with the larger occupied amount, inventing
  unavailable memory. Both paths now select the same limiting free capacity
  and enforce the same system/platform reserve, including the exact boundary
  and one byte below it. An admitted runtime persists the tested demand.
- `resource_planning.py` owns the shared `MemoryRequirement`, inventory
  selection and capacity decision. Run/Switch review and low-level run admission
  both consume it. The old duplicate recipe-memory arithmetic and final
  hand-written subtraction in runtime acceptance were removed; acceptance
  relies on its fresh plan under the existing SQL fence. The real stale-claim
  refusal and competing-admission tests remain. A redundant test that replaced
  `plan_run` with a stub to demand that second calculation was removed.
- Typed review requirements now include memory kind and reserve. CLI review
  shows both. A changed reserve policy changes the reviewed digest even when
  the placement still fits; harmless free-byte observations do not. Generated
  OpenAPI, Python and TypeScript consumers carry the same current fields.
- The complete Controller fast tier exposed four integration gaps from earlier
  implementation: a removed request flag in a scalar fixture, an undeclared
  engine-owned settings extension, inconsistent route-converter spelling in
  model mutation policy keys, and a cache-recovery test assuming child creation
  during submission. Current policy keys now match the registered routes; the
  original security coverage check remains intact. The recovery test now runs
  the worker, verifies a healthy image first, then loses one model object and
  proves shared repair reuses that image and the retained model file.
- **2,272 Controller fast-tier tests passed**, **3 skipped**, in 74.62s.
  **67 focused tests passed** in 20.10s, including PostgreSQL memory admission,
  disk inheritance, preparation checkpoint loss and submission recovery.
  **141 focused standalone CLI tests passed** in 4.38s. The subsequent complete
  standalone fast tier passed **939 tests**, **11 skipped**, and **45 subtests**
  in 33.70s, using permission to bind its real localhost HTTP/HTTPS servers.
  That run also caught an obsolete profile-load field-list assertion. Its
  replacement exercises the packaged validator: a reviewed request is accepted,
  a missing review digest is refused, and a client revision override is refused.
  Root Ruff, changed-file
  formatting, Python types (one existing reviewed exception), web production
  build, Rust wire check and coordination scanner (zero reviewed sites) passed.
  Logs use `/private/tmp/vonk-profile-memory-shared-` with suffixes `red.log`,
  `focused.log`, `control-fast.log` (the initial failures), `control-final.log`,
  `postgres.log`, `cli.log`, `root-final.log`, `types-final.log`, `lint-final.log`, `format.log`,
  `web.log`, `wire.log`, `coordination.log`, and `generation-final.log`.
- Regenerated and verified the curated input manifest, digest
  `9d65c8ef1b438c38ff6858eec0aab50198b3f19b920fd2b07f9cd542d988e6ca`.
  This is the supply-chain input map, not a release manifest.

The next connected ownership change must cover after-stop memory/port reuse,
parent-to-run handoff and builder claims. Builder admission currently sums
host-memory claims; resolve the physical pool relationship with unified-memory
claims from the authoritative inventory/contract. This checkpoint does not
prove that differently named reservations protect independent resources, nor
does it close common lock ordering, W09e artifact lifetime or W09f recovery.

### W09d disk-ownership checkpoint — 2026-09-23

This is working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`. The full objective remains active;
W09 is not complete. No publication, deployment or physical execution is claimed.

- A real PostgreSQL/API regression first reproduced the missing claim after
  profile acceptance. Acceptance now creates the reviewed disk reservation in
  its own transaction. Competing installation admission sees it; the exact
  authorized child can inherit it without counting the parent twice.
- `profile_capacity.py` owns this transition using existing
  `ResourceReservation` rows. Installation acceptance transfers the same row
  to the installation atomically. The claim binds the application, assignment,
  node, reviewed amount and workload ordinal; no SQL schema change is needed.
  Child demand cannot exceed the reviewed claim. Retained installation claims
  survive parent failure; terminal parents release only unassigned claims.
- Stable claim identities reconnect a preparation committed before its
  parent checkpoint. Two additional regressions tightened capacity after that
  crash and reproduced failed recovery before the fix. Recovery now adopts
  the exact installation receipt before seeking new capacity. The six
  checkpoint cases cover profile, preparation and installation boundaries,
  with children active or already complete. They inject `SystemExit` at real
  commit/checkpoint seams and recreate services; they do not kill an operating
  system process or exercise physical Spark agents.
- A contended disk handoff uses nonblocking reservation locks. Its SQL
  transaction rolls back, the same operation exposes `install.capacity_busy`
  and its next attempt time, and the worker resumes the original claim once
  the writer releases it. Waiting consumes neither a transaction nor a new
  request identity. Single- and two-Spark inheritance, supersession, failure,
  contention and restart are covered by actual PostgreSQL tests.
- The broader suite caught recovery inspection advancing an active child.
  Adapter `get` is now read-only; only worker `advance` dispatches or mirrors
  progress. Recovery eligibility cannot change the state it is inspecting.
- **489 affected Controller tests passed** in 38.34s. After consolidating
  terminal state changes and claim release in `_set_application_state`, all
  **295 directly affected Controller tests passed** in 25.94s. The focused
  handoff/restart set passed **18 tests** in 22.96s, and **113 standalone CLI
  tests passed** in 4.07s. Root Ruff, changed-file formatting, Python types
  (one existing reviewed exception), the web production build, Rust wire
  check and coordination scanner passed. Generated OpenAPI/Python/TypeScript
  consumers include the typed optional profile owner on the Run/Switch result.
  Logs use `/private/tmp/vonk-profile-disk-` with suffixes `red.log`,
  `handoff-red.log`, `handoff-green.log`, `control-final.log`, `state-final.log`,
  `cli.log`, `lint-final.log`, `format-final.log`, `types-final.log`,
  `web.log`, `wire.log`, `coordination-final.log`, and `generation-final.log`.
- Regenerated and verified the curated input manifest, digest
  `b04a9b9d85fdb46945d19a60dcea42caf9bb5f481d7b62e24c682ab506a8a24b`.
  This is the supply-chain input map, not a release manifest.

Memory/port ownership and builder admission remain open. The current disk
handoff is not proof of a common lock order across every writer, cancellation
of issued effects, artifact lifetime, or whole-workflow acceptance. Continue
W09d–W09g and W10 before claiming the profile-load package complete.

### W09d shared-port checkpoint — 2026-09-23

This is working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`. The full objective remains active;
W09 is not complete. No publication, deployment or physical execution is claimed.

- While tracing reservation inheritance, four real PostgreSQL/API regressions
  reproduced a prerequisite gap: a fresh-install profile allowed an occupied
  service or distributed rendezvous port, both at review and after its final
  preview (`202` instead of `409` on load).
- `run_admission.py` now owns shared port demand and conflict reasons. The
  Run/Switch fit uses that owner for fresh and existing installations, including
  the profile acceptance recheck. Runtime admission/reservation uses the same
  demand. Independent interface/port predicates and the separate high-level
  stop-port classifier were removed.
- Required ports are explicit in `SparkFitNode` and
  `FleetProfileResourceRequirement`, so the semantic review binds them and the
  CLI displays them. Full OpenAPI and generated Python/TypeScript consumers
  were regenerated. Install-only and logical artifact-job demand has no
  serving-port reservation; the existing logical run receipt retains its
  current non-network placeholder.
- A connected two-Spark run proves after-stop review names precisely the ports
  held by that runtime. Reassigning the rendezvous reservation to another kind
  of owner makes review refuse even when the textual owner ID is the same.
  Thus a stop does not authorize borrowing unrelated capacity.
- **461 affected Controller tests passed** in 36.04s; **113 standalone CLI
  tests passed** in 4.29s. After tightening fixture types, all **12 PostgreSQL
  capacity/port tests passed** in 13.23s. Root Ruff, changed-file formatting,
  Python types (one existing reviewed exception), web production build, Rust
  wire check, coordination scanner (zero reviewed sites), and diff checks
  passed. Logs use `/private/tmp/vonk-profile-ports-` with suffixes `red.log`,
  `focused.log`, `control.log`, `cli.log`, `capacity-final.log`,
  `types-final.log`, `lint.log`, `format.log`, `web.log`, `wire.log`,
  `coordination.log`, and `generation.log`.
- Regenerated and verified the curated input manifest, digest
  `06d6755327367f3e649513096c4adc6f67d5395f87ad7345ddb3a2fda16db198`.
  This is the supply-chain input map, not a release manifest.

This closes port eligibility in reviewed admission, not durable ownership.
Continue accepted capacity claims and child inheritance/release, builder
admission and common lock ordering, then W09e/W09f/W10. The preceding goal turn
made progress by recording and validating the memory/disk admission fix; this
turn changes the shared port predicate and its connected consumers.

### W09d capacity-admission checkpoint — 2026-09-23

This is working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`. W09/W10 remain open and the full
objective remains active. This checkpoint does not claim post-commit resource
ownership, deployment, physical execution, or an operator walkthrough.

- Four real PostgreSQL/API regressions first reproduced accepted loads after
  memory or disk had been consumed between the final preview and acceptance,
  either by a competing reservation or changed inventory (`202` rather than
  `409`). Acceptance now reuses the Run/Switch resource predicate inside its
  short SQL boundary, before application creation, intent advancement, or
  cancellation. Refusal leaves existing jobs and workload ordinals unchanged.
- The semantic comparison checks eligibility, exact demand and preparation
  order. A small headroom change that leaves that decision unchanged still
  accepts. Guarded callbacks verify that this recheck does not inspect managed
  artifacts, image archives, or external model capabilities while holding SQL
  coordination locks.
- Inventory and resource-reservation writers now participate in the admission
  fence, including insertion through PostgreSQL's implicit writer locks. Two
  independent-session tests hold acceptance after its capacity check and prove
  competing inventory/reservation updates cannot commit before acceptance.
  These tests establish the transaction boundary, not durable claims after it.
- The broader suite exposed missing resource evidence during typed image
  repair. That recovery path now retains its Run/Switch assessment when image
  preparation is pending; it does not bypass resource admission. The obsolete
  exception path that discarded the assessment was removed.
- **445 affected Controller tests passed** in 37.70s. Root Ruff, changed-file
  formatting, Python types (one existing reviewed exception), web production
  build, Rust wire check and the coordination scanner (zero reviewed sites)
  passed. Client regeneration left all 572 recorded generated-input/consumer
  hashes unchanged and created no additional generated Python files; this
  checkpoint changes no public API schema.
- Regenerated and verified the curated input manifest, digest
  `990ee1309a47c48dfa167d818dceacd587a618177fefa12aa3683709578dfd70`.
  This is the supply-chain input map, not a release manifest.
- Logs are `/private/tmp/vonk-profile-capacity-red.log`,
  `/private/tmp/vonk-profile-capacity-focused.log`,
  `/private/tmp/vonk-profile-capacity-locks.log`,
  `/private/tmp/vonk-profile-capacity-control-final.log`, and the corresponding
  `lint-final`, `types-final`, `web`, `wire`, `coordination`, and `generation`
  logs. Earlier failed runs were corrected before the final suite above.

The plan now records the actual resource writers and corrects the unused
`NodeLeaseService` assumption. Next implement accepted claims through the
existing workload-intent/reservation owners, with child adoption, no double
counting, exact release and supersession. Join port/build decisions and the
common lock order before closing W09d; W09e asset lifetime, W09f recovery
identity and W10 complete effects presentation still follow.

### W09 installed-intent checkpoint — 2026-09-23

This is working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`. The full objective remains active;
W09/W10 and W11–W19 are not complete. No commit, publication, deployment,
physical Spark run, or operator walkthrough is claimed by this checkpoint.

- A regression first reproduced a successful profile with no installation
  for both one- and two-Spark assignments. The profile now queues an explicit
  `install` child through the existing Run/Switch planner and lifecycle owner.
  Its phases prepare/distribute/install/verify without a runtime-start phase.
- Final verification reads the bound installation identity and exact member
  states, plus active runs and route states. A successful child cannot hide
  a missing member, incomplete member, or changed image. The canonical receipt
  survives restart and is accepted by the CLI's generated-schema validator.
- Installation assessment excludes serving-memory demand and serving-port
  admission while preserving disk checks and the existing build, preparation
  and lifecycle checks. A real occupied-port/zero-free-memory fixture permits
  installation and refuses serving; insufficient disk still refuses review.
  Installation readiness is separate from runtime health so a degraded run
  does not invent a reinstall in the review summary.
- A running-to-installed profile stops its reviewed runtime, withdraws its
  route and reuses the existing installation. The connected multi-child test
  uses PostgreSQL; SQLite's single-writer restriction is not concurrency
  evidence for the Controller.
- Four PostgreSQL checkpoint-failure cases cover parent restart after either
  the profile child or installation child commits, with that child pending or
  completed before restart. The operation and installation remain singular.
  These inject `SystemExit` at real commit/checkpoint boundaries and recreate
  the services; they are not OS-process-death or physical-agent acceptance.
  Adoption derives action and alias from frozen assignment intent without
  consulting mutable cache readiness.
- Regenerated full OpenAPI and Python/TypeScript consumers for the install
  action, child kind and installation receipt. **438 affected Controller tests
  passed** in 34.75s and **113 standalone CLI tests passed** in 5.34s. Root Ruff,
  changed-file formatting, Python types (one existing reviewed exception),
  web production build, Rust wire check and the coordination scanner (zero
  reviewed sites) passed. The suite uses deterministic artifact/agent-result
  fixtures for installation effects; it does not prove physical installation.
- Regenerated and verified the curated input manifest, digest
  `79644217f0b7ff229de979ebf6c713c3c4558b654dfb773de461b2f4f7d37738`.
  This is the supply-chain input map, not a release manifest.
- Logs are `/private/tmp/vonk-profile-installed-red.log`,
  `/private/tmp/vonk-profile-installed-focused.log`,
  `/private/tmp/vonk-profile-installed-control-final.log`,
  `/private/tmp/vonk-profile-installed-cli.log`, and the corresponding
  `types-final`, `lint-final`, `web`, `wire`, and `coordination` logs.

Next close W09d resource ownership and child inheritance, W09e artifact
reference/deletion coordination, and W09f exact recovery identity. Finish
W10's complete effects presentation and connected acceptance before M3.

### W09d workload-effect checkpoint — 2026-09-23

This is working-tree evidence on HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`, not a release or a claim that W09 is
complete. The preceding turn made progress by finishing the implementation
breakdown and verifying its current checkpoint; this turn changes and tests
the admission and worker boundaries.

- Reproduced the actual load API accepting a replacement run after its fresh
  review but before acceptance (`202` instead of `409`). Review, admission and
  queue planning now share `_control_effects`; the old independent queue
  reconciliation predicates are removed. Acceptance compares exact run,
  installation and pending-order effects, assignment states and execution
  nodes before advancing intent or cancelling anything.
- The short PostgreSQL admission transaction also fences mapping membership,
  active workloads/installations and pending jobs/applications, including
  insertions. It reuses the frozen choices and contains no cache resolution.
  Real tests cover replacement runs, new installations, new pending work and
  a concurrent run writer excluded until the acceptance commit. This does
  not reserve capacity or pin artifact bytes.
- Reproduced a worker queue adopting a replacement run after acceptance and
  an assignment child re-previewing an additional valid stop. Queue creation
  and each fresh/adopted child now enforce the accepted destructive effects
  and complete Spark scope. Queue retention comes from persisted intent. The
  child regression uses a replacement with matching reservations so a broken
  stop contract cannot accidentally supply the refusal being tested.
- Real contention on either a superseded `Job` or its `AgentOperation` first
  blocked the load request. Both locks now use nonblocking acquisition. The
  tests retain the competing lock while observing `409`, no application, and
  unchanged workload ordinals; the entire admission transaction rolls back.
- Supersession and retry-eligibility checks compare the saved definition
  directly with accepted intent. A regression previously reached the cache
  resolver from that coordination path; now storage unavailability cannot
  turn the SQL intent comparison into an external wait. Actual admission
  still checks availability through its owning planner/storage boundary.
- **426 affected tests passed** in 31.83s, including profile API/services,
  actual PostgreSQL admission races, child-crash adoption, recipe lifecycle,
  Run/Switch, operation views and agent jobs. Root Ruff lint, Python types
  (one existing reviewed exception), web production build, Rust wire check,
  coordination scanner (zero reviewed sites) and whitespace checks passed.
  Changed Python files are formatted; `agent_jobs.py`'s changed range passes,
  while two unrelated pre-existing tuple-return formatting differences also
  occur at HEAD and were preserved.
- Regenerated and verified the curated supply-chain input map, digest
  `a4cf3f92518f9c761a79ab9c93c70ab7b7fb68cca89adb7198a67c50ca61f863`.
  No wire fields changed in this checkpoint; the existing generated contracts
  remain current. This input map is not a published release manifest.
- Red and green logs are local `/private/tmp/vonk-profile-effect-admission-*`,
  `/private/tmp/vonk-profile-dispatch-effects-red.log`,
  `/private/tmp/vonk-profile-child-effects-red.log`,
  `/private/tmp/vonk-profile-cancel-lock-*`, and
  `/private/tmp/vonk-profile-effects-*`. The storage/intent regression's red
  log is `/private/tmp/vonk-profile-intent-storage-red.log`.

**New concrete closure gap:** an actual production-wired profile service with
the existing deterministic artifact inspector accepts `desired_state=installed`
on an uninstalled assignment, then reports `succeeded` with no installation.
The disposable reproduction returned `review_allowed=true`,
`application_state=succeeded`, and `installations=[]`. The current queue only
starts assignment children for `running`; Run/Switch also always plans a start
for those assignment requests. Next add install-without-start through that same
owner, its canonical contracts and real lifecycle receipts. Do not fix this by
merely rejecting installed intent or labelling an empty queue successful.

Then finish resource ownership, artifact reference/deletion coordination, exact
identity recovery and the remaining connected gates. W09/W10 and W11–W19 remain
open; no deployed Controller, physical Spark or operator-walkthrough claim is
made.

### W09b/W09c admission and review checkpoint — 2026-09-23

Evidence applies to the current uncommitted working tree on
`codex/cli-operator-experience` (HEAD
`38c2018e5ca69c49356d8b19722f8265759ecc08`), not to that commit alone or a
published artifact. Existing staged edits and unrelated work are preserved.

- The numbered load body requires the reviewed digest and caller request key.
  Actual route/service tests use PostgreSQL to exercise current authority,
  original-request replay after profile edits, same-key changed intent, response
  loss, duplicate submission, full-roster insertion and recipe-head races.
  Profile acceptance freezes reviewed assignments instead of resolving cache
  choices while holding SQL locks.
- Profile and Run/Switch share `RunSwitchAssessment`. Semantic admission
  decisions participate in the review fingerprint; observed free capacity
  remains outside it unless eligibility changes. The CLI shows current and
  after-stop headroom, including explicit unknown values and deficits. The
  canonical validator refuses a hidden preparation blocker; the CLI renders
  named admission blockers once.
- Ordinary profile/library inspection cannot create or reset a source build.
  Typed recovery of an accepted cache-loss operation may defer repair to its
  worker, while capacity and invalid-contract failures still block it. The
  regression first showed a review resetting a succeeded build's identity;
  it now preserves that state while reporting the missing archive.
- Retry queue admission and persisted-intent consumption reject stop/removal
  effects outside the original review. The regression first accepted a new
  run with the old alias; it now refuses both that run and a newly introduced
  installation. This is not yet a concurrent fence against changes after the
  check. Existing newer-intent refusal retains diagnostic priority.
- **287 affected Controller tests passed** in 24.56s, including profile
  services/API/recovery, actual load submission races, Run/Switch, recipe
  operations, library assessment and operation views. PostgreSQL tests ran
  with the verified OrbStack engine. Fixture executors in these suites do not
  establish physical Spark or full managed-storage acceptance.
- **163 standalone CLI tests passed** in 4.48s. The affected web component's
  four tests and production build passed. OpenAPI, Python and TypeScript
  clients were regenerated; the Rust wire check passed. Root Ruff, Python
  types (one existing reviewed exception), coordination scanning (zero
  reviewed sites), and whitespace checks passed. No new type allowance was
  introduced.
- The curated supply-chain manifest was regenerated after the connected code
  and generated clients, with digest
  `4585d5e64c1cf8ece73c8ed2cea22467a0670236b4f4f00c15f6039026e63815`.
  This identifies the reviewed input map, not a published release manifest.
- Red/green logs are retained locally under
  `/private/tmp/vonk-profile-assessment-*`,
  `/private/tmp/vonk-profile-readonly-red.log`, and
  `/private/tmp/vonk-profile-retry-effects-red.log`. These temporary logs are
  not release evidence or a substitute for rerunning the named tests at the
  eventual committed revision.
- Plan verification: all twenty package headings match the dependency table;
  the dependency graph is acyclic; eighteen local links/anchors resolve;
  code fences are balanced; and the three review/submission/reconnect examples
  parse without dispatch in the standalone environment. Primary interface
  references were rechecked, with deliberate differences in wait exits,
  signal handling and machine-output contracts retained explicitly. This is
  documentation validation, not a completed operator walkthrough.

W09/W10 remain open. Next inventory and coordinate workload/effect writers,
resource claims and node ownership, then connect managed deletion and complete
dispatch/retry fencing. The implementation plan names the next owning methods
and required PostgreSQL races. W11–W19 and the full objective remain open.

### Detailed-plan and source reconciliation — 2026-09-23

This checkpoint changes planning documents only. It records inspected source
and remaining work; it does not certify the current uncommitted implementation
or supersede the revision-specific test results below.

- Reconciled W09/W10 package states with `FleetProfileLoadRequest`, the actual
  profile routes, `_load_replay`, `_admission_session`, `_queue_application`,
  `_intended_profile`, and CLI load/recovery handling. The required review key
  and digest, authority checks, roster/catalog fences, original review root,
  and consent flow are present. The earlier W09a checkpoint remains historical
  evidence, not the current list of missing code.
- Expanded W09 into seven dependent slices, naming the exact remaining
  planner projection, workload/resource admission, managed-deletion, retry and
  connected-test boundaries. The Run/Switch preparation adapter currently
  returns preparation without carrying all planner admission decisions, so
  preparation alone is explicitly insufficient for the next closure gate.
- Specified provisional/accepted reference coordination and the distinction
  between managed deletion and unexpected disk loss. W09 owns the required
  shared protocol; W17's removal UI depends on it. This avoids a circular
  delivery dependency and does not assert atomicity between SQL and files.
- Expanded W10 into complete review, interaction matrix, uncertain submission
  and connected demonstration slices, including a review/detach/reconnect
  example. W11–W19 retain their scoped owners and acceptance requirements.
- Rechecked the primary interface references: CLI Guidelines; GitHub CLI
  formatting, environment, completion, watch and exits; Terraform plan/workflow;
  Kubernetes waits/pagination; Docker attach; Tailscale status; and AWS
  idempotent requests. This validates the cited design precedents, not Vonk
  runtime behavior or measured usability.
- Document validation: all twenty package headings match the dependency table;
  dependencies are acyclic; fifteen local links/anchors resolve; code fences
  are balanced; and `git diff --check` passes. All three new review/submission/
  reconnect examples parse in the standalone project environment without
  dispatch. The first parser check used the system Python, which lacked the
  CLI's `cryptography` dependency; repeating it in the pinned project
  environment passed. Existing staged content and other working files were
  preserved. Application tests were not rerun for this planning-only change.

### W09a reviewed-decision checkpoint — 2026-09-23

- Reproduced a stale-review defect through real profile preview and persisted
  workload rows: replacing an active run on the same Sparks, with the same
  alias and the same stop count, left the original review digest unchanged.
  The regression now passes and the internal apply path refuses that review
  without creating an application or dispatching a stop.
- `FleetProfileReviewedDecision` is the canonical semantic part of the public
  preview. It binds the saved definition/revision, full enrolled scope including
  idle members, resolved placement/ranks/endpoint aliases, exact model/image
  identities, asset reuse, preparation blockers, identified runs/installations,
  and overlapping pending orders. Summary stop/removal counts derive from the
  same typed effects shown by the CLI. The old hand-edited preparation digest
  dictionary is removed; immutable model/image fields have shared contract
  owners. Generation/verification times and partial-transfer counters remain
  observations, while readiness/reuse and blockers change the decision.
- Applications retain `reviewed_plan_digest` inside their canonical intended
  configuration. Their existing `plan_digest` still identifies the execution
  attempt. Worker intent reads validate the persisted review against its
  canonical decision. Internal `apply` reconciles an existing request before
  re-previewing mutable state, returns the original application after a profile
  edit, and rejects changed issuer or digest. This also avoids treating the
  accepted application's own pending work as a reason to reject its replay.
- The human CLI preview names exact endpoint/run IDs, installation effects,
  complete distributed groups, superseded orders, model/image digests, and
  target reuse. Current OpenAPI, Python clients, and TypeScript schema are
  regenerated together. There is no old-preview fallback.
- Validation: 112 control tests passed, including existing PostgreSQL-backed
  profile/recovery cases in OrbStack and the connected CLI/API parity case;
  103 standalone CLI tests passed. Four new review regressions cover replaced
  runs, transfer observation versus reuse/blockers, idle roster/definition
  changes, and replay after edits with issuer/digest binding. A final focused
  contract/review pass has 13 passing tests. Root Ruff, changed-file format,
  Python types (one existing reviewed exception), web production build, Rust
  wire consistency, coordination scan (zero reviewed sites), and diff
  whitespace checks pass. Offline supply-chain generation and verification pass
  with manifest digest
  `03a452598600a481c85afaad117dfe9ed948c58d7b141a663492eec97c2b7c43`.
- This checkpoint does **not** close W09/W10: the numbered load API still needs
  its required caller review digest/key, authoritative replay policy, and atomic
  roster/catalog/reservation/storage admission. The new tests do not yet prove
  those concurrent boundaries. CLI consent/script flags and the matching web
  load request belong to that connected change. No deployed or physical result
  is claimed; W09–W19 and the full objective remain open.

### W08e storage and process checkpoint — 2026-09-23

- A real lock-holding process and PostgreSQL-backed worker reproduced transfer
  starvation: one busy model object occupied the only transfer slot while an
  unrelated download stayed queued. Model writers now acquire their local and
  cross-process guards once, nonblockingly; contention records the exact object,
  owning cache writer, resume condition, and five-second next check, then
  releases the operation claim and execution slot. Waiting does not consume a
  failed-transfer attempt or alter verified bytes. The short deferral write
  checks current claim ownership/expiry and respects cancellation.
- A second red case placed four requests behind one busy object. The old
  `limit * 4` candidate scan repeatedly skipped them and never reached a later
  eligible download. Claims now inspect eligible-state candidates in order
  until their actual capacity is filled, without that undisclosed prefix cap.
  Both one-waiter and four-waiter PostgreSQL/process cases recover after release,
  while retaining original request identities and retry allowance.
- The OCI index-lock path also parked an image executor inside a polling loop.
  Its real process-lock regression failed before the change. Contention now
  returns immediately for durable rescheduling. Image contention and existing
  builder-capacity waits retain their execution retry allowance. An actual
  availability service, PostgreSQL, image files, and external index-lock holder
  remain queued through five waits with a two-failure transfer budget, then
  publish successfully when the holder exits.
- A separate model worker process exits without cleanup after a synced partial
  write and committed progress. A fresh process takes over its expired lease,
  reads from the exact retained file offset, verifies and publishes the complete
  object, and runs the CLI against registered Controller routes with the original
  request key. Reconnection issues only GET, returns the original operation,
  and reports success. That test uses a local API transport adapter; real HTTPS
  and interruption deadlines remain independently covered by W08c.
- Existing tests were reused for failed replacement preserving verified model
  and image files, missing bytes behind successful history, HTTP range resume,
  cross-service deduplication, cache receipt reuse, and distribution consumers.
  The synchronous deduplication fixture now lets the normal scheduler reconcile
  a contended second operation instead of expecting it to wait inside a slot.
- Verification: the storage/recovery sweep passed 204 tests. The final focused
  process/lock matrix passed all four PostgreSQL cases, including the additional
  image wait-budget case. These use actual managed fixture files and processes
  on the local host, with PostgreSQL in OrbStack. They do not claim a deployed
  NAS, published installer, or physical Spark result. The connected update,
  request-recovery, and recipe API sweep also passed all 40 tests. Root Ruff,
  formatting of six touched Python files, Python types with one existing
  reviewed exception, web build, Rust wire consistency, coordination scan
  (zero reviewed sites), and diff whitespace checks pass. Offline supply-chain
  verification passes with manifest digest
  `dec546236bbc317d3066f4821fc3687cac45d60b30f6ebba01abfde2a41ebe90`.
- W08's repository behavior is qualified. The combined M2 operator journey and
  installed qualification remain in W18/W19; W09–W19 and the full objective are
  still open. Next is the exact reviewed-decision admission boundary in W09,
  together with its current callers in W10.

### W08d durable recipe-update checkpoint — 2026-09-23

- Replaced the synchronous child loop and update array with one typed parent
  in the existing Job authority. Acceptance commits issuer, original key and
  scope, exact revision/content/execution identities, and deterministic child
  keys before any child admission. No new scheduler or persistence authority.
- `--all` derives its complete logical scope from authorized managed image
  receipts and actual files, selecting each current accepted recipe head.
  A 103-recipe fixture with no successful availability jobs proves that the old
  100-row/history selection is gone. Missing files exclude unavailable cache
  entries from fresh scope; replay preserves the original scope, including an
  empty successful no-op. Admission reserves serialized bytes for future child
  failures against the shared API/client document budget, with explicit refusal
  before dispatch when the complete scope cannot fit.
- The existing availability worker coordinates one child per short claim and
  releases its claim while waiting. Image execution retains its own capacity.
  Child admission checks current user authority, parent lease/fence, and exact
  frozen identity in one short transaction. Metadata, storage inspection, and
  child execution remain outside those transactions. Recorded waits expose
  owner, next observation/claim deadline, and resume condition.
- Real worker processes terminate before any child, after child commit but
  before parent linking, and between children. A fresh service adopts existing
  child effects by saved request key and creates only missing work. Concurrent
  PostgreSQL tests cover identical/different-scope submissions, takeover during
  admission and after commit, and revocation before child acceptance. Issued
  effects remain visible; one failed child does not suppress another.
- Regression checks exposed and then verified fixes for a malformed parent
  blocking unrelated claims and a corrupt persisted revision being admitted
  before comparison with its frozen parent identity. Malformed parent records
  are isolated and remain diagnosable in Activity. Real scheduler/service work
  confirms parent metadata waiting cannot consume the child's image slot.
- The recipe operation union, generated Python/TypeScript clients, standalone
  CLI submission recovery/following/rendering, and current web callers consume
  the parent receipt. A connected route/client test loses the committed POST
  response, finds the original parent, and follows that same ID to completion.
  Activity paginates parent summaries; complete child detail stays on the
  recipe operation endpoint. Broader child-detail pagination remains W14.
- Verification: 20 durable-update cases pass (15 local/process cases and five
  real PostgreSQL cases in OrbStack). The connected recipe/cache sweep passed
  78 cases; the final process matrix added two passing crash positions. The
  existing source-build lane now reads its model-child reference after the
  worker step, matching the already-current accept-before-child contract.
  All 265 standalone CLI tests and 45 subtests pass, as do 113 supply-chain
  checks and eight affected web component checks. Generated consumers, web
  production build, root Ruff, formatting of 16 touched Python files, Python
  types with one existing reviewed exception, Rust wire check, coordination
  scan (zero reviewed sites), and diff whitespace checks pass. The new modules
  are curated supply-chain inputs; offline manifest verification passes with
  digest `df76e3a0b08719e3463abdd4bafa9f8769c02ea3cf5446cba132ce729ec7c2fb`.
- This is repository evidence only. W08e, W09–W19, batch cancellation in W12,
  and the full CLI objective remain open; no publication or deployment is
  claimed. Existing staged/unstaged and unrelated files remain preserved.

### W08c elapsed network deadline checkpoint — 2026-09-23

- Replaced the default blocking urllib network path with the already-pinned
  HTTPX async stream behind the existing synchronous client boundary. One
  monotonic deadline covers connection, request delivery, headers, and all body
  reads. Generated JSON calls and file transfers use the same transport; file
  transfers retain their own budget and bounded memory use. No new dependency,
  generated API shape, Controller persistence, or web interaction change.
- Slow-body regressions failed on the original implementation before the fix.
  Real certificate-verified HTTPS checks now cover slowly arriving headers,
  202/403 bodies, closed connections, received status/request ID/retry delay,
  generated-client reads, refusal versus unknown acceptance, and no unjustified
  repeated POST. TLS errors remain typed TLS errors; redirects never forward
  an authenticated request. A 2 MiB artifact traversed real HTTPS upload and
  verified atomic download, crossing multiple stream/read boundaries.
- A second regression proved that the system resolver could delay return for
  1.531 seconds under a 250 ms budget. A process test then caught a resolver
  worker preventing CLI exit after the request had already timed out. The
  final transport shares identical in-flight lookups in a daemon worker that
  receives only resolver arguments. Expired callers discard late answers;
  repeated attempts do not accumulate workers for the same lookup. Completed
  addresses are not cached here. DNS remains an operating-system call, but it
  cannot submit HTTP work or keep an expired CLI process alive.
- Actual SIGINT during HTTPS body receipt exits 130, emits one JSON document
  with the original key and unknown acceptance, closes the connection, and
  sends no cancellation or second POST. This is local-process/network evidence;
  Controller/storage process recovery still belongs to W08e/W19.
- Verification: all 264 standalone CLI tests and 45 subtests pass, including
  the installed updater and twelve new TLS/DNS/process cases. The 32 connected
  cache/API/contract checks pass (four PostgreSQL lane cases retain their
  earlier separately recorded race evidence; no coordination changes here).
  Root Ruff, changed-file formatting, Python types with the one existing
  reviewed exception, web production build, Rust wire check, coordination
  scan, and diff whitespace checks pass. The new transport is included in the
  curated supply-chain inputs and generated manifest. The fixture now copies
  that new input, and all 109 supply-chain checks pass, including rejection of
  transport source drift. Final offline manifest verification passes with
  digest `97f64b504972e85ade24a099d644cca0c154521167ddb83107018208938297dc`.
- W08c's request-recovery boundary is implemented and qualified. W08d's durable
  update parent, W08e's managed-storage/process qualification, and W09–W19
  remain open. No publication, deployment, or physical Spark claim.

### W08 submission-recovery checkpoint — 2026-09-23

- Model and recipe downloads now perform one original-key lookup after an
  ambiguous POST. A matching issuer-owned request is adopted; only not-found
  permits one identical replay. Changed keys, actions, selectors, or recipe
  options are rejected. Malformed success receipts permit read-only diagnosis.
  A second uncertain POST, failed lookup, or interrupt retains the original key
  and reports acceptance as unknown. Human mode flushes the key and reconnect
  command before POST; JSON remains one final document.
- Submission schedules its three possible calls against one monotonic budget
  derived from the client's request timeout. Server retry delays are preserved
  rather than truncated to 30 seconds. A delay beyond the remaining budget
  defers replay; observation likewise reaches its deadline without polling
  early. At this checkpoint the scheduling boundary was implemented but the
  strict transport deadline remained open; the later checkpoint above closes
  that measured gap.
- Real client/registered-route tests inject connection loss, interrupted body
  reads, and malformed JSON after the recipe owner has committed acceptance.
  Each recovers and follows the original operation without a second POST.
  The model equivalent recovers after catalog/storage admission has changed.
  A qualified model selector exposed a route that rejected encoded slashes;
  both model mutation routes now preserve that valid selector form.
- Transport failures retain received status, request ID, and retry delay.
  A 403 remains a refusal with a lost or malformed body, including a long
  valid selector; partial body bytes are never disclosed. The new tests
  reproduced uncaught incomplete reads, overly generic JSON failures, loss of
  HTTP evidence, shortened retry delays, and rejected qualified model routes
  before their fixes. Inapplicable `with_model` is removed from model actions
  and the current web callers; all generated consumers were refreshed together.
- Verification: 252 standalone CLI tests pass (251 in the sandbox plus the
  separately run localhost updater check), with 45 subtests; 32 connected
  cache/API/contract checks pass. Ruff, formatting of all 58 changed handwritten
  Python files, types with only the existing reviewed exception, web production
  build, Rust wire check, coordination scan, and `git diff --check` pass.
  Supply-chain verification passes with curated manifest digest
  `8baeed0bb0b49eb1e1f6201db625a1c0692b4a2d5939ea790d4c580220bda90b`.
- **Gap recorded at this checkpoint, now fixed above:** a real, certificate-verified localhost HTTPS
  response sending one byte every 50 ms took 1.108 seconds under a 200 ms
  request timeout. It ended in receipt validation instead of deadline expiry.
  The original urllib timeout bounded socket inactivity, not total elapsed I/O.
  The scratch probe is `/private/tmp/vonk-cli-slow-response-check.py`; the
  durable regressions now live in `test_control_transport_deadline.py`.
  W08/M2 and the full twenty-package objective remain open.

### W08 original-request and following checkpoint — 2026-09-23

- Model selector downloads now recover accepted original intent before catalog
  resolution, disk admission, or a changed transfer preview. Cold-cache
  submission also passes the resolved model identity to its existing owner;
  previously it attempted to read a set that did not yet exist. Regression
  tests reproduced both the cold-cache failure and completed-request replay
  failure before their fixes.
- Recipe operations persist a canonical typed original selector, exact
  revision request, or retry request separately from derived execution flags.
  Replay checks the original issuer and every original option; force-download
  and force-rebuild no longer collapse to one replay binding. Changed-head and
  changed-force regressions failed before the fix. Parent acceptance commits
  before the worker can create or repair its model child, keeping child work
  outside the accepting SQL transaction.
- Both noun request-key routes return their current canonical operation
  document. Missing and foreign request keys have the same not-found response;
  operation-ID reads retain authenticated shared visibility. A viewer can
  inspect its original request but cannot replay its mutation. Registered API
  tests cover these distinctions and unknown recipe-download options are now
  refused rather than silently ignored. Generated schemas and Python/TypeScript
  clients were updated together with the existing web download caller.
- CLI progress accepts exactly one operation ID or request key. Key lookup
  binds to the returned ID, and every following observation must match it.
  Recipe download now follows its canonical `id`; missing identities cannot
  produce detached success. Interrupted submission retains its request key
  without claiming acceptance. The connected CLI/client/registered-route test
  finds and follows the original recipe operation through actual service and
  managed-file completion using a deterministic image transport.
- Four duplicate-insert tests pass against disposable PostgreSQL on the
  verified OrbStack engine. Two independent service instances reach the real
  insert after both absence checks: identical requests converge on one row,
  and changed issuer/intent is refused. Disabling only the two IntegrityError
  recovery handlers in a separate test process makes all four tests fail on
  the real unique constraints; product files were not altered for that check.
- Current verification: 229 standalone CLI tests (228 plus the separate
  localhost updater check), 45 CLI subtests, 191 focused cache/API/contract
  checks, four PostgreSQL races, and the typed lifecycle receipt consumer pass.
  Ruff and formatting of all 55 changed handwritten Python files pass; Python
  types retain only the existing reviewed exception. The web production build,
  generated-client refresh, Rust wire check, and coordination scan pass. No
  type or coordination allowance was added. Supply-chain verification and
  `git diff --check` pass; the regenerated curated manifest digest is
  `a124ebfb3040481c95492e0ea0a9f05d20c4afbd5d4df7392a8b81c01a3e4433`.
  These checks establish repository behavior, not physical image execution or
  full storage/process qualification.
- Remaining W08 work: the bounded POST/lookup/identical-replay helper and
  pre-submission human receipt; a durable update parent with complete frozen
  scope; parent/child process-death recovery through PostgreSQL and storage;
  final fresh-process request-key reconnection. Recipe update still has its
  existing transient response. This checkpoint does not complete W08 or M2;
  W09–W19 remain in the full plan.

### Implementation-plan refinement

- Expanded W08 into five dependent slices with a submission decision table,
  explicit request-key visibility, complete batch scope, worker ownership,
  restart behavior, and process-death limits. W12 and W19 now explicitly cover
  the update parent. This remains part of the original twenty-package scope.
- At that planning checkpoint, inspection confirmed that model selector
  previews ran before request replay, recipe selector downloads resolved the
  current revision first, recipe following missed its canonical `id`, and
  recipe update returned a transient array after iterating a silently limited
  historical scope. These
  were design inputs at that point. The later W08 checkpoint above records
  delivered fixes and their evidence.
- Rechecked the official interface references in the plan and added AWS's
  original-intent/idempotency guidance. Reference comparison validates the
  chosen patterns; application behavior still requires each package's tests.
  That refinement changed planning documents only and preserved all existing
  implementation edits and their staged state.
- Document checks confirmed all twenty ordered packages, acyclic dependency
  order, eleven local links, and balanced code fences; `git diff --check`
  passed. Application tests were not rerun for this planning-only refinement.

### W05 grouped Fleet checkpoint

- The Fleet overview presents each canonical run once, with Controller run
  state, owner-projected group health, exact membership, and per-rank state and
  freshness. Wide output also groups installations by their exact identity.
  Titles are presentation only: three different runs sharing one recipe title
  remain three runs. Filtered views retain reported members outside their
  selected nodes and never reinterpret the owner's health result.
- Reused the actual Fleet projection's exact-rank scenario to reproduce the
  missing grouped display before implementing it. The connected renderer
  checks cover three two-Spark runs, a stale rank, a failed route, an incomplete
  installation, and a filtered member view. This adds presentation over the
  existing canonical owner; no persistence or additional API field is needed.
- Fleet/operator/assessment checks passed 47 tests. The standalone CLI tree
  again passed 222 tests and 45 subtests; with the separately passing localhost
  updater test, the CLI evidence covers 223 tests. Types report one existing
  reviewed exception and no new errors; Ruff, all 45 changed handwritten Python
  files' formatting, and `git diff --check` pass. W05 implementation is complete;
  final installed and operator qualification remains in W18/W19. W08 cache
  submission recovery is the next unimplemented dependency in M2.

### W05 readiness checkpoint

- Recipe list/detail now carry separate fit, exact NAS cache, and readiness
  assessments. The existing planner and ModelCache owner provide the decisions
  and reasons. Current capacity includes live reservations; it does not assume
  another workload will stop. Exact recipe manifests include companion files,
  and authorized archive presence is required. A primary-model cached subset
  cannot make an incomplete recipe ready.
- Planner inspection cannot create/reset a planned build. The normal planning
  path retains that capability. The mapping authority owns candidate rank and
  role assignment. Shared preparation validation now keeps NAS readiness
  independent of unfinished Spark copies.
- `--ready` and `--fits-fleet` filter on the Controller before pagination.
  Ordinary reads assess only their page. Stale/unknown evidence and exhausted
  assessment budgets remain explicit; filters cannot report false empty
  success. Identity-only scans skip assessment. Human output shows each check,
  unique reasons, full candidate IDs and observation time; JSON preserves the
  complete canonical assessment.
- Six connected cases exercise actual Library, Run/Switch, ModelCache and file
  boundaries, including CLI/generated-client/API round trips, missing files,
  an unrelated cached subset, deleted image archives, stale capacity, later
  Spark/candidate selection, and discarded late results. Reintroducing build
  creation during inspection or bypassing exact-file checks fails the relevant
  regression. No physical Spark operation or deployment was performed.
- Library/planner/preparation/mapping checks passed 91 tests. A further
  API/profile/CLI-contract selection passed 39 tests, including six assessment
  cases repeated after adding the actual generated-client round trip. The
  standalone CLI tree passed 222 tests and 45 subtests; its localhost update
  server case passed separately with the required socket permission (223
  passing CLI tests total). Python types retain one reviewed exception and
  no new errors. Ruff, all 44 changed handwritten Python files' formatting,
  web build, regenerated Rust wire check, and coordination checks passed.
  Generated Python retains its generator-owned formatting.

### W05 selection checkpoint

- Profile authoring scans the complete accepted recipe library under one
  configurable deadline, including definition and Spark reads. Every request
  receives the remaining budget; late results, cyclic cursors, repeated
  selectors, and malformed rows refuse a save. Streaming pages avoids retaining
  complete recipe documents. Canonical selectors/logical recipe IDs and exact
  Spark IDs outrank friendly names; prefix matching remains refused.
- Model and recipe continuation cursors bind the matching collection's exact
  identities/documents as well as filters and ordering. Changing an accepted
  revision or matching membership requires restarting the read. No additional
  snapshot store or arbitrary total-record cap was introduced.
- Duplicate Spark names return distinct canonical IDs. The Controller's typed
  validation problem carries a separate optional candidate list, preserved
  through its central exception boundary and generated Python/TypeScript/Rust
  contracts. All node-selection routes now use the existing error translator;
  an ambiguous detail read previously escaped as an internal error.
- `fleet node-profile SELECTOR` resolves the canonical node, then reads its
  detail under the same deadline. Human output focuses on identity, labels,
  and lifecycle; JSON retains the full detail contract. The unused local
  generic selector helper and its isolated test were removed; regression
  coverage exercises the actual recipe/Spark resolvers and save boundary.
- Regressions failed before correction for unverified canonical selectors,
  identity/title collisions, cursor cycles, deadline overrun, discarded
  malformed rows, duplicate-name hints, changed accepted heads, and candidate
  loss through bounded errors. The connected authenticated API/generated
  client/CLI case preserves all 40 candidates in JSON and human errors.
- Controller/API/library checks passed 42 tests. The standalone CLI tree
  passed 221 tests and 45 subtests inside the sandbox; its existing localhost
  update-server test was denied a socket, then passed separately with the
  required local socket permission (222 passing tests in total).
- A further 60 Fleet/metrics/generated-contract checks passed. The bearer/
  cookie parity case now uses the real library projection over accepted
  canonical heads instead of authoring an absent recipe; it passed separately
  after that fixture correction (103 passing Controller tests across these
  selections). Python types retain one reviewed exception and no new errors;
  Ruff, web build, regenerated Rust wire check, and coordination checks passed.
  All 37 changed Python files pass formatting; `git diff --check` passes.
  Supply-chain generation and verification report no errors, with manifest
  digest `1982c3274f33bfef95c03dd1a244dcc78a825d6e91a3c4d6f8ce428357f7de33`.
- W05 remains open for shared Controller admission assessments, trustworthy
  `--ready`/`--fits-fleet` filtering, and grouped distributed placement output.
  No readiness, deployment, or physical qualification claim is made here.

### W02 presentation checkpoint

- Replaced flattened/truncated dictionary rendering and retired response-key
  fallbacks with task views selected by the command. Empty lists differ from
  malformed or missing records. Current nested `identity`, `local`, and
  `resources` contracts drive catalog output; full selectors and continuation
  cursors remain copyable. The unused selector-envelope fallback and progress
  forwarding wrapper were removed with their callers.
- Fleet shows authenticated connection/freshness, memory/disk evidence,
  actual workloads, and contextual warnings. Profiles show saved desired state
  beside observed state and both saved/loaded revisions. Preview shows scope,
  idle Sparks, steps, blockers, and the full plan digest. Application and job
  views retain identity, current phase, failure, and owner-projected recovery.
- Human output escapes terminal controls and accommodates ASCII streams.
  Tables are used only when their complete cells fit; narrow/long records
  become stacked fields. `--wide` adds catalog/node detail. Byte values retain
  zero and distinguish unavailable totals; an explicitly unknown total never
  creates a percentage. JSON remains the complete validated data, separate
  from human display formatting and the private enrollment delivery path.
- Resource watches append changed snapshots to stderr and emit their final
  snapshot on stdout. Their deadline message no longer implies that a read
  accepted remote work. Operation observation retains its separate progress
  behavior and durable identity.
- Regressions reproduced lost profile intent in presentation, invented known
  transfer totals, generic job failure output hiding the actual agent reason,
  and Unicode failure on ASCII output before correction. Width checks cover
  60/80/120 columns, long selectors, Unicode, zero/unknown measurements, stale
  evidence, missing records, and blockers. Existing rendering fixtures now
  use current nested resource fields rather than retired flattened shapes.
- Focused CLI/process/transport/error checks: 150 passed in 6.15s. Broader
  standalone CLI tree: 211 tests and 45 subtests passed in 4.67s. Three actual
  CLI/generated client/API cases passed, including a human profile read of
  persisted installed-only intent and the enrollment delivery/recovery paths.
  This is not a substitute for the remaining W19 connected scenarios.
- Root Ruff, changed-file format checks (30 files), Python types (one existing
  reviewed exception), web build, Rust wire check, and coordination scanner
  passed. Follow-up rendering/command checks passed 50 tests after removing
  unused legacy fixture fields. Supply-chain generation and verification
  passed with no errors. Final installed/PTY/operator qualification remains W18/W19; no
  published, deployed, or physical acceptance claim is made here.

### W04 enrollment checkpoint

- Required `--output` reserves a new private regular file and persists the
  nonsecret request identity before issuance. The exact grant is delivered
  only to that file; human/JSON output contains a receipt and recovery commands.
  Existing files and symlinks refuse issuance. A reproduced setup failure
  leaked a reserved file/descriptor; cleanup now removes only its own file,
  closes the descriptor, and reports that issuance was not attempted.
- The caller's canonical UUID4 becomes the grant identity. Duplicate identity
  cannot issue another secret. Status and revocation require administrator
  authority and the original issuer. PostgreSQL stores the token digest, never
  a recoverable token. The fresh schema adds the grant's `revoked_at`; this is
  a schema-changing checkpoint and must not be auto-merged.
- Response loss looks up the original identity without repeating issuance.
  A confirmed issued grant whose file delivery fails is revoked only while
  still unused. A consumed grant remains consumed; this is not certificate
  revocation. Denied issuance performs no follow-up request or authority bypass.
- The actual CLI/generated client/authenticated API/enrollment service tests
  cover successful private delivery and redemption, accepted response loss,
  explicit revocation, ownership denial, and denied re-enrollment. A real
  PostgreSQL race between separate service instances proves consumption and
  revocation serialize; exactly one effect wins.
- The broad checkpoint passed 137 CLI tests and 224 Controller/API/contract
  tests (14 lane tests deselected). The PostgreSQL race ran separately and
  passed. Subsequent focused private-file/profile checks passed 32 tests,
  including failures at actual file and directory sync after secret bytes
  were written, interruption, and cleanup of failed reservations.
- Python types passed with the one existing reviewed exception; the web build,
  generated Rust wire check, and coordination scanner passed. Static checks
  and supply-chain verification also passed at the following W02 checkpoint.
  Hard process-death acceptance remains W19; an in-place grant write is not
  claimed to be an atomic file replacement. No deployment or physical claim.

### W07 authoring checkpoint

- Extracted `FleetProfileDefinition` from the input contract and generated the
  authenticated definition route/client schemas. It reads canonical saved
  fields without consulting cache, catalog, or runtime; malformed stored data
  is refused. The existing profile projection includes the same definition for
  active web consumers. No new persistence owner or schema migration.
- Omitted `expected_revision` now means create-only (`0`), never unconditional
  replacement. Existing writes require the observed revision. CLI edits reject
  an explicitly requested revision that differs from the definition read;
  imports use the supplied precondition without performing a load.
- CLI regression tests failed before fixes for discarded metadata, absent
  configure/export/import, removing a Spark outside the chosen assignment, and
  authorizing a write with a different revision from the one read.
- Actual CLI + generated client + authenticated API + SQL service round trip
  preserved metadata, installed state, assignment name, and variant through
  import, rename, export, and stale import refusal. Progress remained absent:
  saving never submitted an application. This uses SQLite for persistence,
  not as evidence of concurrent locking.
- Two real PostgreSQL races on the verified OrbStack engine passed: simultaneous
  creation and simultaneous edits each accepted only one writer at the observed
  revision. Definition reads also passed with a deliberately unavailable cache
  resolver and refused malformed stored assignments.
- Focused CLI/process/client/error/presentation run: 127 passed in 5.63s.
  Controller/API/contract suite: 91 passed, 4 lane tests
  deselected; the two new PostgreSQL races ran separately and passed.
- Active web edits preserve authoring intent instead of inferring it from live
  state. A connected composer regression also exposed a self-aborting request
  that left profile selection disabled; the request lifecycle was fixed. Five
  related browser component tests and the production web build passed.
- Root Ruff, changed-file formatting, Python types (one existing reviewed
  exception), generated Rust wire check, coordination scanner (zero reviewed
  sites), and offline supply-chain verification passed. Generated OpenAPI and
  both clients were refreshed together. No deployment or physical Spark claim.

### Observation foundation

- Before edits: 75 existing command/transport/error tests passed in 7.74s.
- New process regressions reproduced human errors on stdout, failed-read exit
  confusion, successful blocked previews, online no-command startup, silently
  clamped/NaN wait values, implicit profile mutation, progress in result pipes,
  repeated broken-pipe writes, terminal-control injection, and missing offline
  completion before their fixes.
- Exact-follow regression reproduced switching to a newer application;
  deadline regression reproduced a 30-second sleep under a 0.1-second budget.
- Fleet-filter regression reproduced dropping filters after the first read.
  Credential FIFO regression reproduced a blocking open before file validation.
- Focused CLI/process/client/error/presentation/update run: 119 passed in
  13.01s at the observation-foundation checkpoint, including the real updater
  installation and shell syntax/completion checks.
- Existing connected CLI contract harness: 1 passed. It remains a small test
  server and is not evidence for real admission or worker behavior.
- Existing signed updater suite, including real wheel installation in a fresh
  environment: 12 passed in 7.52s with a task-specific dependency cache.
- Root Ruff lint passed. Python types passed with the existing one reviewed
  exception; no new allowance. Web dependency install and production build passed.
- Formatting checks pass for all 11 changed/new Python files at this
  checkpoint. Repository-wide formatting also reports unrelated pre-existing
  files, including `.tmp/recipes-reasons.py`; those files were not reformatted.
- Rust wire regeneration check and supply-chain verification passed. The
  curated file-digest manifest was regenerated with the changed build inputs.
- No actual profile admission race, process/storage cancellation acceptance,
  deployed Controller, physical Spark, or operator usability gate has run yet.

Next: enforce the implemented reviewed decision at the numbered load API and
atomic admission boundary, then connect CLI consent/script input and the current
web caller in W09/W10. Concurrent roster/catalog/reservation/storage races still
need their dedicated PostgreSQL evidence.
Keep the full plan active; these foundations do not complete the CLI objective.
