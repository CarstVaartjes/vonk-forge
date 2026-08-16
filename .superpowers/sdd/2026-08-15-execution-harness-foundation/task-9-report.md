# Task 9 report: Fresh reset, physical acceptance, and operator documentation

Date: 2026-08-16

Status: DONE_WITH_CONCERNS

Implementation commits:

- `aa641a3868d667e5e077271e81fd38e0e614e717` — initial Task 9 implementation.
- `cec0738f138fd2c6798bf60829fdc4e5a583074d` — initial Task 9 report.
- `41e8f7735bd19362f50cb5a27353beb2fd420262` — review-fix round 1 implementation and documentation.

Physical Steps 6–8 remain intentionally pending controller execution. No SSH,
NAS reset, Compose deployment, Spark mutation, re-enrollment, workload install,
or physical acceptance was performed in this implementation round.

## Review remediation

- Reset now requires exact project name `vonk-forge` and a private journal path
  outside the permanent NAS project. It renders the real nameless NAS artifact
  with explicit `--project-name`, freezes a mode-`0400` canonical Compose
  snapshot, binds its SHA-256 into a mode-`0600` phase journal, and uses that
  snapshot for every later Compose call.
- Before mutation it inspects actual project containers, service labels,
  mounts, exact named volumes, and project/volume labels. Orphans, anonymous or
  foreign mounts, incomplete service sets, redirected volumes, changed labels,
  and source-graph swaps fail closed. Teardown uses `compose stop` and
  `compose down` without `--volumes` or `--remove-orphans`, then deletes only
  individually validated exact named volumes.
- The journal records strict phases `validated`, `drained`, `stopped`, `down`,
  `volumes-deleted`, `postgres-started`, `migrated`, `stack-started`, and
  `verified`. Fault-injection covers every destructive boundary, API
  unavailability during teardown, and label substitution before resumed volume
  deletion.
- Fresh verification requires `/api/v1/agents` to be empty while repository
  Fleet nodes remain exact `ready` projections with unregistered connection,
  no inventory, telemetry, installations, runs, or reservations. Terminal
  pagination proves exactly the eight built-in harness identities and checked-in
  content digests, zero recipes, and no prototype catalog state at exact head
  `0027_execution_harness_catalog`.
- Readiness is shared between acceptance and the runner: lifecycle `ready`,
  agent state `active`, certificate state `valid`, online state `online`, and
  inventory freshness `fresh`, plus required capabilities.
- Restart checkpoints now capture each selected node's host boot ID from the
  real serialized `FleetSnapshot` and supervisor generation from
  `/api/v1/agents`. Heartbeat and `generated_at` movement cannot satisfy the
  gate. Both boot ID difference and strict generation increase are required;
  post-restart Fleet and inference digests are hash-bound to that identity, and
  cleanup remains after the gate. The no-restart retry regression proves the
  sidecar prefix remains safe and cleanup is not attempted.
- Fleet selectors and SSH aliases are separate ordered CLI inputs. Current
  hostnames `spark-3542` and `spark-2297` map to `vonk-node-1` and
  `vonk-node-2`; selector reuse as an SSH destination is rejected before
  network access and the mapping is retained in evidence.
- Dual-node preflight invokes `scripts/validate_fabric.py --preflight-only`
  with exact selected Fleet-ID/SSH-alias bindings. The existing validator checks
  reciprocal peers, both interfaces, HCAs, GID indices, consumer variables,
  absence of fabric default routes, and bounded interface-bound peer probes.
- Mia checkpoints keep the Rust agent running and emit only exact
  `vonk-<run-id>` container inspect plus stop/start actions, including the three
  managed/run/runtime-request labels. No checkpoint instructs stopping an
  agent.
- Reset fixtures use the actual NAS publisher transformation and acceptance
  fixtures serialize through the repository's real `FleetSnapshot` models.
  The permanent NAS layout remains only `docker-compose.yaml` plus `secrets/`.

## Exact TDD evidence

Reset review RED before production changes:

```text
uv run --project control --frozen python -m pytest scripts/tests/test_reset_development_recipe_domain.py -q
19 failed, 1 passed in 10.08s
```

The failures were the absent required project/journal contract and the new
orphan, anonymous-volume, graph-swap, exact-label, and recovery behavior.

Restart/readiness RED before runner changes:

```text
uv run --project control --frozen python -m pytest scripts/tests/test_run_development_slices.py::test_model_restart_gate_rejects_heartbeat_then_retries_without_cleanup -q
1 failed in 0.66s
```

The old readiness vocabulary failed first and the runner had no boot-ID plus
supervisor-generation checkpoint.

