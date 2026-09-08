# `vonkctl` Live Node Health Implementation Plan

Historical plan, not current implementation instructions. Follow
[current API contracts](../../api-contracts.md) for Pydantic/typify generation
and optional-field serialization: omit unused optional-null fields, retain
required nullable fields, and use shared canonical serialization.

**Goal:** Add a fresh, concurrent, read-only health view for both Vonk Forge GPU nodes through `vonkctl nodes status`.

**Architecture:** A standalone Bash collector reads bounded raw telemetry from one GPU node and is sent over strict SSH stdin without persistent installation. A developer-machine health service probes both nodes concurrently, validates raw output, compares fabric data with canonical inventory and accepted RDMA baselines, grades stable health states, and renders table or schema-version-1 JSON without persisting history.

**Tech Stack:** Python 3.12, Bash, pytest, JSON Schema, `concurrent.futures`, OpenSSH, `jq`, `/proc`, `/sys`, `nvidia-smi`, and `rdma`.

**Prerequisite:** Tasks 1–5 of [Model Profile Framework](2026-08-02-model-profile-framework.md), including `SshBackend.run_script` and the base `vonkctl` CLI.

**Approved design:** [`vonkctl` live node health](../specs/2026-08-02-vonkctl-node-health-design.md).

## Global Constraints

- `vonkctl status` remains local persisted controller state; `vonkctl nodes status` always probes live.
- No database, retained sample, background agent, daemon, cron job, sudo, package install, or persistent remote script.
- Probe `node2` and `node1` concurrently but render canonical `node1`, `node2` order.
- Each probe has a 10-second deadline, a 250 ms CPU sample, and a 262144-byte output cap.
- Use the checked-in collector only; send it over SSH stdin with strict host checking, `BatchMode=yes`, `IdentitiesOnly=yes`, and `ForwardAgent=no`.
- Unified memory comes from `/proc/meminfo`; never present `nvidia-smi` GPU memory as a second pool.
- Expected fabric functions come from `inventory/cluster.toml`; accepted RDMA counter baselines come from `inventory/reports/rdma-nccl.json`.
- Raw missing optional fields remain `null`; never silently convert them to zero.
- Available memory, load, utilization, and raw temperature are telemetry, not invented generic health thresholds.
- Exit 0 covers healthy/warning, exit 4 covers critical/unreachable, and exit 5 covers local collector/schema/inventory failure before probing.
- No health failure repairs, switches, publishes, drains, or mutates controller/profile state.
- Work proceeds directly on `main` by explicit user instruction.

---

### Task 1: Implement the standalone raw node collector

**Files:**
- Create: `nodes/bin/collect-health`
- Create: `schemas/node-health-raw.schema.json`
- Create: `src/cluster_profiles/schemas/node-health-raw.schema.json`
- Modify: `pyproject.toml`
- Create: `tests/nodes/test_collect_health.py`
- Create: `tests/fixtures/node-health/healthy/`

**Interfaces:**
- Produces: `collect-health --json --cpu-sample-ms 250 --interface <name> --hca <name>`.
- Emits one raw JSON object; it does not assign final health.
- When sourced for tests, functions accept explicit proc/sys roots; normal execution always uses `/proc` and `/sys`.

- [x] **Step 1: Write failing raw-collector tests**

Write tests before the collector exists:

```python
def test_collector_reports_cpu_unified_memory_and_root(run_collector):
    result = run_collector("healthy")
    assert result["cpu"]["logical_processors"] == 20
    assert result["memory"]["total_bytes"] == 130663231488
    assert result["root_filesystem"]["read_only"] is False


def test_unsupported_power_is_null_not_zero(run_collector):
    result = run_collector("healthy", gpu_power="N/A")
    assert result["accelerator"]["available"] is True
    assert result["accelerator"]["power_watts"] is None


def test_requested_fabric_mapping_is_retained(run_collector):
    result = run_collector("healthy")
    assert [item["interface"] for item in result["fabric"]["functions"]] == [
        "enp1s0f1np1",
        "enP2p1s0f1np1",
    ]


def test_raw_output_satisfies_schema(run_collector, raw_schema):
    jsonschema.validate(run_collector("healthy"), raw_schema)
```

