# `vonkctl` control API client

`vonkctl` is the routine operator interface to the authenticated control API.
It does not read local controller state, construct SSH dependencies, contact a
GPU node directly, or fall back when the control plane is unavailable. Configure
the HTTPS origin and a restrictive bearer-token file before use:

```bash
export VONK_CONTROL_URL=https://control.example.invalid
export VONK_CONTROL_TOKEN_FILE=/run/secrets/vonk-control-token
```

The routine command surface is:

```bash
uv run --project /path/to/vonk-forge vonkctl nodes status --json
uv run --project /path/to/vonk-forge vonkctl validate PROFILE --json
uv run --project /path/to/vonk-forge vonkctl prepare PROFILE [--apply] [--wait|--no-wait] --json
uv run --project /path/to/vonk-forge vonkctl switch PROFILE [--apply] [--wait|--no-wait] --json
uv run --project /path/to/vonk-forge vonkctl restore-default [--apply] [--wait|--no-wait] --json
uv run --project /path/to/vonk-forge vonkctl endpoint ALIAS --json
```

`nodes status` and `endpoint` return their typed server resources. `validate`
requests and prints the exact server plan without applying it. `prepare`,
`switch`, and `restore-default` also print that exact plan by default. A
mutation requires `--apply`, submits only the digest from the freshly returned
plan, and waits for the accepted job unless `--no-wait` is selected. `--wait`
is available for explicit scripting. Control errors emit bounded, redacted
`error_type=control_api` output and stop.

## Explicit legacy compatibility

`bin/vonkctl-legacy` preserves the retired developer-machine controller for
explicit migration and recovery compatibility only. It may use local state and
SSH. It is not a production interface, must not be installed or configured as
the ordinary `vonkctl`, and is never selected implicitly after an API error.

The remainder of this document records the compatibility controller's former
behavior for recovery context. Commands in this archived section require the
separately named `vonkctl-legacy` launcher; they are not supported by routine
`vonkctl`.

Its checked adapter Compose files can contain host networking and host IPC;
host networking and host IPC are legacy runtime exceptions for the historical
NCCL/RoCE implementation. They are not authority for accepted recipe policy.
The supported outbound-agent path normally rejects host networking, host IPC,
raw InfiniBand, Docker socket access, and added capabilities. Its sole
direct-fabric exception is a connected multi-node recipe compiled to the exact
host-network/IPC/InfiniBand shape and guarded by the packaged host firewall;
arbitrary variants remain rejected.

## Archived local-controller behavior

Developer-machine SSH selection is cross-platform. macOS and native Linux use
`ssh` and `scp` from `PATH`. WSL uses `ssh.exe` and `scp.exe` only when WSL is
detected and those Windows OpenSSH commands are available; otherwise it falls
back to the POSIX commands. `VONK_SSH_BIN` and `VONK_SCP_BIN` explicitly
override those defaults, including for wrappers that integrate a credential
agent. Runtime-release deployment reapplies the manifest-required POSIX modes
after transfer and then performs its unchanged hash-and-mode final verification,
because Windows SCP bridges can preserve bytes without preserving executable
bits.

## Commands

```text
vonkctl-legacy catalog [--json]
vonkctl-legacy validate PROFILE_OR_SELECTOR [--json]
vonkctl-legacy status [--json]
vonkctl-legacy nodes status [--json]
vonkctl-legacy prepare PROFILE_OR_SELECTOR [--json]
vonkctl-legacy switch PROFILE_OR_SELECTOR [--restore PROFILE_OR_SELECTOR] [--dry-run] [--json]
vonkctl-legacy restore-default [--dry-run] [--json]
vonkctl-legacy endpoint ENDPOINT_ALIAS [--json]
vonkctl-legacy break-stale-lock [--json]
```

- `catalog` lists profiles, workload definitions, content hashes, maturity, and
  selector mappings. Planned profiles remain visible.
- `validate` resolves the selector, confirms the checked-in contracts loaded,
  collects live health and capacity from both GPU nodes, and runs admission.
  `valid: true` does not imply `admitted: true`.
- `status` reads only local controller state. It makes no SSH call. Endpoint
  availability is always fail-closed as `published_endpoints: {}` because a
  persisted snapshot cannot establish that either GPU node is still alive.
- `nodes status` concurrently probes both configured GPU nodes and reports live
  host, NVIDIA, thermal, and direct-fabric health without retaining history or
  changing either node or the active profile. See
  [Live node health](node-health.md).
- `prepare` resolves a selector, acquires the same controller lock used by
  transitions, and requires a clean `stopped` state with no active profile or
  transitional target. It invokes each workload's declared `prepare` command
  concurrently on all of that workload's nodes, with the definition's
  operation-specific deadline applied independently to every node. Preparation
  does not run admission, change controller state, publish an endpoint, or
  activate a profile.
- `switch` resolves selectors before invoking the ordinary switch path.
  `--dry-run` reports only the truthful status, hashes, and restore intent
  exposed by the switcher; the CLI does not maintain a second action planner.
- `--restore` records a canonical restoration intent. It never restores during
  the same switch call.
- `restore-default` is a later, explicit ordinary switch to selector `default`.
  Run it only after outputs and provenance from temporary work are recovered.
- `endpoint` returns an address only for an alias published by an active,
  currently accepted profile after a fresh health probe confirms both GPU nodes
  are reachable and their exact boot IDs still match the successful
  activation. It holds the transition lock and runs the workload adapter's
  read-only health check before returning the address. Stopped, stale,
  rebooted, unhealthy, planned, dead, or unpublished endpoints are denied.
