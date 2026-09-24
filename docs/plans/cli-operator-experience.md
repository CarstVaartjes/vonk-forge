# CLI operator experience: design and delivery plan

Status: accepted design baseline from 2026-09-22. Implementation is underway;
see the [current package status](cli-operator-status.md) and
[operator runbook](../runbooks/vonkctl.md) for implemented behavior and commands.

The source inventory, “Proposed” labels and blocked-command descriptions below
record the design baseline, not current command availability. They preserve the
reasoning behind the work packages; use the detailed implementation plan and
its status record to determine what remains. This document is not a second
operator command reference.

Reviewed 2026-09-22 against repository commit
`49389f3dced50d2ecf7680e256dbe1827817eaee` and the primary interface references
linked below. This is a plan for `vonkctl` and the Controller contracts it
needs. It does not authorize deployment or physical fleet changes.

The [detailed implementation plan](cli-operator-implementation.md) expands this
design into ordered work packages, exact code and contract boundaries,
regression scenarios, validation commands, and completion gates.

## 1. Outcome and scope

An operator should be able to connect, inspect Sparks, select a model and
recipe, prepare the Controller cache, compose a whole-fleet profile, review
its exact effects, apply it, follow or reconnect to its progress, use its
published endpoint, and recover or clean up through the CLI.

The completion bar is a reliable complete workflow, with understandable
failure and recovery, rather than an indefinitely polished command surface.
The CLI is the first acceptance client for shared Controller behavior. The
later web interface consumes the same authority, decisions, receipts, and
progress; it must not need a second orchestration implementation.

In scope:

- Existing Fleet, Model, Recipe, Profile, and signed CLI update workflows.
- Terminal presentation, help, selectors, automation, and shell completion.
- Exact operation observation, safe cancellation where supported, endpoint
  discovery, and the artifact-job workflows already exposed by the web/API.
- Bounded changes to canonical API contracts needed to make these workflows
  truthful and independently usable.
- Connected client/API tests, terminal usability checks, and named later
  Controller and Spark acceptance gates.

Outside this plan: a web redesign, a full-screen terminal application, a new
scheduler, direct SSH administration, a new persistence architecture, public
catalog work, and changes to recipe qualification policy. Generated web
clients and the minimum connected call-site changes must accompany a shared
contract change; deferring web design is not permission to break its current
consumer. The user subsequently authorized necessary fresh-schema changes and
confirmed that there is no active production environment; see the implementation
plan and its status record.

