# Agent guide

This repository is the Vonk Forge platform and control plane. The reviewed
recipe definitions live in the sibling `/opt/vonk-forge-recipes` checkout.
Keep repository, CI/publication, Controller deployment, and physical Spark
qualification as separate evidence boundaries.

## Operator access

Prefer authenticated CLIs and HTTP APIs for operational work. Follow the
[operator CLI runbook](docs/runbooks/operator-cli-access.md) and, when present,
the execution host's `~/.config/cli-access/README.md` for configured access.
Use installed command help; do not open a browser or desktop app for an
operation the configured CLI supports. Documentation research is separate.

Distinguish sandbox/network/socket denial from missing authentication before
requesting login. Keep credentials out of output, arguments, and tracked files;
never fall back from a scoped service account to a personal desktop session.
Existing credentials do not authorize unrelated remote changes. A locked,
awake user session is not proof of availability after logout, sleep, or reboot.

## Engineering stance

Vonk Forge optimizes for simplicity, stability, security, and automatic recovery
while keeping every frontier recipe runnable on local Spark capacity. Read
[docs/engineering-principles.md](docs/engineering-principles.md) before making a
structural choice. The short form:

- **Simplicity:** one Compose application, one owner per fact, one current
  execution path per operation. Kubernetes, a service mesh, an event bus, custom
  microservices, and a mandatory Vault are explicit non-goals. GPU nodes run
  workloads, never ingress, databases, monitoring, or the admin UI.
- **Stability:** state before controls, live-versus-desired before mutation, and
  one Spark at a time for consequential fleet-wide platform change. Keep
  workload profile changes topology-atomic. Remove retired paths together with
  their callers instead of carrying compatibility shims.
- **Recovery:** converge automatically to the latest authorized intent after a
  recoverable fault clears. Reuse completed work and durable partial progress
  through the normal execution path. Routine recovery must not require manual
  retirement, Idle/reapply, or a new recipe revision to escape poisoned state.
- **Transparency:** show truthful progress, the meaningful cause, and the next
  action or retry. Unknown, waiting, refused, and completed are distinct states.
- **Security:** fail closed. Never soften a `PermissionDenied`, never clamp an
  invalid request into a valid one, keep the update-signing key separate from
  administrative authorization, and never let candidate tooling install itself.
  Spark SSH is diagnostic and bootstrap-only. NAS Compose redeployment follows
  the explicit host-operation boundary below.
- **Every frontier recipe:** the curated library exists to make any model
  reproducible, not to restrict what can run. Missing assets are actionable
  cache blockers and never a problem deferred to a Spark. Runnable, cache-ready,
  and physically qualified are separate claims.

The linked engineering and architecture documents own durable policy. Plans
record implementation progress; dated handovers record evidence. Neither an
old workaround nor a status snapshot authorizes a new operational action. Keep
one current action list and verify changing claims against their actual owner.
Keep commands in their linked runbooks so this guide stays focused on decisions.

## Shared decisions and operator visibility

The Controller owns planning, admission, orchestration, and durable progress.
CLI and web consume the same typed decisions and receipts; do not add a client
planner, independent refusal classifier, or second recovery implementation.
Preview and execution share one decision: bind the reviewed effects and exact
identities, revalidate at acceptance, and reject changed effects instead of
silently broadening consent. Name predicate conditions once and derive both
the decision and its explanation from those same conditions.

Every operation exposes its phase, measured progress, meaningful failure cause,
waiting dependency/owner, next attempt, deadline, and required operator action
where applicable. Preserve safe typed reasons and correlation identity across
helper, agent, Controller, and clients under the
[error policy](docs/error-reporting.md). Do not truncate away the cause or invent
an ETA, percentage, missing value, or success when evidence is unknown.

Persist request identity before effects and reconcile a lost response before
retrying. Reconnect to the original operation. A client timeout, disconnect, or
Ctrl-C stops observation; remote cancellation is explicit and retains its own
authorization and cleanup semantics. Read-only progress views must not advance
or block the work they observe.

## Resource accounting, budgets, and bounds