- `break-stale-lock` uses the state-store safety checks. It refuses a held lock,
  a live local PID, a lock written by a different controller host, or a lock
  younger than the configured threshold. A foreign-host record must be
  inspected and recovered on that host; age alone never authorizes removal.

Operational `validate`, `switch`, `restore-default`, and `endpoint` commands
use the same live health collector as `nodes status`. Health is projected into
the bounded admission inventory: node health, available memory, root-disk
space, and boot ID. A missing or failed probe blocks admission or publication;
it never falls back to stale local measurements. The checked-in
`agent-full-dual` profile resolves correctly but remains unactivatable while
`deepseek-agent-dual` has `verified` maturity. Its direct Mia runtime is
operational, but profile admission remains fail-closed until the definition is
`accepted`. The single-GPU node `deepseek-agent-single` DS4 definition is also
operational and `verified`, but has no accepted profile path: `vonkctl-legacy` must
continue to reject it until performance, thermal, lifecycle, reboot, and exact
co-residency evidence advances the definition and a complete profile to
`accepted`.

## Durable preparation

Run preparation only after deploying the exact digest-qualified runtime
release and while `vonkctl-legacy status` reports a clean stopped state:

```bash
uv run --no-project --with jsonschema -- \
  bin/vonkctl-legacy prepare default --json
```

The adapter owns the durable node-local preparation job. For each workload, the
controller submits exactly the nodes declared by that definition, using the
operation-specific deadline independently for each call. The dual-GPU node Mia
definition submits GPU node 2 as `worker` and GPU node 1 as `head` concurrently; the
single-GPU node DS4 definition submits only GPU node 1 and has no role suffix. Results
report every declared node's role (when applicable), timeout, return code, and
bounded diagnostic independently in deterministic definition order, even when
calls finish in a different order. A timeout or failure on one node does not
prevent another declared node from being collected.

Worker-first and head-first ordering applies to distributed runtime startup and
shutdown, not artifact preparation. The Mia definition prepares both GPU nodes in
parallel; the DS4 definition prepares GPU node 1 only.

A client-side timeout returns status `in-progress`, `resumable: true`, and exit
code `8`. It does not issue `stop`, kill the remote job, write controller
state, or change the active profile. Re-run the same command to reattach to the
deterministic preparation job. A nonzero adapter result is `failed` and exit
code `6`; a non-clean controller state is `blocked` and exit code `3`.

Preparation starting or finishing does not advance Model Definition maturity.
The separate prepared, verified, and accepted evidence gates remain required.

## Remote container prerequisite

This subsection applies only to the explicitly archived
`vonkctl-legacy` SSH controller described above. It is not a fresh-install
instruction. The supported outbound-agent path must not add `vonk-agent` to
the Docker group and does not require an operator account there either.

Profile transitions must start and stop containers noninteractively. On each
dedicated GPU node, the trusted `carst` administrator therefore belongs to the
`docker` group. This is root-equivalent access and must not be extended to
untrusted accounts.

After adding the group membership, close the SSH session and reconnect before
testing because an existing login retains its original supplementary groups:

```bash
sudo usermod -aG docker carst
exit
ssh vonk-node-1  # or vonk-node-2
id -nG
docker version --format '{{.Server.Version}}'
```

The group list must include `docker`, and the server query must succeed. For
live-collector failures, continue with the Docker-specific troubleshooting in
[Live node health](node-health.md).

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Successful read, admitted dry-run, or completed transition |
| `2` | Invalid arguments, selector, catalog, or controller configuration |
| `3` | Admission blocked or endpoint unavailable |
| `4` | `nodes status`: at least one node is `critical` or `unreachable` |
| `5` | Local health collector, schema, inventory, or baseline failure before probing |
| `6` | Transition or explicit restoration failed |
| `7` | Switch-lock conflict or unsafe stale-lock override |
| `8` | Durable preparation is still running after the client deadline; rerun to resume |

CLI errors and switch diagnostics are bounded and redact common credential,
authorization, token, password, secret, and private-key forms. Do not place
credentials in profile files, command arguments, or remote diagnostic output.
Argument failures that include a sensitive option use generic error text so a
whitespace-separated option value cannot be echoed by the parser.

## Safe bring-up checks

These archived compatibility commands do not mutate either GPU node.
`catalog` and `status` are local; `validate` and `nodes status` perform live
read-only probes:

```bash
uv run --no-project --with jsonschema -- bin/vonkctl-legacy catalog --json
uv run --no-project --with jsonschema -- bin/vonkctl-legacy validate default --json
uv run --no-project --with jsonschema -- bin/vonkctl-legacy status --json
uv run --no-project --with jsonschema -- bin/vonkctl-legacy nodes status --json
```

At the current milestone, `catalog` succeeds, `validate default` exits `3` with
the verified-not-accepted maturity denial, and `status` reports `stopped` when
no local state has been written. On 2026-08-02, `nodes status --json` exited `0` with both
nodes healthy, Docker available, and no warnings or errors. It exits `4` if a
later probe finds either node critical or unreachable. The controller/profile
framework and live health are implemented. The pinned Mia DeepSeek runtime is
installed, running and quality-verified, but is not yet accepted. Performance
fine-tuning plus sustained thermal, repeated lifecycle and reboot gates are
deferred to the final cross-model optimization phase.
