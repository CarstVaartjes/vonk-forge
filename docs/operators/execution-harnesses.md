# Execution harness operations

An execution harness is the stable lifecycle contract between a recipe and the
Spark agent. It compiles declarative recipe inputs into the universal source,
build, distribute, install, run, health, route, stop, and uninstall operations.
Operators act through those operations and their preview digests; they do not
start a parallel container and then write catalog state by hand.

See [Model catalog](model-catalog.md) for model identity and revision concepts.

## Harness, distribution, and patch

A **harness** defines adapters, required capabilities, supported lifecycle, and
the compiler that turns a recipe into shell-free runtime intent. A **runtime
distribution** proves one exact upstream source and digest-pinned base image
implement that harness on a platform. It records dependencies, licenses,
security policy, offline behavior, and any verified distributed mechanism. A
**patch bundle** is an optional, immutable, recipe-referenced set of changes for
one exact target. It never silently modifies the harness or a resolved runtime.

This split permits several accepted distributions for one harness and permits a
recipe-local patch without inventing a ninth lifecycle. Add a new harness only
when an accepted target cannot implement the universal lifecycle through an
existing built-in.

## The eight built-in harnesses

The supported v1 seed is exactly `comfyui`, `diffusers`, `ds4`, `llama-cpp`,
`pytorch-pipeline`, `sglang`, `tensorrt-llm`, and `vllm`. A fresh development
database must resolve those eight `vonk-forge` execution-harness entities and
no prototype catalog state. Their names describe execution contracts, not a
promise that every model works with every distribution or topology; the recipe,
distribution capability, structural qualification, and Fleet admission still
have to agree.

## Interface publication

An `openai` interface follows the serving path
`client → Tailscale → Caddy → LiteLLM → accepted entrypoint`. The controller
publishes the recipe alias to LiteLLM only after all required ranks report fresh,
matching evidence and the route-serving lease is valid. Caddy owns static path
and trust boundaries; LiteLLM neither discovers containers nor resolves catalog
documents. Rank loss, stale evidence, stop, or lease loss withdraws the alias.

Artifact-producing interfaces are jobs rather than OpenAI model routes. Their
submission, progress, cancellation, and result artifacts use the declared
controller/job interface directly; a result location is not placed in LiteLLM.
Health and cleanup still use the same harness lifecycle and exact node evidence.

## Clean development reset

The reset is intentionally destructive and pre-production-only. It first
requires both exact gates:

```bash
scripts/reset-development-recipe-domain \
  --environment development \
  --project-name vonk-forge \
  --journal-file /volume1/vonk-reset-state/task-9-reset.json \
  --docker-mode sudo \
  --confirm-destructive-preproduction-reset
```

Run the reset as the unprivileged NAS operator with non-interactive
`sudo -n docker` authority. Do not run the whole script through `sudo`: its
short-lived administrator token must remain a mode-`0600`, operator-owned file.
The permanent NAS project still contains only `docker-compose.yaml` and
`secrets/`; stage the repository script and token outside that directory for
the reset and remove both afterward.

Before mutation, the script resolves Compose with explicit project name
`vonk-forge`, validates the exact development services and named volumes, and
freezes a mode-`0400`, SHA-256-bound Compose snapshot beside a mode-`0600`
phase journal outside the permanent project. It inspects the actual project
containers, service labels, mounts, volumes, and project/volume labels. Any
orphan, anonymous mount, foreign label, changed graph, or unexpected object
fails before teardown. Every later Compose call uses the immutable snapshot
and explicit project name. Teardown uses bounded `compose stop` and `compose
down` without broad volume/orphan flags, then removes only each prevalidated
named volume explicitly.

The journal advances only after drain, stop, down, exact volume deletion,
PostgreSQL startup, migration, stack startup, and verification. An interrupted
run resumes from that hash-bound phase without assuming the API is available
during teardown. Final verification requires `/api/v1/agents` to be empty;
PostgreSQL Fleet registrations are empty, so no nodes are projected and no
inventory, telemetry, workload, or reservation state remains. Terminally
paginated catalog reads must contain exactly the eight built-in harness
identities and their checked-in content digests, no recipes, and no prototype
state. Migration head must be exactly `0001_fleet_library_baseline`.