Acceptance/fabric/mapping/Mia RED before production changes:

```text
uv run --project control --frozen python -m pytest scripts/tests/test_accept_recipe.py::test_mia_preflight_binds_two_distinct_fabric_nodes_and_peak_memory_contract scripts/tests/test_accept_recipe.py::test_preflight_requires_complete_selector_to_ssh_mapping_before_network scripts/tests/test_accept_recipe.py::test_mia_checkpoint_emits_label_verified_rank_container_action tests/scripts/test_validate_fabric.py::test_expected_fleet_nodes_bind_to_exact_inventory_ssh_aliases -q
4 failed in 1.13s
```

Bounded peer-probe RED:

```text
uv run --project control --frozen python -m pytest tests/scripts/test_validate_fabric.py::test_read_only_preflight_probes_each_exact_peer_on_its_bound_interface -q
1 failed in 0.08s
```

Resume-time exact-volume-label RED:

```text
uv run --project control --frozen python -m pytest scripts/tests/test_reset_development_recipe_domain.py::test_reset_resume_rejects_exact_named_volume_whose_project_labels_changed -q
1 failed in 1.27s
```

The old retry skipped an exact-named volume after its Compose labels changed.

## Exact GREEN evidence

```text
uv run --project control --frozen python -m pytest scripts/tests/test_reset_development_recipe_domain.py -q
22 passed in 26.25s
```

```text
uv run --project control --frozen python -m pytest scripts/tests/test_run_development_slices.py -q
61 passed in 37.24s
```

```text
uv run --project control --frozen python -m pytest scripts/tests/test_accept_recipe.py -q
14 passed in 9.14s
```

```text
uv run --project control --frozen python -m pytest tests/scripts/test_validate_fabric.py tests/test_docs_contract.py -q
74 passed in 4.18s
```

```text
uvx --from ruff==0.16.1 ruff check scripts/accept-recipe scripts/reset-development-recipe-domain scripts/run-development-slices scripts/fleet_snapshot_contract.py scripts/validate_fabric.py scripts/tests/test_accept_recipe.py scripts/tests/test_reset_development_recipe_domain.py scripts/tests/test_run_development_slices.py tests/scripts/test_validate_fabric.py
All checks passed!
```

```text
cargo test --workspace --all-targets
177 passed; 0 failed
```

`git diff --check` exited `0` before the implementation commit.

Two full `control/tests` processes were inadvertently started concurrently by
the execution wrapper. Per controller instruction, no third full control run
was started; both existing processes were allowed to finish and one completed
stream was used. The detached wrapper retained progress output but not its
terminal pytest summary, so this report does not overstate that run as exact
GREEN. The fresh focused suites above are the canonical local Task 9 evidence.

The requested historical command
`cargo test --manifest-path agent/rust/Cargo.toml --all-targets` exits `1`
because that manifest no longer exists; the repository's current Rust command
is the successful workspace command above.

The unrelated full Python agent suite is not GREEN in this workstation
environment:

```text
uv run --project agent --frozen python -m pytest agent/tests -q
34 failed, 151 passed, 1 skipped; exit 3
```

The first deterministic failure is
`agent/tests/packages/test_adapter.py::test_digest_selected_dynamic_adapter_executes_without_a_compiled_name`
because this Python runtime exposes no `os.memfd_create`; later failures also
trigger pytest's known legacy internal-error path. Per controller direction,
the separate legacy/root-suite cleanup was not changed in this round.

## Files

- `scripts/reset-development-recipe-domain`
- `scripts/accept-recipe`
- `scripts/run-development-slices`
- `scripts/fleet_snapshot_contract.py`
- `scripts/validate_fabric.py`
- `scripts/tests/test_reset_development_recipe_domain.py`
- `scripts/tests/test_accept_recipe.py`
- `scripts/tests/test_run_development_slices.py`
- `tests/scripts/test_validate_fabric.py`
- `docs/operators/execution-harnesses.md`
- `docs/runbooks/development-agent-workloads.md`
- `docs/runbooks/fabric.md`
- this report

## Safety model and external assumptions

- The NAS operator owns a private mode-`0700` reset-state directory outside
  `/volume1/docker/vonk-forge`; the permanent project contains exactly
  `docker-compose.yaml` and `secrets/`.
- The deployed Compose artifact is the output of the repository publisher,
  contains no `name`, and resolves only through explicit project name
  `vonk-forge`. Docker Compose must support JSON `config`, `--project-name`,
  `--project-directory`, and `--wait` as documented for the development NAS.
- Existing project containers and volumes carry Docker Compose's exact project,
  service, and volume labels. Any mismatch is a blocker, not repair authority.