The fixture provides deterministic `/proc/stat`, `/proc/meminfo`,
`/proc/loadavg`, uptime/boot ID, root filesystem command output, NVIDIA
output, thermal zones/trip points, interface/HCA sysfs, RDMA links/counters,
Docker version, and `earlyoom` state.

- [x] **Step 2: Run the collector tests and confirm the intended failure**

Run:

```bash
uv run --no-project --with pytest --with jsonschema \
  pytest tests/nodes/test_collect_health.py -v
```

Expected: FAIL because `nodes/bin/collect-health` and its schema are absent.

- [x] **Step 3: Implement strict bounded raw collection**

Use `set -euo pipefail`, exact argument parsing, and `jq -n`. Reject unknown,
missing, duplicate-mismatched, or odd interface/HCA arguments with exit 2.
Collect:

- hostname, boot ID, uptime, and UTC capture time;
- two-sample CPU utilization, logical processor count, and 1/5/15 load;
- total/available/used memory and swap bytes;
- root total/available/used bytes, percentage, and read-only state;
- NVIDIA query availability, GB10 name, driver, utilization, temperature,
  performance state, and nullable power draw;
- readable thermal zones with enabled trip types/temperatures and reached state;
- each requested interface/HCA operstate, carrier, speed, MTU, RDMA state, and
  the same named error counters used by `validate-fabric`; and
- Docker query availability/version plus `earlyoom` load/enabled/active state.

The script never grades, enumerates processes/containers, invokes sudo, writes
files, or reads arbitrary paths during normal execution. Guard `main` with
`[[ "${BASH_SOURCE[0]}" == "$0" ]]` so fixture tests can source pure helpers.

- [x] **Step 4: Verify raw collection**

Run:

```bash
uv run --no-project --with pytest --with jsonschema \
  pytest tests/nodes/test_collect_health.py -v
bash -n nodes/bin/collect-health
git diff --check
```

Expected: all collector/schema tests pass and the script parses cleanly.

- [x] **Step 5: Commit the collector**

```bash
git add nodes/bin/collect-health schemas/node-health-raw.schema.json \
  src/cluster_profiles/schemas/node-health-raw.schema.json pyproject.toml \
  tests/nodes/test_collect_health.py tests/fixtures/node-health
git commit -m "feat: collect live GPU node telemetry"
```

### Task 2: Implement concurrent health evaluation and CLI output

**Files:**
- Create: `src/cluster_profiles/health.py`
- Create: `schemas/node-health.schema.json`
- Create: `src/cluster_profiles/schemas/node-health.schema.json`
- Modify: `pyproject.toml`
- Modify: `src/cluster_profiles/cli.py`
- Modify: `config/controller.toml`
- Create: `tests/cluster_profiles/test_health.py`
- Modify: `tests/cluster_profiles/test_cli.py`
- Create: `docs/runbooks/node-health.md`
- Modify: `docs/model-profile-overview.md`

**Interfaces:**
- Produces: `NodeHealthService.collect() -> ClusterHealth`.
- Adds: `vonkctl nodes status [--json]`.
- Emits the exact schema-version-1 envelope from the approved design.

- [x] **Step 1: Write failing concurrency, grading, and CLI tests**

```python
def test_node_probes_overlap(service, probe_barrier):
    result = service.collect()
    assert probe_barrier.parties == 2
    assert tuple(result.nodes) == ("node1", "node2")


def test_active_or_enabled_earlyoom_is_critical(service):
    service.raw("node2")["services"]["earlyoom_active"] = True
    result = service.collect()
    assert result.nodes["node2"].status == "critical"
    assert "earlyoom_active" in result.nodes["node2"].errors


def test_available_memory_is_not_a_generic_health_failure(service):
    service.raw("node1")["memory"]["available_bytes"] = 1
    result = service.collect()
    assert result.nodes["node1"].memory.available_bytes == 1
    assert "memory_low" not in result.nodes["node1"].errors


def test_rdma_counter_is_compared_with_accepted_baseline(service):
    service.rdma_baseline("node1", "rocep1s0f1", "packet_seq_err", 2)
    service.raw_counter("node1", "rocep1s0f1", "packet_seq_err", 3)
    result = service.collect()
    assert "rdma_counter_above_baseline" in result.nodes["node1"].errors


def test_unreachable_node_preserves_other_live_result(cli):
    cli.fail_ssh("node2")
    result = cli("nodes", "status", "--json")
    assert result.exit_code == 4
    assert result.json["nodes"]["node1"]["status"] == "healthy"
    assert result.json["nodes"]["node2"]["status"] == "unreachable"
```