This is a truly fresh control domain: all control database rows, users, browser
sessions, agent enrollments, route publications, repository projection,
supervisor state, generated runtime configuration, control identity, LiteLLM
database state, and Tailscale gateway state in the project volumes are removed.
The NAS `secrets/` source generation is retained. Spark-local model and build
caches are outside this reset; they may be reused only when the acceptance run
independently verifies the exact required digest. There is no legacy migration,
prototype conversion, compatibility import, or preservation path.

After reset, let the development auth initializer recreate exact administrator
subject `admin` from the retained verifier, sign in to establish a fresh browser
session, create a fresh one-use grant for each Spark, and perform the strict
grant/pair/approve/pair sequence from
[Install the Vonk Forge agent](../operations/install-vonk-agent.md). Do not use a
pre-reset browser cookie, pairing token, certificate enrollment, acceptance
file, or node binding as proof. Create a fresh short-lived administrator token
for the post-reset acceptance and remove it when the run finishes.

## Acceptance evidence

`scripts/accept-recipe` checks recipe node count before credentials or network
access. Structural qualification resolves every exact checked-in catalog
reference and source bundle. Read-only Fleet and agent snapshots bind each
operator selector to one certificate-bound `spk_…` identity, agent build,
supervisor generation, certificate expiry, fresh inventory, capacity, and
fabric. Fleet selectors and SSH destinations are separate inputs: current
Fleet hostnames `spark-3542` and `spark-2297` map explicitly to inventory SSH
aliases `vonk-node-1` and `vonk-node-2`. The mapping is validated before any
network access and recorded in evidence. Read-only SSH preflight verifies
native `linux-arm64`, the exact NVIDIA driver and Docker runtime identities,
the native NVIDIA runtime, image manifest
access, and every model artifact URL. Dual-node preflight also runs
`scripts/validate_fabric.py --preflight-only`, binding selected Fleet IDs to
inventory aliases and checking reciprocal peers, both interfaces, HCAs, GID
indices, consumers, and bounded live path probes. Physical lifecycle work is
delegated to `scripts/run-development-slices`, the repository's canonical
public-API runner.

Evidence is canonical JSON in a mode-`0600` file. Its phase list is a strict
prefix of `authored`, `structurally-verified`, `container-verified`,
`spark-canary`, and `spark-accepted`. A phase advances only after independently
validating all exact runner outputs required at that point. State names without
an image digest, artifact-set digest, inference digest, cleanup operation, or
advanced post-restart supervisor generation cannot overstate acceptance. A
changed recipe, catalog digest, node/certificate binding, qualification file,
API origin, topology, or noncanonical sidecar cannot resume older evidence.

At each restart checkpoint the runner records both the supervisor generation
from `/api/v1/agents` and the host boot ID from serialized Fleet telemetry.
Heartbeat timestamps and Fleet `generated_at` are not restart proof. Every
selected node must return with both a strictly greater generation and a
different boot ID before route and inference evidence can be bound to the new
identity; cleanup remains deferred until that gate succeeds.

One-Spark acceptance pauses after canary inference for an offline restart.
Distributed acceptance additionally pauses for failure-rank loss, route
withdrawal, rank recovery, recovered inference, and a final offline restart.
The same command is rerun after each explicit operator action; checkpoint exit
code `4` means the evidence is valid but not complete. Only the full single-node
ladder, or the full distributed rank-loss/recovery ladder, cleanup, and advanced
supervisor generations can write `spark-accepted`.

## Controller execution sequence

These commands are an execution handoff, not evidence that the physical steps
have run. Keep pre-reset preflight evidence separate from fresh acceptance
evidence so stale enrollment identity can never be promoted.

