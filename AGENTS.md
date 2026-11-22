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
The Controller login is `vonkctl auth login` (admin password at a no-echo
prompt, never stored; token in `~/.config/vonkctl/`): agents never read those
files or handle the password.
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

Package direction is one way: the Controller (`vonk_control`) and `nas_setup`
may import `vonk_agent_protocol`; nothing else imports a sibling package, and
the server never imports vonkctl (`cluster_profiles`). Facts reach the CLI only
through the generated client (`scripts/generate-control-clients`);
`tests/test_package_import_direction.py` enforces it, function-level imports
included.

Every model an `/agent/` route takes or returns (grant requests and replies, the
bounded error envelope, the capability-unavailable reply, the enrollment
observation reply) lives in `vonk_agent_protocol`, never in `vonk_control`. The
wire exporter (`scripts/export-agent-wire-schema`) walks only the protocol and
runs with only the protocol member installed (`scripts/check_environment.py`
`run_in_protocol`, into the workspace's one `.venv`, or `UV_PROJECT_ENVIRONMENT`
when set), so Cargo's `build.rs` and the Rust jobs need no Controller environment. A route model defined outside the protocol fails
`require_wire_exports` in `control/tests/test_api_contract_completeness.py`.

NAS/project setup and administration use Python, including the standalone
`nas_setup` wheel and the existing `vonkctl` CLI. Rust owns the resident Spark
agent, privileged Spark helpers and native Spark installer, where bounded memory
use and predictable performance matter on GPU hosts. Keep one Python NAS setup
path instead of a native NAS build matrix. Preserve typed validation, credential
custody, private files, atomic publication and durable replay when changing an
implementation language; portability is not permission to weaken recovery or
security.

Package lifecycle logic lives in `vonk-agent-helper::package_hooks`; the Debian
maintainer scripts (`packaging/debian/{postinst,prerm,postrm}`) are shims that
exec it, and `scripts/verify-agent-deb` requires them to equal the packaged
shim sources. A new dpkg action is a new `Hook` variant (the `match` is
exhaustive), never new shell. `packaging/systemd` units run binaries, not
`/bin/sh -c`. The `maintainershim` added-line guard enforces both.

## Shared decisions and operator visibility

The Controller owns planning, admission, orchestration, and durable progress.
CLI and web consume the same typed decisions and receipts; do not add a client
planner, independent refusal classifier, or second recovery implementation.
Preview and execution share one decision: bind the reviewed effects and exact
identities, revalidate at acceptance, and reject changed effects instead of
silently broadening consent. The review checksum binds the profile definition
content, not row id or revision, so an unsaved draft preview equals the review
after the same save. Name predicate conditions once and derive both
the decision and its explanation from those same conditions.

Every operation exposes its phase, measured progress, meaningful failure cause,
waiting dependency/owner, next attempt, deadline, and required operator action
where applicable. Preserve safe typed reasons and correlation identity across
helper, agent, Controller, and clients under the
[error policy](docs/error-reporting.md). Do not truncate away the cause or invent
an ETA, percentage, missing value, or success when evidence is unknown.

The Fleet words (vitals, what a Spark serves, the status line, attention items, GPU throttle reasons, host checks) are derived from the same Controller data by the web Fleet page and `vonkctl fleet`. Wording is not tested: no test compares printed or rendered text, and the two clients may word things differently. A secret or terminal control in output, counts, ordering and the `--json` document stay tested. Every vonkctl test decides on
data: the exit status, the requests sent, a typed result or the `--json` document
(the `vonkctl_json` fixture). Decisions are tested as data through view functions.
A renderer decision (empty versus unreadable, which state or command applies)
lives in a small pure function in `cluster_profiles/cli_render` that returns an
enum, a dataclass or a typed value the renderer prints. A secret or terminal
control in any output is a leak, not wording, and a line shown twice is a
duplication fault: absence, count and emptiness checks on captured stdout/stderr
stay valid. Artifact-job size, count and timeout limits live only in `agent_protocol/src/vonk_agent_protocol/job_limits.py`; import them from there (the `job-limits` added-line guard refuses a restated assignment). The `printed-output` added-line guard refuses only a new positive
wording assertion on captured output (`assert "<literal>" in captured.out`) in
the rest of `tests/cluster_profiles`.
In the web the text is the one `NOT_REPORTED_TEXT` in `control/web/src/lib/fleet.ts`; a component never
types the words. Fleet totals (power, tokens, energy, decode speed) are the Controller's fleet line or total of
the metric series, never a sum made in the browser.