Follow [the resource policy](docs/engineering-principles.md#resource-accounting-and-budget-policy).
Classify a rule by what it protects, not by the word "budget":

- Estimates and heuristic forecasts expose value, unit, source, and uncertainty.
  Warn without permanently poisoning otherwise admissible work.
- Actual allocator, device, container, storage, reservation, and concurrency
  limits remain enforced. Temporary shortages wait with a visible resume
  condition; invalid requests receive actionable refusals.
- Authorization, integrity, contract, and exact-plan checks fail closed. Retry
  cannot change the model, runtime, context, topology, or image unless that
  alternative is explicitly permitted and bound in the accepted plan.
- Bound attempts and retry rates. Persist deadlines across restart, expose the
  next check, and reconcile at expiry. An exhausted attempt does not permanently
  ban a fresh authorized request; resetting its deadline indefinitely is not
  recovery.

Count each physical resource once. Keep observations, reservations, future
promises, and estimates distinct; inherited parent/child claims must not compete
with themselves. A planned stop or terminal row does not prove capacity free.
Release claims only after reconciling exact effects. Retain recipe-declared
system reserves and account for unified memory as one physical pool.

Derive bounds from bytes, time, disk, memory, or another protected resource.
Avoid arbitrary structural counts; report the limit and observed value. All
layers must accept values permitted by the canonical plan contract, including
empty or multiline arguments, or reject them at the owning compile/admission
boundary before effects.

## State ownership and resilient recovery

Read the [ownership boundary](docs/architecture-overview.md#state-ownership)
before changing persistence. PostgreSQL remains the authority for identity,
permissions, accepted catalog revisions, saved profiles, desired fleet state,
operation intent, leases, cancellation, reservations, and audit. It coordinates
API and worker decisions transactionally; LiteLLM also uses this PostgreSQL
service through its own database. Replacing PostgreSQL is outside this change.

The target architecture makes managed artifact storage authoritative for model
files, image archives, verification manifests, and local download/build
checkpoints. Self-descriptive records use canonical typed JSON beside the work.
Do not independently maintain these facts in SQL and files. A derived index is
disposable, rebuilt from its owner, and never a second admission authority.
SQL may reference an exact artifact identity without owning its availability.
Use a database only where its transactions, coordination, or queries reduce
total complexity; do not build a second job scheduler out of JSON files.

This is a target boundary, not a claim that the ownership cutover has shipped.
Follow the [implementation plan](docs/plans/resilient-artifact-storage.md).
Move each producer, reader, API projection, and meaningful test together, then
remove its obsolete persistence path. Never add dual writers, fallback readers,
or a schema-1 compatibility path to make the transition appear complete.

- Existing valid artifacts are reused; compatible partial transfers resume.
  A failed attempt or stale status must not permanently block a new download,
  refresh, or rebuild. Preserve the last verified result until its replacement
  is verified and published. Rebuilding may produce a different image digest;
  never silently substitute it into a bound plan or running workload.
- Recover automatically within current authorization, with bounded retry rates,
  visible next attempts, and durable checkpoints. Reconcile exact effects before
  retrying. A fresh explicit rebuild has a new request identity; retrying an
  existing request preserves its identity. Cancellation and newer intent win.
- Publish complete verified artifacts with crash-safe storage ordering and
  atomic visibility. Serialize writers per artifact and fence stale attempts.
  A JSON `running` field proves neither a live worker nor completed bytes.
- Invalid contracts, denied access, revoked authority, and integrity failures
  remain explicit blockers. Repair corrupt partial work under the managed
  storage contract; never bless it as valid or weaken a check to keep moving.
- Garbage collection must coordinate with authoritative references and active
  work. An unavailable database or failed scan does not prove an object unused.
  Artifact discovery cannot recreate users, grants, profiles, or authorization.
- Test process death, restart, duplicate requests, cancellation, storage loss,
  and eventual recovery through the real storage and worker boundaries. Keep
  repository, deployment, and physical acceptance claims separate.

### Deadlock prevention is an architectural requirement

Follow the [coordination boundaries](docs/architecture-overview.md#coordination-and-deadlock-prevention).
Every wait must name its dependency, owner, deadline, and resume condition.
Waiting work releases execution slots and database transactions. A parent must
never hold a slot its child needs, and a child inherits its parent's node
ownership instead of competing for it. Reject dependency cycles before dispatch.

Never acquire or wait for an artifact lock inside a SQL transaction. Artifact
locks use nonblocking acquisition; busy work is rescheduled. The only permitted
nesting is an artifact lock followed by a short, nonblocking SQL ownership
check or lease renewal.
SQL transactions acquire explicit and implicit locks in the documented common
order, use bounded lock/statement/transaction budgets, and never span transfer,
build, subprocess, external HTTP, child completion, or retry sleep. On conflict,
roll back and release resources before scheduling a bounded retry. Deadlock or
timeout recovery must preserve intent and partial work without broadening grants.

An expired lease alone does not prove an old executor stopped. A new attempt
must fence old results and reconcile exact effects before taking over. Scope
failures to their owner; do not turn a busy artifact, malformed history row, or
unavailable source into a barrier for unrelated eligible work. Verify these
rules with concurrent PostgreSQL/process tests, not mocked locks or SQLite.

## Verification and completion

The [testing policy](docs/testing-and-ci.md) owns verification commands,
worktree-local environments, OrbStack setup, lane selection, pinned lint/type/
generation checks, and reviewed baselines. Use those instructions from the
active task worktree. On macOS, check the intended OrbStack engine before
calling a container/Linux lane unavailable. Physical NVIDIA, fabric, and model
quality evidence still requires its designated lane.

Every test must name a wrong implementation it catches. Prefer behavioral
boundaries and real producer/store/consumer seams; do not duplicate constants,
field lists, workflow text, or schema shapes already owned elsewhere. A bug
fix ships with a regression demonstrated to fail before the fix. Follow
[what earns a test](docs/testing-and-ci.md#what-earns-a-test).

Completion means the authorized user workflow works across its producers,
storage, workers, and consumers, including recovery after the injected fault
clears. Passing isolated components, writing a handover, or shrinking the scope
to a passing subset does not close an unfinished integration boundary. Record
exact revisions, relevant checks, and remaining evidence gaps. Source completion,
merge, accepted publication, deployment, and physical acceptance are distinct.

The coordination scanner detects defined syntax patterns; a passing scan does
not prove arbitrary runtime code deadlock-free. Use real PostgreSQL and process
checks for the claimed concurrency behavior. Fix violations and remove stale
baseline entries; never expand a reviewed baseline to conceal a new violation.

## Current contracts only: no legacy compatibility

This is a first internal release, not a migration. Keep one current definition
and one current execution path for each document and operation. Remove retired
parsers, DTOs, database models/tables, endpoints, CLI compatibility aliases,
fixtures, packaged assets, and callers together. Do not retain deprecated
constructors, old field names, dual readers/writers, or fallback shapes merely
to keep old tests or old worktrees working. The current parser and
[operator runbook](docs/runbooks/vonkctl.md) own command vocabulary; do not
restore the obsolete `fleet profile` alias from a historical guide.

Published Model and Recipe structures are defined by the canonical Pydantic
package in vonk-forge-recipes. Controller APIs and Controller/Spark messages
use their authoritative nested Pydantic models. Validate persisted contract
JSON on reads and writes; malformed data must not become empty/default state.
Generate OpenAPI and Python/TypeScript clients from the current API. Generate
Rust wire structures with typify from the Pydantic-derived JSON Schemas; do not
maintain parallel handwritten payload fields. Keep handwritten semantic and
security validation, and pass connected producer/consumer tests that preserve
required-field presence, nullability and strict structure.

Every application API route must appear in the full OpenAPI schema. Do not use
`include_in_schema=False` in production routes. Declare byte uploads, downloads,
and streams explicitly too. Derive narrower client schemas from the complete
schema; never hide the underlying route to limit a client audience. Keep the
discovered-route completeness gate in CI, including its deliberate hidden-route
rejection test.

Consume wire and persisted JSON with the canonical model's JSON validation
semantics, including JSON already decoded by the database driver. Do not treat
JSON arrays as malformed Python tuples or disable strict validation to conceal
that mode mismatch. Test the actual serialize/store/load/consume path.

Optional fields with a declared `None` default accept omission and explicit
`null` as the same value. Omit those unused fields when sending documents.
Normalize through the authoritative model before hashing or signing, and use
the identical canonical representation when verifying in Python and Rust.
Do not make an optional field required merely to repair a serialization
mismatch. Required nullable fields remain required and retain their `null`.
Preserve meaningful `false`, `0`, empty values, and engine-owned JSON values;
never recursively strip every `null` from arbitrary dictionaries. Apply only
declared defaults, never defaults that hide malformed required data. Preserve
formatted string values as strings; a date-time format is not permission to
rewrite a timestamp's spelling. Test actual producer-to-consumer round trips,
including signatures/digests and both missing/null forms, not just acceptance
by two parsers. See `docs/api-contracts.md` for the serialization policy.

Allow engine-owned content through its declared extension fields; do not
introduce an exhaustive engine-argument allowlist.

Test fixtures and health probes are consumers too: use the same typed contract
as the producer, rather than raw dictionary equality or duplicated key lists.
For external protocols, model their documented required/optional fields and
extension behavior; keep deterministic test-content assertions separate from
structural validation. An omitted optional default is not a security failure.
Exercise normal streaming and non-streaming paths where the protocol supports
both.

Update producers, consumers, documentation and meaningful tests together.
A current document's version number is not a reason to introduce another
version reader. Historical audit documents may remain clearly marked as
history; they must not be imported, packaged as active configuration, or used
as instructions to restore old behavior.


This is a greenfield deployment. Active installer, release-publication,
catalog, recovery, and authority paths use schema 2. Do not add schema-1
fallbacks, dual readers/writers, migration shims, or stale schema-1 fixtures to
those paths. Keep schema 1 only where the code explicitly defines it as the
current wire/build/job/evidence contract or as an inert historical migration;
the `/api` URL prefix is API versioning, not permission to restore an old
document schema. “Dual-Spark” means two-node topology and remains supported; it
does not mean maintaining schema-1/schema-2 runtime paths.

## Recipe-library checks

Build and validate the sibling recipe checkout against the exact platform
worktree using [the recipe-library procedure](docs/operators/recipe-library.md#validate-a-checkout-locally)
before calling its contract installable. Record both commits, the recipe digest,
and the evidence level. Structural qualification proves an executable contract;
it does not prove container or physical model acceptance. A candidate with a
valid contract may proceed under normal authority, capacity, and exact-cache
checks without prior physical qualification. Keep that unproven status visible.

## Deployment and fleet safety

Routine Spark package upgrades are Controller-authorized and signed; do not use
SSH as the rollout path. `vonkctl` exposes one authorized upgrade command; there
is no separate candidate/preview/apply or plan-digest subcommand:

```bash
vonkctl fleet upgrade Atlas --strategy one-at-a-time --yes --json
vonkctl fleet upgrade --all --strategy one-at-a-time --yes --json
```

For a mounted controller project, consume the signed NAS installer from the
directory containing the existing bundle, preserve `.env`, `secrets/`, and
named volumes, then redeploy through Docker Compose over approved host access.
Prefer the configured headless CLI to the NAS browser interface; follow the
[NAS redeployment procedure](docs/runbooks/operator-cli-access.md#nas-compose-redeployment).
Inspect the published manifest first: it must be schema 2 and bind the intended
current source and artifacts. Do not delete PostgreSQL volumes or run `docker compose down -v` for
a normal upgrade. An explicitly authorized NAS Compose redeploy may use SSH as
its host transport; do not request another approval merely for that transport
when the operation is already authorized. This exception does not permit SSH
Spark rollouts or bypass a Controller authorization decision.

## Profile cache contract

The local NAS/Controller cache is the authoritative availability surface for
Fleet profile authoring and apply admission. Profile choices must resolve from
that cache to an exact model and recipe-image identity; a public catalog entry
or a Spark-local copy is not sufficient. Persisted profiles still belong to
the current PostgreSQL authority and schema-2 contracts, but their choices and
readiness are cache-backed.

Missing model or recipe-image assets are actionable blockers. Preview and
authoring must name the missing asset and expose the current prepare-cache
action; do not silently admit a profile and defer the problem to a Spark.
The local NAS cache is trusted under its managed-storage contract, so do not
promise costly repeated full hashing as part of ordinary profile reads or
admission.

Applying a ready profile binds the exact profile and plan digests, fans out
exact model and recipe-image preparation to the target Sparks in parallel, and
skips assets already local on each target. It must stop and replace workloads
safely, retain durable per-target progress, and report that progress before a
route is published. NAS reconciliation removes unused local model-cache
entries while preserving profile and active-workload references. Spark-local
copies are execution caches only: they may be reused or rehydrated, but they
never become profile authority.

Keep these readiness and admission facts separate from repository, CI and
publication evidence, Controller deployment evidence, and physical Spark or
model-quality acceptance. A passing profile preview or cache operation is not
physical Spark qualification, and SSH must not become an undocumented
alternative rollout path.

## Checkout, pull requests, and worktree lifecycle

`/opt/vonk-forge` is the canonical checkout of local `main`, tracking
`origin/main`. Keep it clean and fast-forward it after fetching before starting
work and after merges. A fetch updates `origin/main`, not local `main`. One
coordinator updates the canonical checkout while it is idle; agents must not
switch it to feature branches or use it as an integration workspace.

All repository edits, including documentation, agent instructions, lead-agent
and subagent work, use an isolated `codex/` branch/worktree based on freshly
fetched `origin/main`. Create it before the first edit; read-only inspection
does not require another worktree.
Dependent work may use an explicitly coordinated integration base. Each tree
has one active owner. Do not edit another agent's tree or shared uncommitted
files. If the canonical checkout is already dirty or on another branch,
preserve it, report the deviation, and coordinate with its owner before
restoring `main`; never force checkout, reset, or stash others' work to comply.

When related subagent results arrive together, prefer one coherent integrated
PR. One coordinator combines scoped results in an integration worktree, resolves
overlaps, and verifies the combined behavior. Keep unrelated work separately
reviewable. Account for component PRs and trees through the integrated merge;
do not merge the same work twice or leave its landed component trees behind.

Use [the development workflow](docs/runbooks/development-workflow.md) for the
commands and release evidence. Its lifecycle is required:

- Open the scoped PR against remote `main`. Inspect status, run relevant checks,
  and review `git diff --check` before each commit.
- When merging is authorized and the PR is eligible, enable auto-merge while
  retaining required checks and reviews. Arm only one PR at a time. A
  schema-changing PR requires an explicit operator merge decision; see below.
- If the PR is blocked because its branch is out of date, fetch and merge
  `origin/main` into its worktree branch, deliberately resolve conflicts,
  regenerate affected outputs, rerun affected checks, and push. Continue until
  the authorized merge completes or report the concrete remaining blocker.
- For a build-producing merge, wait for accepted publication containing that
  merge before arming the next PR. Verify workflow path filters and artifact
  provenance; a documentation-only merge does not imply a new image exists and
  must not wait for a build that will never run.
- After merge, verify GitHub's merged result and that the branch's final work is
  accounted for (including squash merges). Stop its agents, inspect tracked,
  untracked, and valuable ignored files, and remove the clean, inactive task
  worktree locally. Preserve/report unmerged or dirty work; never force-remove
  it. Delete the landed local branch only after checking for additional commits.
  Fast-forward the clean, idle canonical `main` checkout and report any deferred
  cleanup. Do not leave a merged task's worktree as the normal completion state.

Never use a bare `git stash` or `git stash pop` in the canonical checkout.
The shared stash may belong to another task. Use a detached comparison worktree
or explicitly named paths in your own worktree, and remove temporary comparison
trees once they are clean and inactive.

### Schema implementation, merge, and deployment

Necessary schema implementation is allowed within the authorized task scope.
Inspect the actual schema effect of changes to `control/src/vonk_control/models.py`
and `control/migrations/`; a file edit alone is not proof of a schema change.
Reuse an existing canonical fact when appropriate, without hiding new state in
untyped JSON to avoid a justified schema change.

Keep schema-changing PRs out of auto-merge; obtain an explicit operator decision
for their merge, honoring authorization already given for that action. Describe
the deployment impact in the PR. The fresh-schema contract has no in-place
upgrade path for an incompatible existing database, and a deployed Controller
refuses that mismatch. Merging source does not itself wipe a database.
Resetting a deployed database, losing enrollment/profiles/audit, and re-enrolling
nodes are separate consequential operations requiring explicit authorization.
Do not infer that authorization from permission to implement or merge, or infer
a live production environment from an old incident record.