```bash
install -d -m 0700 .state/recipe-acceptance
scripts/dev-admin-token \
  --output "$PWD/.state/recipe-acceptance/admin-token" \
  --signing-key-file '<DEVELOPMENT_TOKEN_SIGNING_KEY_FILE>' \
  --ttl-seconds 3600

scripts/accept-recipe \
  --recipe ../vonk-forge-recipes/recipes/deepseek-v4-flash-0731-ds4-single.json \
  --library-root ../vonk-forge-recipes \
  --nodes spark-3542 \
  --ssh-target spark-3542=vonk-node-1 \
  --preflight-only \
  --evidence-file "$PWD/.state/recipe-acceptance/pre-reset-ds4.json"

scripts/accept-recipe \
  --recipe ../vonk-forge-recipes/recipes/deepseek-v4-flash-0731-mia-dual.json \
  --library-root ../vonk-forge-recipes \
  --nodes spark-3542,spark-2297 \
  --ssh-target spark-3542=vonk-node-1 \
  --ssh-target spark-2297=vonk-node-2 \
  --preflight-only \
  --evidence-file "$PWD/.state/recipe-acceptance/pre-reset-mia.json"

scp scripts/reset-development-recipe-domain \
  "${NAS_SSH_HOST}:/tmp/vonk-reset-development-recipe-domain"
scp "$PWD/.state/recipe-acceptance/admin-token" \
  "${NAS_SSH_HOST}:/tmp/vonk-reset-admin-token"
ssh -t "$NAS_SSH_HOST" '
  set -eu
  trap "rm -f /tmp/vonk-reset-development-recipe-domain /tmp/vonk-reset-admin-token" EXIT
  install -d -m 0700 /volume1/vonk-reset-state
  chmod 0700 /tmp/vonk-reset-development-recipe-domain
  chmod 0600 /tmp/vonk-reset-admin-token
  /tmp/vonk-reset-development-recipe-domain \
    --environment development \
    --project-directory /volume1/docker/vonk-forge \
    --project-name vonk-forge \
    --journal-file /volume1/vonk-reset-state/task-9-reset.json \
    --api-base http://127.0.0.1:8080 \
    --admin-token-file /tmp/vonk-reset-admin-token \
    --docker-mode sudo \
    --confirm-destructive-preproduction-reset
'
rm -f "$PWD/.state/recipe-acceptance/admin-token"
```

After the reset, sign in as the newly recreated `admin`. For each Spark, create
a different one-use grant in that fresh browser session, place it in the
node-local mode-`0600` file shown below, run the command, approve the displayed
pending enrollment in the browser, then run the same command once more:

```bash
sudo -u vonk-agent -- \
  /var/lib/vonk-forge/supervisor/current/vonk-agent pair \
  --enrollment https://<ENROLLMENT_HOSTNAME>:8443/ \
  --ca-sha256 <64_LOWERCASE_HEX_FROM_SHA256SUM> \
  --token-stdin < /run/secrets/vonk-enrollment-token
```

Start the supervisor only after the second pairing succeeds and verify both
fresh `spk_…` identities and inventories in Fleet. Then create fresh token files
as described in
[Development agent workload acceptance](../runbooks/development-agent-workloads.md)
and run the physical ladders with new evidence paths:

```bash
scripts/accept-recipe \
  --recipe ../vonk-forge-recipes/recipes/deepseek-v4-flash-0731-ds4-single.json \
  --library-root ../vonk-forge-recipes \
  --nodes spark-3542 \
  --ssh-target spark-3542=vonk-node-1 \
  --level spark \
  --evidence-file "$PWD/.state/recipe-acceptance/fresh-ds4.json"

scripts/accept-recipe \
  --recipe ../vonk-forge-recipes/recipes/deepseek-v4-flash-0731-mia-dual.json \
  --library-root ../vonk-forge-recipes \
  --nodes spark-3542,spark-2297 \
  --ssh-target spark-3542=vonk-node-1 \
  --ssh-target spark-2297=vonk-node-2 \
  --level spark \
  --evidence-file "$PWD/.state/recipe-acceptance/fresh-mia.json"
```

At a Mia rank checkpoint, keep the Rust agent running and execute only the
emitted inspect plus exact `vonk-<run-id>` container stop/start action after all
three managed/run/runtime-request labels match. At every checkpoint, perform
only the named restart or failure/recovery action from the script, confirm
Fleet reflects it, and rerun the identical command.
Archive the final private evidence outside Git. Do not call the result accepted
until both documents say exact status `spark-accepted` and all expected routes
are withdrawn after cleanup.
