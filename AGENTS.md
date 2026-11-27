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

Package direction is one way: the Controller (`vonk_control`) and `nas_setup` may
import `vonk_agent_protocol`; nothing else imports a sibling package, and the
server never imports vonkctl. Facts reach the CLI only through the generated
client. Imports sit at module level and the packages import without a cycle.
Every model an `/agent/` route takes or returns lives in
`vonk_agent_protocol`. NAS/project setup and administration are Python; the
resident agent, privileged helpers and native Spark installer are Rust. Package
lifecycle logic lives in `vonk-agent-helper::package_hooks`; the Debian
maintainer scripts are shims that exec it. The details, with the guards that
enforce them, are in [Package boundaries and implementation languages](docs/agent-rules.md#package-boundaries-and-implementation-languages).

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


Fleet words are derived from Controller data and never tested as wording: vonkctl
tests decide on data (exit status, requests sent, a typed result or the `--json`
document), and a secret, terminal control, count, ordering or duplicated line in
output stays tested. Artifact-job limits live only in `job_limits.py`. Startup
schema reconciliation drops every table in the Controller's `public` schema that
`Base.metadata` does not declare, so never keep another tool's table there. See
[Fleet words, vonkctl tests and schema reconciliation](docs/agent-rules.md#fleet-words-vonkctl-tests-and-schema-reconciliation).

Model names clients call are one namespace shared by local runs and external
models (`gateway/alias_namespace.py`). Nothing locks: a producer of a name reads
`alias_owner` and writes; a local producer is refused only by an external owner,
and a race is settled by the unique alias and the renderer (a local route wins). An
external entry is admitted only by `validate_external_entry` against the pinned
LiteLLM surface (`deploy/compose/litellm/surface.json`, regenerated with every
LiteLLM bump, see [image pin refresh](docs/image-pin-refresh.md)); a credential is
a binding name, never a value. See
[External models](docs/architecture-overview.md#external-models-and-the-alias-namespace).

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
before changing persistence. The full rules are in
[State ownership and resilient recovery](docs/agent-rules.md#state-ownership-and-resilient-recovery).

- PostgreSQL is the authority for identity, permissions, accepted catalog
  revisions, saved profiles, desired fleet state, operation intent, leases,
  cancellation and reservations. Managed artifact storage is authoritative for
  model files, image archives, verification manifests and download/build
  checkpoints. Never maintain one fact in both SQL and files; a derived index is
  disposable and never a second admission authority.
- Each periodic background loop runs in exactly one process (key-free loops in
  the worker, gateway-key loops in the API). See
  [Background loops](docs/controller-startup-inventory.md#background-loops).
- Per-Spark facts have one owner: agent-reported data is one typed Pydantic
  contract per item in a typed column of `node_inventory_snapshots` or
  `node_telemetry_samples`; contact observations use `agent_node_contacts`,
  written only through `agent_contacts.py`. Do not add per-feature fact tables,
  ad hoc JSON, duplicate fact owners or authoritative in-memory copies. The
  `rank-installed`, `live-reservation`, `newest-snapshot`, `head-digests`,
  `set-first-requester` and `model-sets` added-line guards enforce the shared
  readers named in the full rules.
- Contact, claim and result recording never lock `agent_nodes`; admission locks
  that row FOR NO KEY UPDATE. Intent numbering goes through `workload_intent.py`
  only. A one-shot JobRun belongs to its run and is cancelled only by that run's
  Stop; an artifact-job submit never calls a Stop.
- Move each producer, reader, API projection and meaningful test together, then
  remove the obsolete persistence path. Never add dual writers, fallback readers
  or a schema-1 compatibility path.
- Existing valid artifacts are reused and compatible partial transfers resume. A
  failed attempt or stale status never permanently blocks a new request.
  Recovery is automatic within current authorization, bounded, visible and
  checkpointed; cancellation and newer intent win. Publish complete verified
  artifacts atomically, serialize writers per artifact and fence stale attempts.
  Integrity failures and revoked authority stay explicit blockers. Garbage
  collection coordinates with authoritative references; an unavailable database
  or failed scan does not prove an object unused.
- Test process death, restart, duplicate requests, cancellation, storage loss
  and eventual recovery through the real storage and worker boundaries.

### Deadlock prevention is an architectural requirement

Follow the [coordination boundaries](docs/architecture-overview.md#coordination-and-deadlock-prevention).
Every wait names its dependency, owner, deadline and resume condition. Waiting
work releases execution slots and database transactions; a parent never holds a
slot its child needs. Never acquire or wait for an artifact lock inside a SQL
transaction; SQL transactions follow the documented lock order, have bounded
budgets and never span transfer, build, subprocess, external HTTP or retry
sleep. An expired lease does not prove the old executor stopped: fence old
results and reconcile exact effects first. Scope failures to their owner. Verify
with concurrent PostgreSQL/process tests, not mocked locks or SQLite. A handler
that parks work on a broad exception classifies it with `disposition()` first
(the `retry-dispositions` guard). Full text:
[Deadlock prevention](docs/agent-rules.md#deadlock-prevention-is-an-architectural-requirement).

## Verification and completion

The [testing policy](docs/testing-and-ci.md) owns verification commands,
worktree-local environments, OrbStack setup, lane selection, pinned checks and
the added-line principle guards; use them from the active task worktree. Full
text of this section:
[Verification and completion](docs/agent-rules.md#verification-and-completion).

- The pre-commit hook (`scripts/dev/check-staged-code`) runs the CI added-line
  guards on the staged diff. A plain path literal in a guard module,
  `scripts/ci/select-ci-areas` or a `tools/*.json` allowlist must name a path or a
  path prefix (`tests/test_guard_paths_exist.py`); write guard scopes as plain
  full-path literals and move or delete the literal with the code.
- One uv workspace owns every Python package, with the single root `uv.lock`
  and one environment. Never exact-sync a subset (`uv sync --package X`); use
  `uv sync --inexact --package X`, `uv run --package X` or
  `scripts/dev/ensure-project-venv`. After changing any member's dependencies run
  `uv lock`.
- Every change lands through an integration PR. A member PR runs only the quick
  tier and a gate that cannot merge itself; check member work locally with
  targeted tests and fast guards. A Dependabot PR runs nothing; fold it into an
  integration PR. The integration PR runs the complete suites and proofs, and
  main's Release reuses its run for the identical tree. See
  [the contributor CI guide](contributor.md#ci-and-pre-merge-release-acceptance).
- Before pushing, run `scripts/test changed`: the fast guards plus only the
  tests your diff edits or that import a touched module (`-n 2`). It is THE
  local check; full suites run in batch CI, not on the laptop.
- Use `scripts/dev/test-local` for the Controller pytest suites. CI runs the
  required Linux lane and fails when its prerequisites are absent.
- `scripts/` holds one entry point, `scripts/test`, and groups by purpose:
  `dev/` (environment, local checks, contract generation, local test lanes),
  `ci/` (CI selection, reuse, retry and gates), `images/` (image build,
  acceptance, promotion and verification), `agent/` (agent package build, seal,
  verify and apt), `release/` (installer publication, Compose rendering, supply
  chain, wheels), `fleet/` (fabric validation, lifecycle canary, NAS and Spark
  measurement) and `recipes/` (recipe-library build and validation). Scripts
  that import a sibling module stay in the same group; scripts addressed by
  path from other commits or repos stay at the scripts root
  (`render-accepted-compose-overlay`, `build-recipe-library`,
  `build-control-wheel`); a script's repository
  root is `parents[2]`.
- Every test names a wrong implementation it catches; prefer behavioural
  boundaries and real producer/store/consumer seams. A bug fix ships with a
  regression shown to fail before the fix. See
  [what earns a test](docs/testing-and-ci.md#what-earns-a-test).
- Completion means the authorized user workflow works across producers,
  storage, workers and consumers, including recovery after an injected fault
  clears. Source completion, merge, publication, deployment and physical
  acceptance are distinct claims; record exact revisions and remaining gaps.
- The coordination scanner detects defined syntax patterns only. New violations
  fail only on added lines; there are no debt counts or ledgers to update.
- Recipes CI validates with the signed per-channel recipe-tools bundle
  (`scripts/recipes/build-recipe-tools-bundle`, entry point
  `scripts/recipes/validate-recipe-checkout`). Its `platform-features.json`
  comes only from the tables the platform executes (`scripts/recipes/platform-features`;
  a serving kind, adapter or source provider without a handler fails
  `control/tests/test_platform_features.py`); never type a feature value by hand.
  Recipes CI addresses the bundle's `scripts/recipes/validate-recipe-checkout`,
  `platform-features.json` and `.tool-versions` by path (recipes plans its own
  image builds with `tools/build-plan` and a recipe-owned key; forge ships no
  planner, and recipes CI neither checks out forge nor builds its control wheel): move them only together with the recipes reader. Release acceptance runs the extracted
  bundle against the pinned library, and promotion writes its pointer last; see
  [Recipe-tools bundle](docs/runbooks/platform-release-publication.md#recipe-tools-bundle).
- The public contracts are the attested wheel asset of one recipes release
  (`control/packaging/public-contracts.lock`: `package`, `release`, `wheel`,
  `sha256`; a uv `path` source to the committed wheel). Only
  `scripts/dev/refresh-public-contracts` moves it: it verifies the wheel's
  attestation at ingress and regenerates what the contract feeds; the build and
  `verify-supply-chain` check the committed digest offline. The acceptance pin
  (`tests/acceptance/recipe-library-revision.txt`) is its own input. At every
  forge batch cut the merge captain runs
  `scripts/dev/refresh-public-contracts --latest --acceptance-latest` and commits
  any change as the batch's first commit
  ([supply chain](docs/runbooks/supply-chain.md)).
- `cross-repo-nightly.yml` validates the recipes `main` library with the published
  dev recipe-tools bundle every night (a red run is the drift alarm); `fuzz.yml` fuzzes
  the parsers in `rust/fuzz` (the fix adds one case to the parser's existing test).

## Current contracts only: no legacy compatibility

The dev channel keeps one current contract with no compatibility; transitions
happen by deploy order. From 1.0.0 the stable channel answers the previous
stable vonkctl and agent (N-1 minor) for one minor release: a breaking response
shape ships only in a minor release, consumer first (the vonkctl that tolerates
it is in a published stable release before the Controller change lands, and the
`client-compatibility-report` CI job enforces it), and a deprecated API field or
CLI flag keeps working for one minor and is announced in the release notes. An
added optional response field is not a break: the shipped OpenAPI copy opens
response objects (`open_shown_words`, run by `scripts/dev/generate-control-clients`).
The stable carry proves the N-1 agent. Still keep one current definition and one
current execution path for each document and operation, and remove retired
parsers, DTOs, tables, endpoints, CLI aliases, fixtures and callers together;
keep no dual readers/writers or fallback shapes. Active installer, release-publication, catalog, recovery
and authority paths use schema 2. The current parser and
[operator runbook](docs/runbooks/vonkctl.md) own command vocabulary. The full
rules are in [Current contracts only](docs/agent-rules.md#current-contracts-only-no-legacy-compatibility).

- Published Model and Recipe structures are defined by the canonical Pydantic
  package in vonk-forge-recipes; read the signed index only through
  `catalog_reader.read_catalog`. The recipes future vector
  (`control/tests/fixtures/future-canary-index.json`, G4) is the gate on that
  reader: `tests/acceptance/future_vector_read.py` runs in `control/tests` and
  inside the clean-Spark lane's installed Controller; fix what it exposes in the
  reader, never by special-casing its words. Controller APIs and Controller/Spark messages
  use their authoritative nested Pydantic models, validated on every persisted
  read and write; generate OpenAPI and the Python/TypeScript clients, and
  generate Rust wire structures with typify from the Pydantic JSON Schemas.
- vonkctl ships the admin OpenAPI with the closed response word sets it only
  shows opened, so an older vonkctl reads a newer Controller's new reason or
  wait-owner word. Sets it decides by stay closed: a new `cli_states_generated`
  import outside `cli_render/` of a set a response field holds goes into
  `DECIDED_BY` in `control_client/shown_words.py` (the guard in
  `tests/test_generated_contracts.py` says so). This is forward tolerance, not
  a legacy shim; see [error reporting](docs/error-reporting.md).
- The Rust toolchain version lives only in `rust-toolchain.toml` (plus `rust-version` in the
  root `Cargo.toml`). Workflows run bare `rustup toolchain install`; scripts read the file.
  Agent builds stay on `ubuntu-24.04` (Spark glibc baseline).
- Rust lint levels live only in `[workspace.lints]` of the root `Cargo.toml` and
  `clippy.toml`; a crate inherits them. Silence a lint only with a narrow
  `#[expect(lint, reason = "...")]`, never `#[allow]`. Take an exclusive file lock
  through `HeldLock`, never a raw `flock`.
- Python lint and type configuration lives only in the root `pyproject.toml`
  (`[tool.ruff]`, `[tool.pyright]`). Silence a finding only on the line it is
  reported, with its reason: `# noqa: RULE -- reason` or
  `# pyright: ignore[rule]  # reason`. Never add a `# type: ignore`, a
  file-level `# ruff: noqa`, or a per-file ignore; the per-file entries that
  exist are guard-blocked sites awaiting their class follow-up.
- Python code runs on 3.15 (`.python-version` is the single owner; a guard
  checks the spellings in `.github/`, `install/`, `scripts/`, `src/` and the
  Controller/LiteLLM images against it, allowing 3.12 only at the adapter-venv
  sites). Ruff stays at py314 on purpose: py314 rejects PEP 810 `lazy import` as
  invalid syntax, enforcing the
  no-lazy-imports rule. A file another interpreter runs (a container image's
  Python, a runner's or host's `python3`), and every repository module such a
  file imports, sits under a py313/py312 `per-file-target-version` entry in
  `pyproject.toml` (`deploy/` 3.13; `scripts/`, `tests/nodes/`,
  `tests/acceptance/` and `tools/generated_contracts.py` 3.12, the ubuntu-24.04
  runner's python3; `agent_protocol/` 3.12, the recipe adapter images' system
  python); it imports the 3.15 packages only when uv runs it.
- Every application route appears in the full OpenAPI schema; never use
  `include_in_schema=False` in production routes.
- Consume wire and persisted JSON with the canonical model's JSON validation
  semantics. Optional fields with a `None` default accept omission and `null`
  alike; normalise through the authoritative model before hashing or signing;
  never strip every `null` or default away malformed required data. See
  `docs/api-contracts.md` for the serialization policy.
- Test fixtures and health probes use the same typed contract as the producer.
  Allow engine-owned content through its declared extension fields; no
  exhaustive engine-argument allowlist.
- Update producers, consumers, documentation and meaningful tests together. A
  stored catalog document is parsed once per content identity
  (`catalog.revision_contract`); read-only paths use
  `read_shared_catalog_document` and never mutate it.

## Recipe-library checks

A recipe image is identified by its manifest digest: the Controller uses the
signed catalog's pin for a revision's content digest and never derives or reads
its `build_key` (the `prebuilt-key` guard), and the helper authorizes by the
signed digest, never by an image label or the image's `USER`. See
[Profile cache contract](docs/agent-rules.md#profile-cache-contract).

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
in both clients. A capability is served as its name (`LibraryModelDisplay`, the
open `CapabilityName` in `schemas/vonkctl`), so the sync keeps one a newer
contract minor added and a new capability needs no Controller or vonkctl
release; an executed word it cannot keep withholds only that entry, with a
note, and pauses retraction so its older revision stays offered.

## Deployment and fleet safety

Routine Spark package upgrades are Controller-authorized and signed; do not use
SSH as the rollout path. `vonkctl` exposes one authorized upgrade command; there
is no separate candidate/preview/apply or plan-digest subcommand:

```bash
vonkctl fleet upgrade Atlas --yes --json
vonkctl fleet upgrade --all --yes --json
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
No NAS service mounts the Docker socket; daemon-level recovery (old images,
lost networks) is an operator step, see
[NAS Docker daemon](deploy/compose/README.md#nas-docker-daemon-old-images-and-lost-networks).

Removal is documented in [docs/operations/uninstall.md](docs/operations/uninstall.md),
derived from what the installers create. A change that makes an installer, the
Compose bundle or the agent package create a new file, volume, unit, host entry
or account updates that page in the same PR.

## Profile cache contract and whole-fleet membership

The local NAS/Controller cache is the authoritative availability surface for
Fleet profile loads: choices resolve to an exact model and recipe-image
identity, and a choice need not be cached to be saved. A load prepares what it
needs in that cache by itself (the review lists the model download and the
image build, each skipped when cached), names an unfinished preparation as
what it waits on, and fails with the reason of an exhausted one; never leave a missing asset for a target Spark
to discover. The managed cache is trusted, so do not promise repeated full hashing. Applying a
ready profile binds the exact profile and plan digests, prepares assets on the
targets in parallel, skips what is local, replaces workloads safely and reports
durable per-target progress before a route is published. Spark-local copies are
execution caches only.

One durable selected-profile record in PostgreSQL points to the immutable
accepted application snapshot, which is standing desired state for the whole
enrolled, non-revoked fleet; the worker reconciles it when enrollment or
revocation changes membership, without a new operator decision. Never infer the
selected record from application or audit history. Report cleanup only when the
exact effect is confirmed. Keep readiness evidence separate from CI,
deployment and physical Spark acceptance, and never make SSH an alternative
rollout path. Full text: [Profile cache contract](docs/agent-rules.md#profile-cache-contract) and
[Whole-fleet profile membership](docs/agent-rules.md#whole-fleet-profile-membership).

## Checkout, pull requests, and worktree lifecycle

`/opt/vonk-forge` is the canonical checkout of local `main`; keep it clean and
fast-forward it only while idle. All repository edits use an isolated branch and
worktree based on freshly fetched `origin/main`, created before the first edit;
each tree has one active owner, and you never edit another agent's tree or force
checkout, reset or stash others' work. Never use a bare `git stash` in the
canonical checkout. Prefer one coherent integrated PR when related results
arrive together. Follow [the development workflow](docs/runbooks/development-workflow.md);
its lifecycle is required: open the scoped PR, run relevant checks and
`git diff --check`, enable auto-merge only when merging is authorized and the PR
is eligible (GitHub's merge queue is not available here), merge `origin/main`
into an out-of-date branch and resolve conflicts deliberately, require accepted
publication before deployment (not before merging the next independent PR),
and after merge verify the result, then remove the clean, inactive task worktree
and landed branch. The full lifecycle is in
[Checkout, pull requests, and worktree lifecycle](docs/agent-rules.md#checkout-pull-requests-and-worktree-lifecycle).

### Schema implementation, merge, and deployment

Necessary schema implementation is allowed within the task scope; inspect the
actual schema effect of changes to `control/src/vonk_control/models/` and
`control/migrations/`. Keep schema-changing PRs out of auto-merge and obtain an
explicit operator decision; describe the deployment impact in the PR. Startup
reconciles the live schema to the models transactionally (missing objects added,
unknown columns preserved, undeclared tables kept for one minor release on stable, then dropped; dropped at once on dev). Merging source does not
wipe a database, and a deliberate reset needs its own explicit authorization.
See [Schema implementation, merge, and deployment](docs/agent-rules.md#schema-implementation-merge-and-deployment).

The agent validates a compiled execution plan once, through `executor/plans.rs`
`refuse_invalid_plan`; the `plan-validation` guard refuses other `.validate()`
calls on a plan. The exact scope is in
[docs/agent-rules.md](docs/agent-rules.md#the-agent-validates-a-compiled-execution-plan-once).
