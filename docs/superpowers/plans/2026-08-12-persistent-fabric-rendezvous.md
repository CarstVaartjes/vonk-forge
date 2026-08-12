# Persistent Fabric Rendezvous Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep rank 0's direct-fabric coordinator available for same-container rank-1 recovery while bounding every incomplete client.

**Architecture:** Preserve the existing Bash/BusyBox HELLO protocol. Apply the validated timeout to the Bash connection read and leave the `nc -lk -e` listener unbounded until the rank-0 wrapper terminates it.

**Tech Stack:** Bash, BusyBox `nc`, Python/pytest socket integration tests, canonical recipe source-bundle generation.

## Global Constraints

- Do not change the rendezvous protocol, ports, address bindings, container privileges, or DS4 process model.
- Per-client input remains bounded by `VONK_FABRIC_RENDEZVOUS_SECONDS` in the inclusive range 1–300.
- Rank 0 owns the coordinator for exactly the lifetime of its DS4 wrapper.
- Physical validation uses accepted GitHub artifacts and preserves model/image caches.

---

### Task 1: Persistent listener with bounded clients

**Files:**
- Modify: `scripts/tests/test_qualify_development_model.py`
- Modify: `config/recipes/development/model-smoke-context/fabric-rendezvous`
- Regenerate: `config/recipes/development/model-smoke.json`

**Interfaces:**
- Consumes: `VONK_FABRIC_RENDEZVOUS_SECONDS`, the existing `vonk-fabric-v1` HELLO/ack, and BusyBox `nc -lk -e`.
- Produces: a listener that survives idle periods and a handler that fails an incomplete read within the declared timeout.

- [ ] **Step 1: Write the failing tests**

Teach `_socketbox` to apply `-w` to listener `accept()` when supplied and make `-w` optional. Add one test that idles a one-second coordinator for longer than one second before joining, and one that holds handler stdin open and requires bounded failure.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --project control --frozen pytest \
  scripts/tests/test_qualify_development_model.py \
  -k 'rendezvous or model_pair' -q
```

Expected: the idle coordinator test fails because the current listener exits;
the stalled handler test exceeds its timeout because the current `read` is
unbounded.

- [ ] **Step 3: Implement the minimal timeout relocation**

In the server branch, replace the unbounded read with:

```bash
IFS= read -r -t "${timeout_seconds}" worker_message ||
  fail "worker sent no complete data"
```

Remove `-w "${timeout_seconds}"` only from the `nc -n -lk ... -e` coordinator
invocation. Do not change client `nc -w 2` behavior.

- [ ] **Step 4: Regenerate the canonical recipe context identity**

Print the canonical generated bundle identity with the same production helper
used by the controller:

```bash
uv run --project control --frozen python - <<'PY'
from pathlib import Path
from vonk_control.source_bundles import generate_source_bundle

root = Path("config/recipes/development/model-smoke-context")
files = {
    path.relative_to(root).as_posix(): path.read_bytes()
    for path in sorted(root.rglob("*"))
    if path.is_file()
}
bundle = generate_source_bundle(files)
print(bundle.sha256, len(bundle.archive))
PY
```

Update only `build.context.sha256` and `build.context.expected_bytes` in
`model-smoke.json` to those two printed literals. The source-bundle test then
proves they match production generation.

- [ ] **Step 5: Verify GREEN and broader contracts**

Run:

```bash
uv run --project control --frozen pytest \
  scripts/tests/test_qualify_development_model.py -q
uv run --project control --frozen pytest scripts/tests -q
git diff --check
```

Expected: all pass with no rendezvous timeout or canonical-source mismatch.

- [ ] **Step 6: Commit**

```bash
git add config/recipes/development/model-smoke-context/fabric-rendezvous \
  config/recipes/development/model-smoke.json \
  scripts/tests/test_qualify_development_model.py
git commit -m "fix(recipe): keep fabric rendezvous available"
```

### Task 2: Accepted publication and physical recovery proof

**Files:**
- Update after acceptance: `docs/audits/development-agent-workload-acceptance.md`
- Retain privately: `.state/development-acceptance/model-multinode-physical-20260812.json`

**Interfaces:**
- Consumes: the accepted `main` source, GitHub development image/package channels, both enrolled Sparks, and the existing private qualification/evidence files.
- Produces: public redacted acceptance status and private full lifecycle evidence.

- [ ] **Step 1: Run full relevant checks, request independent review, and merge a PR only after required CI is green.**
- [ ] **Step 2: Redeploy the accepted NAS `:dev` cohort if its control image changed; preserve all named volumes and secrets.**
- [ ] **Step 3: Resume the physical multi-node runner with `--timeout-seconds 1800 --stop-after inference-ok`; require a new source bundle/image identity and no model redownload.**
- [ ] **Step 4: Verify firewall/address bindings, stop only rank 1's exact managed container, and resume through `route-withdrawn-after-failure`.**
- [ ] **Step 5: Start that same rank-1 container and resume through `inference-recovered`.**
- [ ] **Step 6: Restart both supervisors and the NAS project in ordered stop/start form, then resume without `--stop-after` through normal stop, route withdrawal, and uninstall.**
- [ ] **Step 7: Update the redacted audit with exact accepted public commit/workflow identities and verified outcomes; never commit private evidence, tokens, keys, model contents, or host-specific secret output.**
