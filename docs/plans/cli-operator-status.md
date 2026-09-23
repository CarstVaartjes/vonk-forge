# CLI implementation status

Implementation is active on `codex/cli-operator-experience`, based on
`49389f3dced50d2ecf7680e256dbe1827817eaee`. The full objective remains the
[twenty-package implementation plan](cli-operator-implementation.md).
This record distinguishes implemented pieces from completed packages; nothing
below claims deployment, hardware acceptance, or complete web parity.

The user authorized implementation and routine decisions, including necessary
fresh-schema changes, and confirmed there is no active production environment.
Existing unrelated `.tmp/` files and `docs/handover-spark-canary.md` are preserved.

## Package state

| Package | State | Current evidence / remaining work |
| --- | --- | --- |
| W00 | Inventory established | Current command/route map below; initial 75 focused tests passed in 7.74s. Node-profile owner identified; its missing command belongs to W05. |
| W01 | Implemented process foundation; package open | Contextual exits, human streams, JSON, explicit profile selection, finite timing, no-input, Ctrl-C and pipe handling. Remaining: apply the consent policy to newly delivered consequential workflows. |
| W02 | Implemented current task views; final installed qualification in W18 | Explicit Fleet/Library/Profile/review/application/job/error views; complete identifiers, truthful unknown/zero values, adaptive widths, ASCII-safe text, and append-only resource snapshots. New workflow contracts add their presentations in the owning later package. |
| W03 | Implemented; final qualification pending | Offline orientation/help/version, parser-derived Bash/Zsh completion, connection check, nonblocking rejection of nonregular credential files. Operator walkthrough remains W19. |
| W04 | Implemented; final process qualification in W19 | Exclusive private grant file, original request identity, owner-authorized status/revoke, and lost-response/disk-failure recovery. Enrollment/consumption authority remains server-owned. |
| W05 | Implemented; final installed qualification in W18/W19 | Complete bounded selection, canonical identity priority, typed ambiguity candidates, catalog-change cursor refusal, read-only owner-derived fleet-fit/cache assessments, and grouped distributed placement display. |
| W06 | Implemented observation foundation; package open | Exact profile ID pinning; profile request/application selectors; noun-owned model/recipe/fleet-job progress; bounded sleep/request budget; distinct timeout/interruption documents and reconnect commands. Receipt/recovery integration for later workflows remains. |
| W07 | Implemented | Canonical definition read, preserving edits/configure, installed/running intent, private export and bounded import; stale and concurrent writes refused. |
| W08 | Implemented and repository-qualified | Original-key recovery, HTTPS deadlines, durable update parents, process restart adoption, and real storage recovery are verified. Busy model/image writers release execution slots and reschedule without consuming transfer retries; eligible work remains reachable. Final installed and combined operator acceptance remain W18/W19. |
| W09 | In progress: reviewed decision and resource ownership; admission still incomplete | W09d evidence below adds one effect projection, PostgreSQL workload/capacity fences, physical-pool memory accounting, durable disk/port/memory claims and child handoff. Exact preparation builds inherit parent memory, preserve declared reserves and reconnect before mutable capacity checks. Build cancellation now belongs to the exact attempt, preserving valid requests and verified images; new intent can proceed after cleanup without reviving cancellation. Installed intent no longer succeeds with an empty queue. Measured usage, post-stop evidence, shared-consumer cancellation/replanning, common lock order, asset lifetime and exact rebuilt-image admission remain open; follow W09d–W09g. |
| W10 | In progress: consent, headroom and submission recovery verified | Interactive review, explicit scripted digest/consent, capacity display, bounded original-key recovery and the required web load body have affected tests. Complete route/effect presentation and qualify the whole W09/W10 boundary before closing M3. |
| W11 | In progress in isolated branch; not integrated | Model cancellation separate from eviction. |
| W12 | Planned | Recipe cancellation with shared-child ownership. |
| W13 | Planned | Profile cancellation and issued-effect reconciliation. |
| W14 | In progress in isolated branch; not integrated | Canonical activity pagination and authorized resume. |
| W15 | In progress in isolated branch; not integrated | Profile-to-published-endpoint discovery. |
| W16 | In progress in isolated branch; not integrated | Complete artifact-job input, execution, and verified-output flow. |
| W17 | Fleet maintenance in progress in isolated branch; not integrated | Sequential signed fleet maintenance is independent. Reviewed removal still requires W09's reference protocol. |
| W18 | Started | Real entry-point and shell tests; wheel build and existing signed updater installation verified. Final installed surface and all implementation checks remain. |
| W19 | Planned | Actual service/PostgreSQL/storage acceptance and operator walkthrough; no deployed or physical claim. |