Startup schema reconciliation drops every table in the Controller's schema `public`
that `Base.metadata` does not declare (except `alembic_version`), foreign keys
first, no CASCADE. Never keep another tool's table there (LiteLLM has its own
database). A rollback to an older Controller drops the tables a newer one added.

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
Release claims only after reconciling exact effects. Enforce accepted platform
memory floors and account for unified memory as one physical pool.
The recipe's `reserve_bytes` is informational; never add it to `peak_bytes`
or create an additional reservation from it.

Derive bounds from bytes, time, disk, memory, or another protected resource.
Avoid arbitrary structural counts; report the limit and observed value. All
layers must accept values permitted by the canonical plan contract, including
empty or multiline arguments, or reject them at the owning compile/admission
boundary before effects.

## State ownership and resilient recovery

Read the [ownership boundary](docs/architecture-overview.md#state-ownership)
before changing persistence. PostgreSQL remains the authority for identity,
permissions, accepted catalog revisions, saved profiles, desired fleet state,
operation intent, leases, cancellation, and reservations. It coordinates
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

Each periodic background loop runs in exactly one process. Key-free loops
(the automatic catalog sync) run in the worker, which has egress; the API keeps
only the loops that need the gateway master key (default key, route grants) or
its in-memory library view, and never contacts GitHub. See
[Background loops](docs/controller-startup-inventory.md#background-loops).

Per-Spark facts have one owner: agent-reported data uses one canonical typed
Pydantic contract per item in a typed column of `node_inventory_snapshots` or
`node_telemetry_samples`; Controller observations of contact use columns of
`agent_node_contacts`, written only through `agent_contacts.py`. Whether a
Spark holds a model file has one source, the agent's store scan
(`node_inventory_snapshots.model_objects`): Run/Switch and fleet removal read it
through `inventory_repository.spark_model_objects`, install admission from the
inventory snapshot it already loaded. Copy planning and disk admission count an
unknown or stale scan as missing (the copy reuses what the agent already
holds); fleet model removal waits for a fresh scan instead.
A Spark holds a runtime image while an installed rank there names it (the
helper keeps an image whose installation plan is on the host): Run/Switch and
install admission read `disk_reservations.runtime_images_named_on_nodes` (one
query for all target Sparks), and any other image is charged. Whether a rank is
installed is `machine_states.rank_installed`, for a stored state or the
`InstallationNode.state` column; the `rank-installed` added-line guard refuses
an inline `== InstallationNodeState.INSTALLED`. Admission row locks cover only live reservations (`machine_states.reservation_live`; released rows are 24 h history), and the `live-reservation` added-line guard refuses an inline `ResourceReservation.state != RELEASED`. A node's newest inventory report is `inventory_repository.newest_snapshot_ids` (one indexed probe per node); the `newest-snapshot` added-line guard refuses a window or grouped max over `node_id`. The Controller records no image or model-file availability receipts
of its own. Contact,
claim and result recording take no explicit lock on `agent_nodes` and never
update it; admission locks that identity/intent row FOR NO KEY UPDATE, which a
child row's foreign-key KEY SHARE never blocks. The recipe intent check reads the
ordinal without a lock; numbering goes through `workload_intent.py`, the only
writer of `workload_intent_ordinal` (a fenced conditional UPDATE through
`allocate_workload_intent` / `take_workload_intent`), guarded by
`control/tests/intent_ordinal_writers.py`. A one-shot JobRun is never fenced
by a Spark's intent: it belongs to its run (run and RunNode RUNNING, run
generation), and only the run's own Stop cancels it
(`request_run_job_cancellation_in_session`). A job plan may be one rank of a
multi-rank job (protocol 4.5): every rank is its own order on host network;
only the rendezvous owner (`is_endpoint_owner` / `RecipeJobRunRequest.endpoint_owner`)
owns `/inputs`, `/outputs` and the result, and a companion rank reports an empty
output manifest. Job recipes may be `distributed` on the `diffusers` and `vllm-omni` engines only;
run admission reserves the rendezvous port on the owner of a multi-node job. A job submitted to a multi-node run is one order per rank in one
phase (`recipe_start_payloads.bind_compiled_execution_plan`, the service start's
own binder); `lifecycle/job_ranks.py` names the owner's order, the job's
lifecycle is the owner's (`ArtifactJobAdapter.order_of`), and `fan_in` ends the job
with the first failed companion's reason and cancels every live sibling (and
all companions when the owner ends) through the supersession cancellation. A
run's Stop and a cancelled job's cleanup build one exact Stop per issued rank
(`_exact_jobrun_sources`; target key `(node_id, artifact_job_id)`); a rank on a
Spark outside the Stop's scope never holds the others, two orders on one Spark
still hold (nothing is stopped on a guess), and the job and its parent are
confirmed stopped only when every rank's Stop is proven. The
host firewall is told the owner of a job serves no endpoint
port by passing `0` instead of one. Decide an intent fence by kind
(`fencing_intent`), never by a stored ordinal alone. Admission still locks target nodes
through `lock_admission_rows`, and the cleanup Stop a failed start enqueues
does too until lock-plan step 6. Telemetry, inventory and contact writes never
lock `agent_nodes` (`test_agent_node_lock_tree.py`). An artifact-job submit or create never calls a Stop (a run Stop ends the whole run and every job on it): an unobservable previous job answers a bounded retryable wait (`tests/test_submit_never_stops_guard.py`). A recipe revision's source bundle digest is the typed, indexed column
`catalog_document_revisions.source_bundle_sha256`, written by the import and read
by every lookup (build planning, reconcile, collection); the `projected` JSON
carries no copy. Do not add per-feature fact tables, ad hoc JSON, duplicate fact
owners, or authoritative in-memory copies. Operation intent/history, grants,
and desired configuration are distinct from observed Spark facts. Existing
misplaced fields require an owner-approved move; do not extend them. The
per-node fact guard freezes audited outside columns and checks JSON bindings.

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
  Retention decides "is this what the head runs" by the head revision or
  `content_identity.runs_identically` (equal execution keys, source bundles,
  options and Model files), never by content digest: a metadata-only release
  must not uninstall anything (the `head-digests` added-line guard).
  A cached model set serves its first requester and its verified bindings:
  read them through `model_cache.lineage`, or an installation's
  `model_artifact_set_sha256`, never by comparing
  `ModelCacheSet.model_content_sha256` with a Model digest. Likewise "does this
  Spark already hold the model" is the set its installations run
  (`disk_reservations.model_sets_stored_on_node`), never their Model digests
  (both: the `set-first-requester` added-line guard).
  NAS coverage is required only by the copy that reads NAS bytes; planning and
  compilation see model identity only (the `model-sets` added-line guard).
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
A handler that parks or defers work on a broad exception classifies it with
`disposition()` first: a deterministic refusal ends the work with its evidence
instead of repeating on every retry (the `retry-dispositions` added-line guard).