Also cover malformed/truncated/timeout/nonzero remote output, root read-only,
NVIDIA unavailable, fabric speed/MTU/HCA mismatch, thermal trip, swap warning,
disk warning, optional null fields, deterministic error ordering, schema
validation, table columns, and exit codes 0/4/5.

- [x] **Step 2: Run health and CLI tests and confirm failure**

```bash
uv run --no-project --with pytest --with jsonschema \
  pytest tests/cluster_profiles/test_health.py \
         tests/cluster_profiles/test_cli.py -v
```

Expected: FAIL because `cluster_profiles.health` and `nodes status` are absent.

- [x] **Step 3: Implement concurrent probing and fail-closed evaluation**

Use a two-worker `ThreadPoolExecutor`. Read the fixed collector bytes locally,
construct inventory-pinned repeated interface/HCA arguments, and call
`SshBackend.run_script` for both nodes. Validate raw JSON before evaluation.
Return canonical node order irrespective of future completion order.

Implement the approved states and codes:

- `unreachable`: SSH connection/authentication failure;
- `critical`: collector timeout/malformed/truncated/nonzero, root read-only,
  NVIDIA unavailable, `earlyoom` enabled/active, fabric mismatch/down,
  RDMA inactive/mismatched, error counter above accepted baseline, or hardware
  critical thermal trip;
- `warning`: swap above 1 GiB, root free below 150 GiB, whole optional
  telemetry source missing, or enabled passive/hot thermal trip; and
- `healthy`: none of the above.

Unsupported individual optional fields remain null without warning. Overall is
healthy only when both nodes are healthy, warning when neither is
critical/unreachable and at least one warns, otherwise critical. Errors and
warnings are stable sorted codes. Percentages round to one decimal; integer
telemetry remains integer.

Add exactly:

```toml
[health]
timeout_seconds = 10
cpu_sample_milliseconds = 250
max_output_bytes = 262144
collector = "nodes/bin/collect-health"
inventory = "inventory/cluster.toml"
rdma_baseline = "inventory/reports/rdma-nccl.json"
```

- [x] **Step 4: Implement human/JSON CLI and run the live smoke test**

The table columns are `NODE`, `STATE`, `CPU`, `LOAD1`,
`MEM AVAILABLE`, `SWAP USED`, `ROOT FREE`, `GPU`, `TEMP`, `FABRIC`,
and `UPTIME`, followed by stable warnings/errors. JSON validates against
`node-health.schema.json`. Use exit 0 for healthy/warning, 4 for any
critical/unreachable, and 5 for local collector/schema/inventory failure before
probing.

After offline tests pass, run:

```bash
uv run vonkctl nodes status --json > /tmp/node-node-health.json
jq -e '
  .schema_version == 1 and
  (.nodes | keys == ["node1", "node2"]) and
  all(.nodes[]; .status == "healthy" or .status == "warning")
' /tmp/node-node-health.json
```

Do not lower a rule when the live result is critical. Preserve the result and
diagnose the exact mismatch.

- [x] **Step 5: Verify, document, and commit**

Update the visual overview's implementation-state table truthfully; do not mark
any model runtime accepted. Document fields, null semantics, exit codes, and
troubleshooting.

Run:

```bash
uv run --no-project --with pytest --with jsonschema pytest tests -v
bash -n nodes/bin/collect-health
python3 -m compileall -q src
git diff --check
```

Verify every changed local Markdown link resolves, then:

```bash
git add src/cluster_profiles/health.py src/cluster_profiles/cli.py \
  src/cluster_profiles/schemas schemas/node-health.schema.json pyproject.toml \
  config/controller.toml tests/cluster_profiles docs/runbooks/node-health.md \
  docs/model-profile-overview.md
git commit -m "feat: report live GPU node cluster health"
```

## Completion gate

The feature is complete only when both tasks pass task review, the full offline
suite passes, the live probe returns a truthful bounded result for both aliases,
and the final whole-tree review has no unresolved load-bearing finding. Health
completion does not install or accept a model runtime.