The user authorized six GPT-6 Luna agents at maximum reasoning on 2026-09-23.
Five isolated branches cover W11, W14, W15, W16 and W17's fleet-maintenance
slice. A sixth audits W09 admission and upstream overlap, then implements the
bounded published-image storage-recovery gate. The shared checkout qualifies
source-image reapproval and coordinates integration. The isolation baseline is `9708f54c`,
recorded on current `origin/main` (`9e2e166b`) with the ongoing task's files
overlaid. This is a collaboration snapshot, not evidence that all upstream
changes have been reconciled. Integrate only each agent's incremental work,
resolve overlaps deliberately, regenerate shared contracts once, and qualify
the combined result before changing these states to implemented.

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
| `fleet remove` | `POST /api/fleet/{selector}/remove` | Node revocation/removal; final consent treatment W17 |
| `fleet upgrade` | `POST /api/fleet/upgrade`; AgentUpgradeService | Signed upgrade; receipt's current `operation_id` identifies a job; W17 will complete following and remove all-at-once |
| `fleet progress JOB_ID` | `GET /api/jobs/{job_id}` | Exact job snapshot; `--follow` awaits its outcome |
| `fleet loginfo` | `GET /api/fleet/{selector}/loginfo` | Bounded collected diagnostic read; no SSH |
| `model`, `recipe` | `GET /api/model`, `/api/recipe` | Controller cache/operation views; bounded watch |
| `model library`, `recipe library` | Singular noun `/library`; LibraryProjection | Page/cursor and task facets; public catalog differs from cached availability |
| `model detail`, `recipe detail` | Singular noun `/{selector}` | Exact selector/detail; technical option and bounded watch |
| `model download`, `recipe download` | Singular noun `/{selector}/download` | Schema-2 request key; follows noun operation or detaches |
| `recipe update SELECTOR` / `--all` | `POST /api/recipe/update`, recipe request/operation reads | Durable frozen scope; follows by default; original-key recovery and explicit empty success |
| `model remove`, `recipe remove` | Singular noun `/{selector}/remove` | Explicit cache eviction; recipe dependency choice; not cancel-only |
| `model progress ID`, `recipe progress ID` | Singular noun `/operations/{operation_id}` | Read succeeds independently of remote state; follow awaits terminal result |
| `model progress --request-key`, `recipe progress --request-key` | Singular noun `/requests/{request_key}`, then the exact operation route | Original issuer plus current read access; key resolves once; no authority or ID visibility change |
| `profile`, `profile list` | `GET /api/profile/{number}`, `/api/profile` | Stable numbered profiles; reads may default visibly to 1 |
| `profile name`, `add`, `remove`, `configure` | `GET /api/profile/{number}/definition`, then numbered `PUT` | Complete saved definition; explicit selection; observed revision; omissions preserve intent |
| `profile export`, `import` | Definition GET / numbered PUT | Export contains authoring fields only; private exclusive file or stdout; bounded import requires an explicit revision and never loads |
| `profile load --dry-run` | `POST /api/profile/{number}/preview` | Read-only review; blocked preview exits 2 |
| `profile load` | `POST /api/profile/{number}/load` | Required reviewed digest and request key; current authority and original-request replay; remaining workload/resource/storage coordination in W09 |
| `profile progress` | Numbered latest/request lookup, then `/api/profile/applications/{id}` | Resolves latest once; follows exact identity; direct application checks profile membership |
| `--check-connection` | Local token/origin checks, then `GET /api/fleet` | Read only; no configuration or credential issuance |
| No command, help/version, `completion` | Local parser/build metadata | No credentials, remote lookup, or update check |
| `update`, `update --apply` | Signed installer publication; CLI updater | Checks or installs the CLI wheel, independently of Controller/fleet upgrade |

The web's profile save/load, library/cache, fleet, activity/resume, and
artifact-job methods were checked in `control/web/src/api/client.ts`. Activity,
artifact jobs and endpoint discovery remain assigned to their packages above.
Browser authentication/token issuance remains the
explicit bootstrap dependency; it is not silently labelled CLI parity.

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