The [engineering principles](../engineering-principles.md),
[state ownership](../architecture-overview.md#state-ownership),
[coordination boundaries](../architecture-overview.md#coordination-and-deadlock-prevention),
[API contracts](../api-contracts.md), and
[error policy](../error-reporting.md) remain authoritative.

## 2. Source inventory at the design baseline

These observations refer to the reviewed commit above, not the current checkout
or deployed Controller.

| Area | Current evidence | Design consequence |
| --- | --- | --- |
| Vocabulary | `controller_cli.py` registers four singular domains; `cli.py` also exposes signed CLI `update`. | Extend this surface; do not introduce a competing CLI or restore retired roots. |
| Configuration | HTTPS origin and private token-file environment variables; offline `--help` and `--version`. No connection config file. | Keep one connection mechanism for this scope; improve diagnosis and setup instructions. |
| Defaults | No command reads the selected profile; profile number defaults to 1. | Proposed: no command shows offline orientation; mutations require an explicit profile number. |
| Browsing | Fleet filters; model/recipe facets and pagination; exact selectors; narrow and wide human tables; JSON. | Improve decision-oriented columns and pagination without inventing new state. |
| Profile authoring | `name`, `add`, `remove` autosave with revision checks; `add` always requests running state. | Expose installed-only assignments and complete supported authoring fields. Keep incomplete drafts distinct from admissible plans. |
| Review and load | `profile load --dry-run` obtains a preview. `FleetProfileLoadRequest` carries only `dry_run` and `request_key`; the actual load calculates a fresh preview internally. | A previously viewed plan cannot currently be supplied as a load precondition. Add an authoritative reviewed-plan precondition before claiming exact review/apply behavior. |
| Following work | Mutations follow durable identities and recover certain lost responses. Separate `profile progress --follow` polls the latest application route. | Preserve existing recovery; pin reconnect/follow to one application so a later load cannot replace the work being observed. |
| Output | Human rendering often falls back to flattened, truncated nested objects. Watch clears the screen when stdout is a terminal. | Add typed summaries and incremental progress; preserve scrollback and distinguish unavailable from empty. |
| Errors and exit codes | Errors currently use the same stdout emitter as results. Existing documented statuses are 0 success, 1 partial, 2 failure/timeout, 130 interrupt. | Adopt explicit stream rules; preserve the numeric exit contract and make its contextual meaning precise. |
| Endpoint and jobs | Endpoint lookup and artifact-job routes exist, but the four-noun parser does not expose their full workflows. | Add focused client commands using those contracts, with route gaps stated explicitly. |
| Documentation | The current `runbooks/vonkctl.md` describes single-step commands; `docs/README.md` still broadly says mutations require `--apply`. The supplied agent guide also names `fleet node-profile`, absent from this parser. | Reconcile command documentation against the current owner before implementation. Do not silently add aliases or assume node configuration is a workload profile. |

Source map:

- [Parser and process behavior](../../src/cluster_profiles/cli.py)
- [Commands, requests, and polling](../../src/cluster_profiles/controller_cli.py)
- [Terminal rendering](../../src/cluster_profiles/cli_render.py)
- [Selector handling](../../src/cluster_profiles/cli_select.py)
- [Validated transport](../../src/cluster_profiles/control_client.py)
- [Profile contracts](../../control/src/vonk_control/fleet_profile_contract.py)
- [Profile routes](../../control/src/vonk_control/fleet_profile_api.py)
- [Profile execution](../../control/src/vonk_control/fleet_profiles.py)
- [Current operator runbook](../runbooks/vonkctl.md)
- [Full OpenAPI contract](../../control/openapi.json)

## 3. Comparison with established interfaces

This is a pattern comparison, not a claim that every convention is universal
or that Vonk has undergone a user study. Each adoption below is a Vonk design
decision; the references establish the precedent.

| Reference and observed practice | Adopt for Vonk | Deliberate boundary |
| --- | --- | --- |
| [Command Line Interface Guidelines](https://clig.dev/): useful help, composable streams, noninteractive behavior, clear errors, terminal-aware presentation. | Offline help with task examples; results separated from diagnostics; no prompts in automation; concise recovery instructions. | No new interactive wizard or pager dependency is needed for the first release. |
| [GitHub CLI formatting](https://cli.github.com/manual/gh_help_formatting): distinct human and JSON output. | Readable tables and lossless structured results. | Do not embed another query/template language; ordinary `jq` pipelines suffice. |
| [GitHub CLI environment](https://cli.github.com/manual/gh_help_environment): control over prompts, color, and terminal behavior. | Explicit noninteractive mode and predictable output under redirection. | Keep the existing private token-file boundary, rather than adopting token-in-argument examples. |
| [GitHub CLI run watch](https://cli.github.com/manual/gh_run_watch): follow a named run with relevant steps and an outcome. | Reconnect to a stable operation, show the current/failed steps, and reflect failure in the exit status. | Vonk follows return a nonzero status for failure without requiring an extra outcome flag. |
| [GitHub CLI exit codes](https://cli.github.com/manual/gh_help_exit-codes): documented meanings, including command-specific differences. | Publish one explicit Vonk table and verify it in subprocess tests. | Preserve Vonk's existing numbers; copying GitHub's numbers would create unnecessary churn. |
| [Terraform plan](https://developer.hashicorp.com/terraform/cli/commands/plan) and [plan/apply workflow](https://developer.hashicorp.com/terraform/cli/run): inspect current versus desired effects before execution. | Bind profile submission to the exact reviewed Controller plan and reject stale intent. | No local Terraform-style state, opaque plan files, independent planner, or extra platform-upgrade preview command. |
| [kubectl wait](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_wait/): wait for an explicit condition with a deadline. | Distinguish accepted work, execution completion, and endpoint readiness; expose bounded observation. | No Kubernetes infrastructure or generic JSONPath condition language. |
| [Docker attach](https://docs.docker.com/reference/cli/docker/container/attach/): attaching, detaching, and forwarding signals are distinct behaviors. | Make observation and remote execution separate concepts. | Unlike signal-forwarding attach, Ctrl-C in a Vonk observer only stops observation; remote cancellation is explicit. |
| [Tailscale CLI](https://tailscale.com/docs/reference/tailscale-cli): readable device status and detailed JSON. | Named Sparks, connection freshness, and progressively disclosed diagnostics. | Network reachability alone does not establish workload readiness or authority. |
| [GitHub CLI completion](https://cli.github.com/manual/gh_completion): shell-specific completion generation. | Generate Bash and Zsh completion from the parser; include only installed commands. | No background remote calls or shell configuration edits during completion generation. |

The central conclusion is to keep resource-oriented commands, truthful status,
explicit consequential intent, and a stable automation contract. Visual
decoration and a larger command vocabulary do not resolve the actual gaps.

## 4. Interaction contract

### 4.1 Vocabulary, identity, and discoverability

Keep `fleet`, `model`, `recipe`, and `profile` as the four operator domains.
Keep `update` as the existing client maintenance command. `completion` is a
proposed ancillary command, not another operational domain.

Use existing verbs (`detail`, `download`, `load`, `progress`, `remove`) rather
than adding synonyms. Explain `profile load` as “apply this whole-fleet
profile.” Do not rename it to `apply` while retaining both paths.

Lists lead with friendly names and show exact reusable selectors. Fuzzy
matching may help search but never chooses a mutation target. Ambiguous
names fail with canonical candidates. Logical recipe selectors choose the
accepted compatible revision at preview; a displayed digest identifies the
immutable execution choice. Selection and explanation must use the same
Controller predicates, not independent client rules.

Help names the command's scope, side effects, default follow behavior, exit
semantics, and two realistic examples. `vonkctl` without arguments should
show offline orientation and the four domains. `--help`, `--version`, and
completion generation require neither credentials nor network access.

Retain `--profile N` for invocation-scoped selection. Read-only inspection
may default to Profile 1, visibly identifying it. Saving, loading, importing,
or cancelling profile work requires an explicit positive `--profile N`.
There is no hidden persistent selected profile.

### 4.2 Terminal and automation output

| Mode | stdout | stderr | Interaction |
| --- | --- | --- | --- |
| Human, one-shot | Result table or task-specific detail | Warnings and errors | Prompts only for designated consequential actions in a real interactive terminal |
| Human, following | Final result | Append-only changes in phase, progress, waiting, or recovery | Ctrl-C stops observing; no whole-screen clear |
| `--json` | Exactly one complete success/result object, or one structured failure object if no result is available | Empty by default; no duplicate prose errors | Never prompts or adds cursor controls |
| Redirected human output | Plain readable result | Bounded plain progress and diagnostics | No prompt; explicit intent flags required |

Keeping JSON errors on stdout is an intentional continuation of Vonk's
documented one-object contract. Human errors move to stderr. Distinguish a
valid observation of failed remote work from a failure to obtain an
observation. Preserve the validated API response for normal JSON results;
put client observation metadata in one documented typed wrapper only where
needed, rather than casually injecting competing `state` fields.

`--json --follow` returns the final or deadline observation as one object;
it does not silently become JSON Lines. Event streaming is deferred unless
a concrete consumer needs it. Do not add a second output schema speculatively.

Human detail follows this order: identity and scope; actual state and
freshness; current versus desired work; blockers; one next action; technical
evidence on request. Avoid raw nested JSON as normal human presentation.
Keep selectors copyable and complete. Show bytes in IEC units and timestamps
with timezone; retain exact integers and original timestamp strings in JSON.

Presentation must fit 60-, 80-, and 120-column terminals, with a stacked form
when columns would hide meaning. Support Unicode names and an ASCII-safe
fallback. Escape untrusted terminal control characters in names, error text,
and logs. State never relies on color. If color is introduced, respect
`NO_COLOR`, `TERM=dumb`, redirection, and `--no-color`.

Add `--no-input`; it does not grant consent. `--yes` acknowledges the displayed
scope of supported destructive/consequential commands, never bypasses
authorization, blockers, revision checks, or plan preconditions. JSON mode
and redirected input are noninteractive regardless of this flag.

### 4.3 Exit semantics

Preserve the existing small numeric vocabulary rather than importing another
tool's codes. Document what success means for each class of command.

| Exit | Meaning |
| --- | --- |
| 0 | Requested read completed; admissible dry-run completed; followed operation succeeded; or explicit detached submission was durably accepted. |
| 1 | Followed operation reached the existing partial outcome. Show completed and unresolved effects. |
| 2 | Usage/configuration/transport/authority/contract error; blocked dry-run; failed/rejected/cancelled operation when following; or observation deadline reached without the required outcome. |
| 130 | User interrupted this CLI process; accepted Controller work was not cancelled. |
| 141 | Unix output consumer closed the pipe; stop local output without cancelling accepted work. |

Inspection returning an operation whose state is failed is still a successful
read (0); `progress --follow` used to await that operation fails (2). Implement
that distinction explicitly rather than deriving every exit from an arbitrary
`state` in any returned document. Likewise `--detach` means accepted, not
completed. A blocked preview must not exit 0 merely because its HTTP request
succeeded. Unknown or malformed operation states cannot count as completion.

Broken pipes terminate cleanly without a traceback or a second write into the
closed pipe. Closing an output consumer must not cancel accepted remote work.
The proposed 141 result follows the Unix SIGPIPE convention; verify it through
the packaged executable and keep it distinct from a remote failure.

### 4.4 Security and ownership

Keep credentials in a private regular file and keep HTTPS validation enabled.
Diagnostics never suggest bypassing TLS or changing permissions broadly.
Maintain the existing installation/token-issuance trust path; this plan adds
connection validation, not another credential authority. The current browser
token download remains an explicit bootstrap dependency until a separately
reviewed non-browser issuance flow exists.

Authorization, placement, retries, cancellation, and readiness belong to the
Controller. The CLI may retry permitted observations; it cannot decide to
restart remote work because a timer expired. Same request key means the same
intent; a new explicit request has a new key. Display/recover the key before
or during an ambiguous submission; never invent a server correlation ID.

There is no local work queue or independent state cache. Private export files
are snapshots, not admission authority. Derive readiness through the existing
managed-storage cutover; do not add SQL/file dual writers. Preserve canonical
JSON null/omission semantics and required nullable fields.

## 5. Operator journey and proposed commands

**Legend:** Existing commands are present in the reviewed parser. “Proposed”
means target syntax and behavior that must not be documented as available
until its implementation and owning API are shipped. Uppercase names below
are placeholders. Examples involving mutations are illustrative, not executed.

### Step A — Connect and orient

Existing: `vonkctl --help`, `vonkctl --version`, `vonkctl fleet`.

Proposed: `vonkctl --check-connection [--json]` validates local configuration,
credential-file safety, TLS connectivity, and one authorized read. It reports
the sanitized Controller identity, client version, and which check failed.
It neither displays token contents nor changes configuration. Keep explicit
environment-variable setup; do not add a multi-context configuration system
for the single-Controller target.

Acceptance: a new operator can follow the documented credential path and
reach Fleet; missing files, unsafe file modes, expired credentials, refused
permissions, DNS, and TLS failures each produce the correct bounded diagnosis.
Offline help never initiates an update check.

Enrollment and re-enrollment also need a safe handoff for their one-time grant.
Proposed `fleet enroll NAME --output FILE` and `fleet re-enroll SPARK --output
FILE` create an exclusive private file and return only nonsecret receipt data
in ordinary output/JSON. Validate the destination before requesting issuance;
on a write failure, report the grant's status through its owner rather than
silently issuing another. Routine diagnostic redaction must not destroy the
deliberate grant delivery, and enrollment credentials must not enter copied
commands or logs. Test expiry and single-use consumption through the actual
enrollment authority.

### Step B — Understand the fleet

Existing examples:

```text
vonkctl fleet
vonkctl fleet --health stale --health offline --warnings-only
vonkctl fleet detail Atlas --metrics all --range 24h
vonkctl fleet loginfo Atlas --since 15m --lines 100 --follow
```

The default view shows Spark name, authenticated health/freshness, actual
workload name, readiness, usable memory, and attention reason. Detail includes
installed inventory, named atomic placements, desired/live drift, current
operation, and published endpoint where available. Count one distributed
placement once in fleet totals; show its constituent Sparks underneath.
Unsupported, absent, stale, and zero measurements remain distinct.

Acceptance: an operator can identify the unhealthy Spark, affected workload,
and safe next diagnostic from the default view. A stale observation never
looks like a fresh zero or a healthy workload.

### Step C — Discover and compare models and recipes

Existing examples:

```text
vonkctl model library --usage code --sort updated
vonkctl model detail MODEL
vonkctl recipe library --model MODEL --all-models
vonkctl recipe detail RECIPE --technical
```

Keep local availability separate from published catalog membership. Model
rows show capability, quantization, bytes, Controller cache status, and exact
recipe choices. Recipe detail shows required topology, startup/steady memory,
image readiness, evidence level, running placements, and precise blockers.
“Structurally qualified” never becomes “tested on your Sparks.”

Proposed filters `--ready` and `--fits-fleet` belong to recipe listing and
must use Controller-authored assessments. Ready means complete exact NAS
assets plus admissible current placement; fit alone means resource/topology
compatibility under stated observation freshness. Return unavailable when the
Controller lacks evidence rather than silently excluding all results.

Retain `--limit` and opaque `--cursor`; every partial list reports its scope
and continuation. Do not treat the first page as the full catalog. Detect a
repeated pagination cursor and bound complete scans by an explicit request
deadline. Empty results include a concrete filter-reset suggestion.

Acceptance: a candidate can be found by task, its fit and cache readiness
understood, and its exact identity reused without copying a truncated cell.

### Step D — Prepare the Controller cache

Existing examples:

```text
vonkctl recipe download RECIPE
vonkctl model download MODEL --detach
vonkctl recipe download RECIPE --request-key REQUEST_UUID
vonkctl recipe update
```

One recipe download coordinates its exact required assets. Show reuse,
resuming, download/build/verification/publication, measured bytes, and what
remains. Do not turn build step counts into a byte percentage or invent an
ETA. Report disk shortfall with required, available, and missing bytes.
Distinguish active work, a fresh refresh request, and a replay of the same key.

Proposed reconnect commands:
`vonkctl model progress OPERATION_ID [--follow]` and
`vonkctl recipe progress OPERATION_ID [--follow]`.
These consume the existing noun-specific operation routes. Where response
loss precedes receipt of an ID, preserve same-key reconciliation; any missing
request lookup is an explicit API prerequisite, never permission to generate
a replacement identity automatically.

Acceptance: a failed refresh preserves the last verified copy, an interrupted
transfer resumes compatible partial work, and cache-ready describes actual
managed artifacts. Download completion alone is not a serving endpoint.

### Step E — Compose and save the desired fleet

Existing examples:

```text
vonkctl profile list
vonkctl --profile 2 profile name "Coding"
vonkctl --profile 2 profile add RECIPE --spark Atlas --spark Boreal --as coding
vonkctl --profile 2 profile remove coding
```

Proposed additions:

- `profile add ... --state installed|running` exposes the canonical desired
  state rather than always requesting a running workload.
- `profile configure --description TEXT --retention keep-cached|exact
  --favorite true|false --label KEY=VALUE` edits supported profile metadata;
  omitted flags preserve existing values. Add `--remove-label KEY` for explicit
  removal and reject conflicting label edits.
- `profile export [--output FILE]` and `profile import --file FILE|-` exchange
  the canonical authoring document. Import saves desired state only, never
  loads it. Export defaults to stdout; file output uses exclusive/private
  creation. Import requires the live revision precondition for replacement.

Use typed current contracts throughout read/modify/write. Preserve description,
retention, favorite, labels, assignment names, model choice, and desired state
when changing just one field. Reject a conflicting revision and show how to
reread; do not automatically replay a stale whole-profile write.

Each save reports profile number, new revision, changed assignments, and
“Saved; running fleet unchanged.” A profile covers the whole fleet: show
unassigned Sparks as idle. Incomplete topology can remain a clearly marked
draft, but readiness and load admission still require exact compatible cached
assets and a complete placement. Do not infer desired state from observed state.

Acceptance: an operator can build an installed-only or running assignment,
save an incomplete draft, and see why it cannot load. Editing a name does not
reset other authoring fields or change a running workload.

### Step F — Review and apply exact changes

Keep the existing `profile load` command and its `--dry-run` option. Proposed
additional flags are `--expected-plan DIGEST`, `--yes`, and `--no-input`.

```text
vonkctl --profile 2 profile load --dry-run
vonkctl --profile 2 profile load
vonkctl --profile 2 profile load --expected-plan PLAN_DIGEST --yes --detach
```

Interactive load fetches a server preview, presents its effects, asks once,
and submits that exact plan identity. A script first obtains a valid dry-run
result and then supplies `--expected-plan` plus `--yes`; it does not approve a
silently regenerated plan. Blocked review displays all actionable blockers
and performs no mutation.

The review must include profile revision and whole-fleet scope; unchanged,
stop, start, install, and remove effects; idle Sparks; atomic multi-Spark
membership; exact resolved model/image identities; missing cache assets;
reusable target assets; resource headroom; endpoint changes; and disruptive
effects. Readiness and expected interruption are distinct from an invented
duration estimate.

**Controller prerequisite:** extend the current schema-2 load request with
the reviewed-plan precondition and validate it under the admission boundary.
Bind reviewed profile, target membership, recipe/model/image identities, and
relevant resource facts. Recheck safety at admission; reject changed decisions
instead of silently substituting a newer recipe/image. Exclude incidental
timestamps from the decision identity. Same-key replay reconciles already
accepted intent before considering a fresh preview. Updating connected
producers, generated clients, and consumers is one change; no fallback load
path may bypass the precondition.

Ordinary profile loading resolves a current compatible cached recipe; the
saved profile itself is not converted into a permanently pinned execution
plan. The pin belongs to the reviewed application.

Acceptance: a profile edit, changed fleet membership, asset loss, or changed
resolved revision between review and submit produces a refusal or a new
review. No workload is stopped under a plan the operator did not approve.

### Step G — Follow, leave, and reconnect

Keep `--detach`, `--timeout-seconds`, `--interval-seconds`, `--watch`, and
`--follow`, with their existing scopes. A watch observes a resource; a follow
awaits one durable operation. Observation deadlines never alter execution
deadlines. Preserve bounded defaults initially; explain the limit in help
and at timeout, and print a copyable reconnect command.

Proposed profile reconnect forms:

```text
vonkctl --profile 2 profile progress --application APPLICATION_UUID --follow
vonkctl --profile 2 profile progress --request-key REQUEST_UUID --follow
```

The two selectors are mutually exclusive. Without either, resolve the latest
application once, display its identity, then pin any follow to that application.
A newer application is reported as newer intent, not silently substituted.
Apply the same stable identity rule to model/recipe progress, artifact jobs,
and proposed `fleet progress OPERATION_ID --follow` for supported fleet jobs.

Progress leads with phase and outcome, then per-target steps. Waiting includes
dependency, owner when known, reason, next attempt, persisted deadline, and
resume condition. Reconnecting shows the last confirmed observation and its
age. Do not reuse “failed” to mean “the observer lost its connection.”

Ctrl-C, terminal closure, and observation timeout stop only the observer.
The CLI prints the durable identity when it can still write safely. After
process death, discovery through resource status and activity must recover the
identity without relying on a local receipt file.

Acceptance: two consecutive loads cannot cause a follower to switch from the
first to the second; reopening a shell finds the same work and its exact state.

### Step H — Use the result

Proposed: `vonkctl --profile 2 profile endpoint [ALIAS] [--json]`.
Without an alias, list the profile's published assignments. With an alias,
show the authenticated published endpoint, client-facing model name, freshness,
generation, and a credential-free connection example. Resolve aliases from
the Controller; never construct a routable URL from a Spark IP or recipe name.
An endpoint absent, expired, or awaiting publication remains unavailable.

For non-chat recipes, expose the existing artifact-job workflow under
`recipe job`: `list --run RUN_ID`, `create --run RUN_ID --file INPUT_JSON`,
`submit JOB_ID`, `detail JOB_ID [--follow]`, `cancel JOB_ID --yes`, and
`download JOB_ID --output DIRECTORY`. These forms are proposed. The create
flow validates capability-specific input, streams verified local files through
the existing upload/finalize routes, and returns a durable draft job identity.
Submit starts execution explicitly; result downloads verify hashes and publish
atomically. Never assume every recipe exposes a chat-completions endpoint.
The implementation plan also specifies `recipe job upload JOB_ID --file
INPUT_JSON` to resume delivery to an existing draft after an interrupted upload.

Acceptance: a successful serving workload yields a usable endpoint; an artifact
workload yields retrievable verified outputs. Neither command claims model
quality or physical acceptance merely from a completed control operation.

### Step I — Diagnose, recover, and cancel

Existing `fleet loginfo` remains the bounded Controller-collected diagnostic
path. Proposed `fleet activity` combines authorized operation/job/audit
references into a paginated read-only view. Filters and links must retain exact
target and request identities; a single malformed historical record must not
hide unrelated current work.

Use the existing structured error context: operation, safe endpoint path,
received HTTP status, canonical code, correlation ID when available, source,
and retry/defer/exit decision. Lead human output with the affected task and the
next safe action. Report limits with observed values and units. A denied
operation is never presented as a temporary network fault.

Automatic recovery belongs to current Controller intent. A new explicit
download or load is new intent; retrying a lost submission uses the original
key. A generic “retry” button/command must not revive cancelled or superseded
work. If an existing job exposes a authorized resume action, propose
`fleet resume JOB_ID --yes` only for that advertised resumable state.

Proposed cache/profile cancellation forms are `model cancel OPERATION_ID
--yes`, `recipe cancel OPERATION_ID --yes`, and
`--profile N profile cancel APPLICATION_ID --yes`. **These are blocked on
canonical cancellation routes and semantics; the inspected API does not expose
them.** Cancellation must preserve reusable assets, fence stale workers,
reconcile effects already issued, and report cancelling versus cancelled.
Removing cache is not a substitute for cancellation. Artifact-job cancellation
already has a distinct API and should retain its own authorization boundary.

Acceptance: refusal, temporary wait, integrity failure, cancelled intent, and
unknown outcome are distinguishable; each has an accurate next action.

### Step J — Cleanup and maintenance

Keep existing model/recipe removal commands and require explicit dependency
intent (`--keep-model` or `--with-model`) where currently required. Show exactly
which NAS cache assets are affected and whether saved profiles lose readiness.
An explicit authorized cache eviction may differ from automatic garbage
collection; preserve the current owner-specific rules, never infer permission
to stop workloads or erase authority from a remove-cache request.

Routine upgrades remain:

```text
vonkctl fleet upgrade Atlas --strategy one-at-a-time
vonkctl fleet upgrade --all --strategy one-at-a-time
vonkctl update
vonkctl update --apply
```

The last two operate on the CLI package, not the Controller or Sparks. Keep
signed publication verification. Fleet upgrades use the existing single
Controller-authorized command: no candidate/preview/apply/plan-digest upgrade
subcommands and no SSH fallback. The parser currently accepts `all-at-once`;
the delivery audit must remove that option and its active consumers for
consequential fleet updates to meet the standing one-Spark-at-a-time rule.

Acceptance: removal scopes, fleet upgrades, CLI updates, and Controller-host
deployment are unmistakably different. Cache cleanup retains the authoritative
references required by its policy; a failed reference scan cannot authorize it.

## 6. Illustrative terminal output

These are proposed layouts with invented demonstration data. They are not
observations of Atlas, Boreal, an installed model, or measured performance.

```text
Fleet — 2 Sparks — observed 2s ago

SPARK    HEALTH   WORKLOAD                 READY   MEMORY FREE
Atlas    Live     Coding · Atlas + Boreal  Yes     34 GiB
Boreal   Live     Coding · Atlas + Boreal  Yes     35 GiB

1 running placement across 2 Sparks. No attention required.
Inspect: vonkctl fleet detail Atlas
```

```text
Profile 2 · Coding · revision 7
Scope: all 2 enrolled Sparks

STOP     Previous chat  Atlas + Boreal  Endpoint will be withdrawn
START    Coding         Atlas + Boreal  Complete 2-Spark placement
TRANSFER Boreal                         18 GiB missing; Atlas can reuse assets

NAS cache: ready       Blockers: none
Plan: <full reviewed plan digest>
Apply these changes? [y/N]
```

```text
Profile 2 · request <UUID> · application <UUID>
Preparing Sparks
Atlas    Ready         Existing model and image reused
Boreal   Transferring  12 GiB / 18 GiB

Waiting on: Boreal model transfer
Owner: <reported owner>   Deadline: <reported deadline>
Next observation: 2s     Resumes when exact assets are ready
Ctrl-C stops watching; the Controller continues.
```

## 7. Controller/API dependencies

| Dependency | Existing foundation | Required closure and owner |
| --- | --- | --- |
| Reviewed profile submission | Preview returns profile and plan digests; applications retain them. | Profile service and canonical load request must enforce the supplied reviewed plan, including same-key reconciliation. No CLI-only comparison. |
| Precise observation | Stable application and noun operation GET routes. | CLI follows the exact route; API projects observation freshness, waiting cause, deadlines, and authorized next actions where missing. |
| Lost response discovery | Profile lookup by request key exists. | Check coverage for each other mutation; add lookup through its owning service when absent. Never invent success or submit a new key to discover the old one. |
| Cancel without eviction | Artifact jobs have a cancellation endpoint. | Model/recipe preparation and profile services need explicit typed cancellation APIs before their CLI commands ship. Cancellation does not imply rollback. |
| Full authoring | `FleetProfileInput` includes description, favorite, labels, retention, and assignment desired state. | Typed CLI updates preserve every untouched field; API remains admission authority. |
| Resource readiness/fit | Fleet, cache, recipe and preview projections exist. | Owners supply reusable named reasons; CLI formatting must not recompute admission independently. |
| Endpoint discovery | `GET /api/endpoints/{alias}` and client helper exist. | Profile/run projections supply authorized aliases and generation relationships. No inferred routing. |
| Activity and upgrade following | Operations, jobs, audit, job logs, and conditional job resume routes exist. | Choose the typed receipt's correct observation route, preserve pagination/source failures, and expose only valid resume actions. |
| Artifact jobs | Capability, create, upload, finalize, submit, status, cancel, result routes exist. | CLI connects the complete sequence with exact IDs and verified transfers. |

Extend current canonical contracts, regenerate OpenAPI/Python/TypeScript and
Rust wire structures when affected, and move producers/consumers together.
Do not add hidden application routes, schema-1 fallbacks, parallel DTO fields,
or a second compatibility path. Prefer existing intent/progress documents over
new SQL columns. Any necessary fresh-schema change is a separately presented
operator decision and cannot be auto-merged under this plan.

For every waiting operation, document dependency, owner, deadline, and resume
condition. The CLI holds no execution slot; API reads do not wait for remote
completion. Admission transactions are short and bounded. Artifact locks are
nonblocking and never acquired inside SQL transactions. Waiting parents release
execution slots; children inherit node ownership; stale attempts are fenced.
Verify those properties with real concurrent PostgreSQL/process tests where
the change touches them; an output test cannot prove deadlock freedom.

## 8. Delivery phases

Each phase is a bounded change or small sequence of changes. All new syntax
remains proposed until the corresponding phase lands. Runbook examples change
with implementation, not in advance.

| Phase | Deliverable and main files | Depends on | Exit criterion |
| --- | --- | --- | --- |
| P0 — Resolve current contracts | Command/API inventory; reconcile `--apply`, node-profile wording, global defaults, upgrade strategy, and output semantics. Files: parser, runbook, API contracts, documentation index. | This plan | One named current command for each task; every gap explicitly assigned to CLI or Controller; no undocumented compatibility interpretation. |
| P1 — Make the terminal predictable | Typed result/detail rendering, stream and exit rules, offline help, explicit profile selection, connection check, no-input mode, generated Bash/Zsh completion. Files: `cli.py`, `cli_render.py`, `cli_select.py`, package/runbook. | P0 | Subprocess/PTY checks pass; readable output at tested widths; JSON and errors are safe; completion cannot perform network work. |
| P2 — Inspect and choose | Fleet workload names/freshness, model/recipe readiness and fit, accurate empty states, complete pagination. Files: command/client/render modules and owning projections if needed. | P1 | Choose a supported exact candidate and identify a blocker without technical JSON or manual ID reconstruction. |
| P3 — Prepare and author | Cache preparation/reconnect and complete typed profile edits, installed-only assignments, import/export, revision protection. | P2 | Prepare → save draft works; interrupted transfer resumes; unrelated authoring fields survive; saving never applies. |
| P4 — Review and apply | Authoritative reviewed-plan precondition; human diff; interactive confirmation and scripted exact-plan submission; connected generated clients. Files: profile contract/API/service, CLI, existing web call sites only as required. | P3 | Stale review is refused; admitted execution binds reviewed identities; same-key replay cannot dispatch a different plan. |
| P5 — Observe and recover | Stable reconnect for all supported operations; explicit cancellation; condition/deadline reporting; safe lost-response reconciliation; activity and advertised resume. | P4 and cancellation/API closure | Real failure/restart tests prove preserved progress, final cancellation, newer-intent precedence, and unaffected unrelated work. |
| P6 — Complete the operator lifecycle | Published endpoint discovery, artifact jobs, removal impact, sequential fleet-upgrade following, signed CLI update UX. | P5 | Both serving and artifact recipes can reach usable results; maintenance scope and failure are unambiguous. |
| P7 — Qualify and hand off | End-to-end acceptance, package/CI evidence, short operator walkthroughs, current runbook, contract-to-web handoff inventory. | P1–P6 | All mandatory gates below have evidence; no unsupported workflow is presented as implemented. |

Keep commits scoped. Preserve other worktrees and uncommitted files. Land one
PR at a time when build inputs are affected; wait for an accepted generation
containing the preceding merge before arming another. Documentation-only merges
are not automatically build or promotion candidates.

## 9. Validation and acceptance

### 9.1 Validation completed for this plan

- Inspected the actual parser, renderer, client, current profile API/service,
  full OpenAPI, operator runbook, and governing architecture/error guidance.
- Compared the design decisions with the primary sources in section 3. These
  sources support the chosen patterns; they do not certify Vonk's behavior.
- Ran the existing CLI command, validated transport, and error-reporting tests:
  **75 passed in 5.12 seconds** on this checkout, using the existing root
  environment with pytest 9.1.1 and the sibling recipe path configured.
- Checked **22 existing command examples** against the actual parser, without
  executing their operations. Proposed syntax is separately labelled and was
  not treated as implemented. All 15 local document links resolve, code fences
  are balanced, and whitespace/diff checks pass.
- The fresh task-specific `uv` environment attempt could not resolve PyPI due
  to network/DNS restrictions. The successful existing-environment run is a
  focused baseline, not a fresh CI-environment reproduction.
- No deployment, physical Spark acceptance, model-quality evaluation, or
  operator usability study was performed for this design document.

The existing tests include an in-process client adapter and transport seams.
They are useful regression evidence for current behavior, not proof that all
proposed API, storage, process-death, or terminal behavior already works.

### 9.2 Required behavioral checks

Every new test must name the wrong implementation it rejects. Reuse existing
coverage where it already catches that defect; do not add another copied field
list, command inventory assertion, or mock that replaces the boundary under
review. Confirm a bug's regression test fails before applying its fix.

| Boundary | Case | Wrong implementation rejected |
| --- | --- | --- |
| Help/configuration | No credentials; offline help/version/completion; unsafe token file; TLS refusal. | Help performs network I/O; diagnostics disclose secrets; TLS errors trigger an insecure fallback. |
| Process streams | TTY, pipe, JSON, `TERM=dumb`, interruption, broken pipe, Unicode/control characters. | Progress contaminates JSON, errors contaminate human result pipes, screen clears leak into logs, or malicious text emits terminal controls. |
| Observation truth | Zero vs missing vs stale metrics; stale endpoint; partial source outage. | Missing evidence becomes healthy/empty; successful submission becomes successful workload. |
| Selection/pagination | Duplicate names, exact retained revision, final page, repeated cursor, changed filter. | First/fuzzy match or first page silently determines the wrong mutation target. |
| Authoring | Rename or add one assignment with existing metadata; revision conflict; installed-only choice; incomplete topology. | Read/modify/write drops fields, overwrites a concurrent edit, or infers desired state from live state. |
| Cache | Missing image/model, disk full, changed source, corrupt partial, interrupted refresh. | Admission trusts stale SQL success; failed refresh destroys the verified result; resume blesses corrupt bytes. |
| Review/apply | Change profile, target membership, selected recipe/image, or capacity between preview and load. | Fresh load silently substitutes an unreviewed plan or fails open on expired authority. |
| Request identity | Drop submission response before and after commit; replay same key with same/different intent. | Client creates duplicate work, returns another request's result, or lets a key authorize changed intent. |
| Following | Submit A, then B; reconnect by A's ID and key; observation timeout; Ctrl-C. | Observer jumps to B, cancels remote work, or labels a network timeout as execution failure. |
| Cancellation | Cancel while queued, transferring, executing, publishing; receive a stale worker result. | Cancelled/superseded intent restarts or stale results publish current readiness. |
| Coordination | One-slot parent/child chain; opposite node order; prepare/remove contention; database unavailable. | Parent holds its child's slot, locks wait cyclically, unrelated work stalls, or missing references authorize deletion. |
| Endpoint/jobs | Publish after all required ranks ready; expire/withdraw; interrupted input/output transfer; hash mismatch. | Partial topology is routable, alias is invented, output is published without verification, or every recipe is treated as chat. |
| Maintenance | Signed update tampering; multi-Spark upgrade; cache removal with references/active work. | CLI trusts unsigned bytes, upgrades consequential targets concurrently, or erases unrelated authority. |

Use real subprocesses/PTYs for terminal and signal semantics, the real client
and FastAPI routes for contract behavior, PostgreSQL for transactional races,
and separate processes/managed storage for durability and lock behavior.
Fixtures may supply external source responses; they cannot stand in for the
mechanism the test claims to verify.

### 9.3 Evidence gates

1. **Repository:** relevant fast-tier tests, focused CLI/PTY and connected
   contract checks; required Ruff, Python types, TypeScript build, generated
   wire checks, and `git diff --check` for implementation changes. Run root and
   control test trees separately with the configured recipe checkout.
2. **Linux/container:** use the intended OrbStack engine on macOS or the
   designated lane. Exercise actual PostgreSQL concurrency and process death;
   do not replace them with SQLite or mocked locks. Record Docker context and
   engine before claiming this gate ran.
3. **Publication:** verify packaged `vonkctl`, generated contracts, signed
   publication, and the accepted generation's build commit. A green local
   test does not establish published availability.
4. **Disposable Controller:** execute every journey through the CLI against
   the accepted package. Validate disconnect/restart and concurrent operators
   with captured request/application identities. No physical model claim.
5. **Physical Spark:** run an authorized representative single-Spark and
   two-Spark placement, endpoint request, interrupted preparation/recovery,
   and supported artifact workload. Record platform/recipe commits, recipe
   digest, image identity, and observed outcomes. Model quality remains its
   own acceptance evidence.
6. **Operator walkthrough:** a first-use operator connects and reaches a
   result from help; a returning operator finds a blocker and reconnects to
   work; an automation consumer uses only structured output and exit codes.
   Record errors, command backtracking, and unexplained pauses. Exact IDs may
   be copied from the CLI; no source browsing or raw-database repair is needed.

A gate not run is marked pending with its inputs and owner. Do not replace it
with a success claim from a lower gate.

## 10. Completion and web handoff

The CLI phase is complete when Steps A–J are implemented or explicitly scoped
to the existing bootstrap dependency, the new cancellation/API prerequisites
are closed, all supported operations have truthful follow/reconnect behavior,
and the evidence gates appropriate to each released capability are recorded.
An unmet core recovery or authority requirement blocks completion; it is not a
polish item to defer to the browser.

The handoff contains canonical status/decision/preview/progress contracts,
working journey examples, exact recovery semantics, safe error vocabulary,
and representative empty/stale/blocked/partial/completed fixtures. The web
can then present model selection, placement, impact, and progress without
inventing missing behavior or wrapping CLI subprocesses.

No aesthetic perfection gate is required. A person must reliably understand
what exists, what will change, what is happening, and what to do next.
