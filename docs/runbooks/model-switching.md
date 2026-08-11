# Model profile switching

`vonkctl` changes the complete desired state of the Vonk Forge GPU node cluster by
activating a named Cluster Profile. It does not start or stop an individual
model outside an accepted profile. The supported production path is the NAS
control API and outbound mTLS agents; the developer-machine controller and SSH
tunnels described by older examples are migration/recovery compatibility only.

Recipe installs and runs are planned from the local PostgreSQL catalog. The
catalog records the immutable recipe revision, resource envelope, placement,
and node-level disk/memory checks before any GPU node mutation. Existing checked-
in profiles continue to use the content-addressed admission checks below until
their equivalent catalog projection is enabled.

## Safety model

Each activation is serialized by `.state/vonkctl/switch.lock` and recorded
atomically in `.state/vonkctl/state.json`. Before any state or remote process
change, the controller resolves a selector or canonical profile ID and runs
admission against the exact profile hash, definition hashes, maturity records,
accepted combination evidence, and a fresh live inventory from both GPU nodes.
The successful activation stores the exact boot ID observed for each node.

If controller state names an active profile, its profile hash and complete
definition ID/hash set must match the current content-addressed catalog before
any mutation. Unknown or changed old runtime content is blocked for manual
recovery; the controller never guesses that a newly cataloged stop command is
safe for an unknown old process.

The checked-in `agent-full-dual` intent is currently `planned`. Its presence in
the catalog does not make it activatable. Until its adapter, checkpoint
manifest digest, and acceptance evidence have been recorded, activation must
return `blocked` without issuing a remote lifecycle command.

Use a dry run before an accepted transition:

```text
vonkctl switch PROFILE --dry-run
```

A dry run loads existing controller state read-only and performs resolution and
admission without acquiring the switch lock. It never creates the state
directory, calls a GPU node backend command, or saves controller state.

## Transition order

For an admitted activation the controller:

1. collects live health, capacity, and exact boot IDs from both GPU nodes;
2. checks whether an unchanged workload is eligible for retention;
3. verifies retained workloads are healthy;
4. writes `transitioning` state with no active profile, withdrawing published
   endpoint metadata before stopping changed services;
5. stops changed distributed workloads head first and worker second;
6. runs `verify-release` after every stop sequence;
7. verifies target runtime prerequisites;
8. starts distributed workloads worker first and head second;
9. after complete target residency is established, runs model-identity health
   checks and the adapter's pinned inference quality gate for every target
   workload, including retained workloads; and
10. atomically records the active profile, definition fingerprints, and the
    exact live boot IDs used for admission.

A workload is retained only when the persisted active profile hash still
matches the catalog, its persisted definition hash is unchanged, its placement
and endpoint aliases are identical in the old and new profiles, and its live
health command succeeds. Both persisted boot IDs must also match the current
live boot IDs. If either GPU node rebooted, no workload is retained: an explicit
switch performs the normal stop/start reconciliation and replaces the stored
boot IDs only after all final gates pass. Nothing restarts automatically.

`vonkctl endpoint ALIAS` is the live publication check. It repeats the node
health probe and refuses the address if either GPU node is unhealthy or
unreachable, either boot ID differs from activation, the active content is no
longer accepted, the adapter's read-only workload health check fails, or the
alias is not in the active profile. The check holds the same exclusive lock as
a transition and confirms persisted state is unchanged before returning, so a
concurrent switch cannot publish a withdrawn endpoint. Local `vonkctl status`
never performs this probe and therefore always reports
`published_endpoints: {}` rather than presenting persisted intent as live
availability.

For the dual-GPU node DeepSeek adapter, the controller appends the role argument
derived from the declared rank order:

```text
node2: profile-start deepseek-agent-dual worker
node1: profile-start deepseek-agent-dual head
node1: profile-stop deepseek-agent-dual head
node2: profile-stop deepseek-agent-dual worker
```

These commands are executed through the strict key-only SSH backend. OpenSSH
passes the complete POSIX-quoted argv as one command to the remote login shell;
the controller does not interpolate shell syntax, enable SSH agent forwarding,
or assume an always-present gateway.

## Failure behavior

Any start, health, quality-gate, or unexpected backend operational failure
withdraws every target endpoint and stops all target processes that may have
started, including a command that returned failure after creating a process.
Cleanup continues across per-node errors, follows each definition's declared
stop order, and then verifies resource release. Successful cleanup is persisted
as `stopped`; a failed stop or release check is persisted as `degraded`.
Diagnostics, remote output, and `last_error` retained by the report or state
are bounded.

The controller never chooses another profile and never automatically restarts
the previous heavyweight profile. A persisted `transitioning` or `degraded`
state blocks another automatic activation until the operator has inspected the
reported node, workload, operation, and diagnostic detail and performed manual
recovery.

## Explicit restoration

Restoration is request state, not Cluster Profile metadata:

```text
vonkctl switch creative-3d --restore default
```

The selector `default` resolves to canonical profile `agent-full-dual`.
This option stores only the canonical restore intent in controller state and
the switch report. It never restores within the same `vonkctl switch` call.
After the caller has completed its work and explicitly recovered the outputs
and provenance, `vonkctl restore-default` performs a later ordinary profile
switch. That later switch reacquires the lock and repeats all admission,
health, quality, and failure gates. The original switch report keeps the
temporary producing profile and definition hashes; no fallback profile is
chosen automatically.

## Recovery checklist

When status is `degraded` or `transitioning` after interruption:

1. preserve `.state/vonkctl/state.json` and the reported diagnostics;
2. inspect each implicated adapter directly over key-only SSH;
3. stop only the declared workload processes in head-first order where
   distributed;
4. run the matching `verify-release` command on every declared node; and
5. repair controller state only after both GPU nodes are known to be stopped.

Do not delete model snapshots, output artifacts, runtime caches, or logs as a
switch-recovery shortcut.

## Development recipe acceptance

The catalog-backed development image lane does not use `vonkctl switch` for its
physical qualification. Follow [Development agent workload
acceptance](development-agent-workloads.md) for the source-build, exact image
distribution, single-node/multi-node recipe, route, restart, rank-failure, and
normal uninstall gates. Successful development evidence does not promote a
planned production Cluster Profile or bypass its immutable acceptance record.