- The current Fleet identities remain display names `DGX Spark 1` and
  `DGX Spark 2`, hostnames `spark-3542` and `spark-2297`, with inventory SSH
  aliases `vonk-node-1` and `vonk-node-2`. SSH host keys and
  `inventory/cluster.toml` must already be independently trusted.
- Each accepted Spark must publish current real telemetry with a canonical host
  boot UUID and `/api/v1/agents` must publish an active, non-stale agent with a
  strictly monotonic supervisor generation.
- `scripts/validate_fabric.py --preflight-only` performs read-only SSH and
  bounded ICMP probes when the controller runs it. No such probe was run here.
- Development images/packages must be published from the independently accepted
  commit and deployed before physical reset. The reset is destructive and has
  no legacy compatibility or preservation path.

## Exact controller commands — do not infer execution from this report

Create a private token and run non-destructive preflight through the accepted
loopback control/inference forwarding:

```bash
install -d -m 0700 "$PWD/.state/recipe-acceptance"
scripts/dev-admin-token \
  --output "$PWD/.state/recipe-acceptance/admin-token" \
  --signing-key-file '<DEVELOPMENT_TOKEN_SIGNING_KEY_FILE>' \
  --ttl-seconds 3600

scripts/accept-recipe \
  --recipe config/recipes/deepseek-v4-flash-0731-ds4-single.json \
  --nodes spark-3542 \
  --ssh-target spark-3542=vonk-node-1 \
  --preflight-only \
  --evidence-file "$PWD/.state/recipe-acceptance/pre-reset-ds4.json"

scripts/accept-recipe \
  --recipe config/recipes/deepseek-v4-flash-0731-mia-dual.json \
  --nodes spark-3542,spark-2297 \
  --ssh-target spark-3542=vonk-node-1 \
  --ssh-target spark-2297=vonk-node-2 \
  --preflight-only \
  --evidence-file "$PWD/.state/recipe-acceptance/pre-reset-mia.json"
```

Only after independent review and successful preflight, stage and run the exact
reset on the NAS:

```bash
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
```

After the initializer recreates administrator subject `admin`, establish a new
browser session. For each Spark create a different fresh one-use grant, place it
in the node-local mode-`0600` token file, and run this command once to submit,
approve the displayed request in the new browser session, then run it again to
collect the certificate:

```bash
sudo -u vonk-agent -- \
  /var/lib/vonk-forge/supervisor/current/vonk-agent pair \
  --enrollment https://<ENROLLMENT_HOSTNAME>:8443/ \
  --ca-sha256 <64_LOWERCASE_HEX_FROM_SHA256SUM> \
  --token-stdin < /run/secrets/vonk-enrollment-token
```

Start each supervisor only after its second pairing succeeds. Verify both new
`spk_…` identities and fresh inventories, create new administrator/inference
token files, then run physical acceptance with new evidence paths:

```bash
scripts/accept-recipe \
  --recipe config/recipes/deepseek-v4-flash-0731-ds4-single.json \
  --nodes spark-3542 \
  --ssh-target spark-3542=vonk-node-1 \
  --level spark \
  --evidence-file "$PWD/.state/recipe-acceptance/fresh-ds4.json"

scripts/accept-recipe \
  --recipe config/recipes/deepseek-v4-flash-0731-mia-dual.json \
  --nodes spark-3542,spark-2297 \
  --ssh-target spark-3542=vonk-node-1 \
  --ssh-target spark-2297=vonk-node-2 \
  --level spark \
  --evidence-file "$PWD/.state/recipe-acceptance/fresh-mia.json"
```

At Mia rank checkpoints, keep the agent running and execute only the emitted
label-verified exact rank-container stop/start action. At restart checkpoints,
perform the documented offline Spark reboot and NAS project stop/start, then
rerun the identical acceptance command. Do not claim physical acceptance until
both private evidence files say `spark-accepted` after cleanup.

## Concerns before external execution

- Steps 6–8 are unexecuted and remain the controller's responsibility.
- Independent review must accept commit
  `41e8f7735bd19362f50cb5a27353beb2fd420262` before reset.
- The controller should resolve or explicitly waive the workstation's unrelated
  Python-agent `os.memfd_create`/legacy-suite failure under its separate cleanup
  plan. No Task 9 workaround was added.
- Because the detached full-control run did not retain its terminal summary,
  the controller should use its existing captured result or run exactly one
  controlled full suite later; this worker did not start a third run.
- Confirm `/volume1/vonk-reset-state` is operator-owned, mode `0700`, outside
  the permanent project, and archive the journal plus immutable snapshot before
  removing reset state.