## Verification and completion

The [testing policy](docs/testing-and-ci.md) owns verification commands,
worktree-local environments, OrbStack setup, lane selection, pinned lint/type/
generation checks, and added-line principle guards. Use those instructions from the
active task worktree. In Rust and TypeScript the contract-literal guard reports a
word only when the change adds it to a file more often than it removes it there
(a reflow is not new). On macOS, check the intended OrbStack engine before
calling a container/Linux lane unavailable. Physical NVIDIA, fabric, and model
quality evidence still requires its designated lane.
The pre-commit hook (`scripts/check-staged-code`) also runs the CI added-line
guards on the staged diff, so a violation fails before push; it skips with a
message only when a staged file also has unstaged edits.
`cross-repo-nightly.yml` validates the recipes `main` library with the platform `main` validator every
night; a red run is the drift alarm.

One uv workspace owns every Python package: the root `vonk-cluster-profiles`
(vonkctl) plus `agent_protocol`, `control` and `nas_setup`, with the single
root `uv.lock` and one environment, the root `.venv`. The protocol is an editable
member, so a fresh clone needs no wheel before tests; `scripts/build-control-wheel`
only builds the protocol wheel the Controller image and the release carry, and
every job that builds `control/Dockerfile` runs it first (a workflow test checks
that). Never exact-sync a subset into the shared environment: `uv sync --package X`
or `--project X` for a member other than `control` uninstalls the other members.
Run a subset with `uv sync --inexact --package X` (what `check_environment`
`ensure_protocol` does) or `uv run --package X`, or give it its own
`UV_PROJECT_ENVIRONMENT` through `scripts/ensure-project-venv`. After changing any
member's dependencies or version run `uv lock`; CI runs `uv lock --check` in the
lint job, the only place the lock is compared with the pyprojects.

Every change lands through an integration PR (same-repository `integration/*`
head, or the `release-acceptance` label); `full` in `scripts/select-ci-areas`
owns that rule. A member PR runs only the quick tier (`Ruff` with the added-line guards, web
typecheck/Biome, and Rust fmt/clippy when Rust changed; no full suite) and a
gate that reports `CI gate (member PR)`, never the required `CI gate`, so it cannot merge
itself. Check member work locally (targeted tests, fast guards); the integration
PR proves it. A member PR is done when its review has passed.
A Dependabot PR runs nothing at all (every root job skips its author,
`tests/test_dependabot_updates.py`); fold it into an integration PR.
An integration PR runs the complete suites, every selected integration, its
candidate acceptance with the upgrade carry, and every proof (the proofs are
`ci.yml` jobs behind `ci-gate`, selected by the same areas; no standalone proof
workflow exists, `tests/test_workflow_assertions.py`). Main's Release runs the
complete CI unless it reuses the integration PR's run for the identical tree,
and its own release acceptance (on a signed `v*` tag `ci.yml` also calls
`stable-release.yml`, the stable-channel publication jobs). Adding the label
re-runs `ci.yml` in full at
once. Jobs that build Rust set it up with `.github/actions/setup-rust`, which
reads `rust-toolchain.toml`. The `recipe-library` action caches the built recipe
library by revision and the hash of the build script and `.tool-versions`.
Before the upgrade the carry's checks require only what any release provides;
`test_carry_baseline_phase_accepts_an_older_release_shape` runs them against an
older release's shape (see `docs/testing-and-ci.md`).
See [the contributor CI guide](contributor.md#ci-and-pre-merge-release-acceptance)
for protected environment setup and dispatch by ref. PR acceptance and main's
Release reuse the same candidate/acceptance jobs; only main promotes channels.

For the local repository and Controller pytest suites, use
`scripts/test-local`; it splits Linux-marked tests into the `vonk-ci` OrbStack
VM on macOS and reports unavailable local prerequisites explicitly. CI must
run the required Linux lane and must fail when its prerequisites are absent.

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
checks for the claimed concurrency behavior. New syntax violations fail only on added lines against the PR merge base.
There are no debt counts or category ledgers to update. Keep behavior tests and
fixture tests for the detectors; retain the contract and type registries.

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
package in vonk-forge-recipes. The Controller reads the signed catalog index
only through its `catalog_reader.read_catalog` and uses the contract's entry
types; never parse index entries by hand or copy their types. Controller APIs and Controller/Spark messages
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

A stored catalog document is parsed once per content identity
(`catalog_revision_contract`). `read_catalog_document` returns the caller's own
copy; read-only paths use `read_shared_catalog_document` and never mutate it.
The Controller test session fails when any caller mutated a shared parse.

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

## Catalog words in the Controller web and `vonkctl`

Capability labels and categories and qualification status meanings are the
release's own words: the Controller stores them at sync and serves
`GET /api/library/vocabulary`, and the Library page and `vonkctl model|recipe
library|detail` render them from there. Never type one in a client; a name the
release does not list shows as the name, and a missing value is `Not reported`
in both clients.

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

Compose dependency edges order startup and never gate it (`required: false` on
any condition other than `service_started`). Every service is long-running: NAS
Docker UIs show an exited container as a failed project. The `runtime-init`
helper stages secrets, the runtime kit and volume ownership as root, then idles
healthy. Its readers start once it is healthy and never poll for a staged file;
see [Runtime staging](deploy/compose/README.md#runtime-staging-runtime-init).
`control-api` runs as 10001 without capabilities or Compose secrets and reads
staged copies only; under an older Compose file its pre-exec steps down from root.

Removal is documented in [docs/operations/uninstall.md](docs/operations/uninstall.md),
derived from what the installers create. A change that makes an installer, the
Compose bundle or the agent package create a new file, volume, unit, host entry
or account updates that page in the same PR.

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
The local NAS cache is trusted under its managed-storage contract, and so are
the Spark copies made from it, so do not promise costly repeated full hashing
as part of ordinary profile reads, admission, Spark installs or profile loads.

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

## Whole-fleet profile membership

The current editor saves drafts and a profile load creates an application. The
target contract has one durable selected-profile record in PostgreSQL pointing
to the immutable accepted application snapshot. That snapshot is standing
desired state for the whole enrolled, non-revoked fleet. Do not infer the
selected record from latest application or audit history; unselected profiles
do not drive fleet actions.

The Controller worker must reconcile this snapshot when Spark enrollment or
revocation changes membership; a roster event is not a new operator decision.
An unassigned or newly enrolled Spark is idle. A removed Spark's independent
assignment is ignored without changing the accepted snapshot. If its removal
leaves a multi-Spark model without a required Spark member, stop and confirm
the remaining reachable ranks, withdraw that model's route, report the missing
member, and continue unrelated assignments. Do not claim the removed,
unreachable Spark stopped or count its historical run as live capacity. If the
same Spark rejoins, reconcile its actual state against the accepted snapshot
before scheduling work there.

This continuing policy is separate from review and load: a review binds current
membership and effects, and any membership change before acceptance requires a
fresh review. Report cleanup only when the exact effect is confirmed; removal
or unreachability alone does not prove a Spark stopped. The selected-profile
worker also checks accepted running assignments for live-state drift and uses
the existing run and route recovery paths.

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
  retaining required checks and reviews. GitHub's merge queue is not available
  to this user-owned repository; the `merge_group` path stays dormant and
  supported. Independently reviewed PRs with nonoverlapping changes may
  be armed concurrently. Account for shared source and generated outputs
  before merging overlapping work. A schema-changing PR requires an explicit
  operator merge decision; see below.
- If the PR is blocked because its branch is out of date, fetch and merge
  `origin/main` into its worktree branch, deliberately resolve conflicts,
  regenerate affected outputs, rerun affected checks, and push. Continue until
  the authorized merge completes or report the concrete remaining blocker.
- Accepted publication is required before deployment, not before arming or
  merging the next independent PR. Verify that the accepted generation contains
  the intended changes; verify workflow path filters and artifact provenance.
  A documentation-only merge does not imply a new image exists and must not wait
  for a build that will never run.
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
the deployment impact in the PR. Controller startup reconciles the live schema
to current model metadata transactionally under the startup advisory lock.
Missing tables, safe columns, indexes and constraints are applied. Unknown
columns are preserved (the NOT NULL barrier is lifted); undeclared tables are dropped
(see the schema reconciliation rule above), while obsolete constraints and indexes are
replaced only after a safe replacement is installed. Data conflicts and unsafe
casts are logged and deferred when the application can continue safely.
Merging source does not itself wipe a database. A deliberate database reset
and its data loss are separate consequential operations requiring explicit
authorization; do not infer reset authorization from permission to implement
or merge.

The agent validates a compiled execution plan once, through `executor/plans.rs` `refuse_invalid_plan`, which `executor/dispatch.rs` calls right after `RecipeOperationRequest::parse` and the wire probes call too (`parse_compiled_execution_plan` in the same file is the probe/test document parser). The `plan-validation` added-line guard refuses added `.validate()` calls on a plan (including the split-line shape) in `vonk-agent/src` and `vonk-agent-protocol/src/compiled_oci/` outside that file. `oci/custody.rs` and `oci/observation.rs` still re-validate persisted copies read back from disk; removing those is a known follow-up. `parse` stays structural so journal re-observation of a claim's plan survives upgrades that tighten rules.
