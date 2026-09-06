# Retired: Vonk WorkloadRun Import and Runtime Implementation Plan

> This historical plan is retained for provenance only. The WorkloadRun
> authoring/import surface was retired during the schema-2 catalog cutover;
> its routes, modules, fixtures, and UI callers are no longer active.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import broad WorkloadRun recipe profiles into safe local Vonk drafts with exhaustive field-level reports, then resolve supported vLLM, SGLang, and llama.cpp profiles into typed immutable recipe revisions.

**Architecture:** A strict YAML parser produces a bounded neutral source model without executing templates. Runtime-specific compilers recognize allowlisted command grammar and translate it into typed recipe fields. Every source field receives one import disposition; unresolved image, artifact, resource, privilege, or topology requirements keep the local draft non-runnable.

**Tech Stack:** Python 3.12, PyYAML safe loader or strictyaml-compatible bounded parser, Pydantic, jsonschema, httpx, OCI Distribution API, pytest, React/TypeScript.

## Global Constraints

- Import never executes shell, Jinja, command substitution, installer, mod, hook, or container content.
- YAML aliases, recursive anchors, duplicate keys, custom tags, non-string environment keys, and documents larger than 256 KiB are rejected.
- Each parsed source path receives exactly one of: `imported`, `transformed`, `resolution_required`, `overlay_required`, `unsupported_blocking`, `dropped_redundant`.
- Raw source is redacted before persistence; secret-like values are neither persisted nor returned.
- A runnable revision requires an immutable OCI digest with `linux/arm64`, immutable artifact revisions, positive bounded disk/memory values, declared privilege shape, and supported topology.
- Unsupported import remains a visible editable draft with precise remediation; it never silently degrades to a different runtime or node count.
- Runtime compilers emit argument arrays, never shell strings.

---

### Task 1: Bounded WorkloadRun YAML parser

**Files:**
- Add dependency: `PyYAML==6.0.3` in `control/pyproject.toml`
- Create: `control/src/vonk_control/workload_run_source.py`
- Create: `control/tests/test_workload_run_source.py`
- Create: `control/tests/fixtures/workload_run/minimal-vllm.yaml`
- Create: `control/tests/fixtures/workload_run/full-sglang.yaml`
- Create: `control/tests/fixtures/workload_run/llama-cpp.yaml`
- Create: `control/tests/fixtures/workload_run/malicious.yaml`

**Interfaces:**
- Produces: `parse_workload_run_yaml(raw: bytes) -> WorkloadRunSource`
- Produces frozen types: `WorkloadRunSource`, `WorkloadRunDefaults`, `WorkloadRunMetadata`

- [ ] **Step 1: Write failing parser tests**

```python
def test_minimal_recipe_parses_without_executing_command(fixture_bytes) -> None:
    source = parse_workload_run_yaml(fixture_bytes("minimal-vllm.yaml"))
    assert source.model == "Qwen/Qwen3-1.7B"
    assert source.runtime == "vllm"
    assert source.command.raw.startswith("vllm serve")


@pytest.mark.parametrize("body", [b"!!python/object:os.system ['id']", b"a: &a [*a]"])
def test_unsafe_yaml_is_rejected(body: bytes) -> None:
    with pytest.raises(WorkloadRunParseError):
        parse_workload_run_yaml(body)
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project control pytest control/tests/test_workload_run_source.py -v`

Expected: FAIL because `workload_run_source` is absent.

- [ ] **Step 3: Implement duplicate-key and node-count-limited safe loading**

Subclass `yaml.SafeLoader` to reject duplicate mapping keys and all unknown tags. Count scalar/sequence/mapping nodes with a hard maximum of 4096, alias references with a maximum of 16, nesting depth 32, strings 64 KiB, and total input 256 KiB. Parse exactly one document.

- [ ] **Step 4: Validate the neutral source shape**

Accept documented WorkloadRun fields `recipe_version`, `model`, `model_revision`, `runtime`, `container`, `min_nodes`, `max_nodes`, `metadata`, `defaults`, `env`, `command`, `mods`, `tuning`, and `benchmark`. Preserve unknown fields as bounded `UnknownField(path, value_type)` records rather than dropping them.

- [ ] **Step 5: Verify parser and rejection corpus**

Run: `uv run --project control pytest control/tests/test_workload_run_source.py -v`

Expected: PASS for valid runtime fixtures and rejection of tag execution, duplicate keys, recursive aliases, multiple documents, oversize input, excessive depth, and secret-shaped environment values.

- [ ] **Step 6: Commit parser**

```bash
git add control/pyproject.toml control/uv.lock control/src/vonk_control/workload_run_source.py control/tests
git commit -m "feat: parse WorkloadRun recipes safely"
```

### Task 2: Exhaustive import-report engine

**Files:**
- Create: `control/src/vonk_control/import_report.py`
- Create: `control/src/vonk_control/workload_run_importer.py`
- Create: `control/tests/test_import_report.py`
- Create: `control/tests/test_workload_run_importer.py`

**Interfaces:**
- Produces enum: `ImportDisposition`
- Produces: `ImportReportItem(source_path, disposition, destination_path, reason_code, detail, blocking)`
- Produces: `WorkloadRunImportResult(draft_document, report, source_sha256, redacted_source)`
- Produces: `import_workload_run(source: WorkloadRunSource) -> WorkloadRunImportResult`

- [ ] **Step 1: Write failing coverage-accounting test**

```python
def test_every_source_leaf_has_exactly_one_report_item(full_source) -> None:
    result = import_workload_run(full_source)
    assert sorted(item.source_path for item in result.report) == sorted(full_source.leaf_paths())
    assert len({item.source_path for item in result.report}) == len(result.report)
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project control pytest control/tests/test_import_report.py control/tests/test_workload_run_importer.py -v`

Expected: FAIL because importer/report modules do not exist.

- [ ] **Step 3: Implement one-disposition accounting**

The report builder registers every source leaf before translation, requires exactly one terminal disposition, rejects duplicate destination writes, and raises an internal error if finalize sees an unaccounted leaf. Sort output by JSON Pointer source path.

- [ ] **Step 4: Implement base mappings**

Map model, exact model revision, runtime, immutable container reference, metadata, node bounds, environment names, and recognized defaults. Mark mutable image tags and absent artifact revision `resolution_required`; resource/topology fields absent from WorkloadRun become explicit `overlay_required`; raw command is delegated to runtime compiler; mods/installers become `unsupported_blocking` unless a compiler proves an exactly redundant behavior.

- [ ] **Step 5: Implement redaction**

Keep source field names and types, but replace secret-like values with `"<redacted>"` before database persistence. The source hash is computed over original bytes in memory and the original bytes are released after import; API responses contain the hash and redacted structure only.

- [ ] **Step 6: Verify all dispositions**

Run: `uv run --project control pytest control/tests/test_import_report.py control/tests/test_workload_run_importer.py -v`

Expected: PASS with at least one fixture for each disposition, no unreported field, no secret value, and deterministic report ordering/hash.

- [ ] **Step 7: Commit report engine**

```bash
git add control/src/vonk_control/import_report.py control/src/vonk_control/workload_run_importer.py control/tests
git commit -m "feat: report WorkloadRun import transformations"
```

### Task 3: Typed vLLM compiler

**Files:**
- Create: `control/src/vonk_control/runtime_compilers/__init__.py`
- Create: `control/src/vonk_control/runtime_compilers/common.py`
- Create: `control/src/vonk_control/runtime_compilers/vllm.py`
- Create: `control/tests/test_vllm_compiler.py`
- Add fixtures: `control/tests/fixtures/workload_run/vllm-*.yaml`

**Interfaces:**
- Produces: `compile_vllm(source: WorkloadRunSource, report: ImportReportBuilder) -> RuntimeProjection`
- Produces: `RuntimeProjection(family, arguments, environment, endpoint, transformed_paths)`

- [ ] **Step 1: Write failing command-translation test**

```python
def test_vllm_command_becomes_typed_arguments(parsed_vllm, report_builder) -> None:
    projection = compile_vllm(parsed_vllm, report_builder)
    assert projection.family == "vllm"
    assert projection.arguments == ["--max-model-len", "32768", "--gpu-memory-utilization", "0.8", "--tensor-parallel-size", "2"]
    assert all(";" not in value for value in projection.arguments)
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project control pytest control/tests/test_vllm_compiler.py -v`

Expected: FAIL because compiler is absent.

- [ ] **Step 3: Implement a grammar, not a shell parser**

Tokenize only line continuations, whitespace, single/double quoted scalar values, `{placeholder}` references to declared defaults, and allowlisted vLLM flags. Require the executable sequence `vllm serve {model}`. Reject pipes, redirects, semicolons, `&&`, command substitution, variable expansion, subshells, unknown executables, repeated singleton flags, and undeclared placeholders.

- [ ] **Step 4: Normalize aliases and defaults**

Normalize `-tp` and `--tensor-parallel-size` to one typed field; map host/port to endpoint; remove container-owned `vllm serve` executable from emitted arguments; convert booleans to presence flags; retain approved environment variables with literal non-secret values. `--trust-remote-code` becomes a visible security capability requiring local policy, not a silently accepted flag.

- [ ] **Step 5: Verify representative and adversarial profiles**

Run: `uv run --project control pytest control/tests/test_vllm_compiler.py -v`

Expected: PASS for single/two-node, quantization, tool-calling, context, KV, and served-name fixtures; reject shell operators, unknown flags, duplicate ports, undeclared defaults, and unsafe environment.

- [ ] **Step 6: Commit vLLM compiler**

```bash
git add control/src/vonk_control/runtime_compilers control/tests
git commit -m "feat: compile WorkloadRun vLLM profiles"
```

### Task 4: Typed SGLang and llama.cpp compilers

**Files:**
- Create: `control/src/vonk_control/runtime_compilers/sglang.py`
- Create: `control/src/vonk_control/runtime_compilers/llama_cpp.py`
- Create: `control/tests/test_sglang_compiler.py`
- Create: `control/tests/test_llama_cpp_compiler.py`
- Add fixtures: `control/tests/fixtures/workload_run/sglang-*.yaml`
- Add fixtures: `control/tests/fixtures/workload_run/llama-cpp-*.yaml`

**Interfaces:**
- Produces: `compile_sglang(...) -> RuntimeProjection`
- Produces: `compile_llama_cpp(...) -> RuntimeProjection`

- [ ] **Step 1: Write failing SGLang and llama.cpp tests**

Assert SGLang normalizes tensor parallel, host, port, context, quantization, and distributed initialization into typed fields. Assert llama.cpp maps GGUF artifact, GPU layers, context, parallel slots, host, and port, while defaulting `max_nodes` to one unless the recipe explicitly uses a supported bounded RPC topology.

- [ ] **Step 2: Verify RED**

Run: `uv run --project control pytest control/tests/test_sglang_compiler.py control/tests/test_llama_cpp_compiler.py -v`

Expected: FAIL because compilers are absent.

- [ ] **Step 3: Implement SGLang grammar and capability mapping**

Require the documented server executable form, normalize allowlisted flags, reject shell syntax, and emit a gang topology overlay requirement when tensor parallel exceeds one. Distributed peer addresses remain controller-generated and cannot come from recipe command text.

- [ ] **Step 4: Implement llama.cpp grammar and GGUF mapping**

Require an immutable GGUF artifact reference, represent RPC as a distinct experimental capability, and block recipes that claim generic multi-node behavior without declared rank/fabric semantics. Runtime server arguments remain an array.

- [ ] **Step 5: Verify both compilers**

Run: `uv run --project control pytest control/tests/test_sglang_compiler.py control/tests/test_llama_cpp_compiler.py -v`

Expected: PASS for valid profiles and the same adversarial shell/placeholder corpus as vLLM.

- [ ] **Step 6: Commit runtime compilers**

```bash
git add control/src/vonk_control/runtime_compilers control/tests
git commit -m "feat: compile SGLang and llama.cpp profiles"
```

### Task 5: OCI and model identity resolution

**Files:**
- Create: `control/src/vonk_control/registry_resolution.py`
- Create: `control/src/vonk_control/model_resolution.py`
- Create: `control/src/vonk_control/import_resolution.py`
- Create: `control/tests/test_registry_resolution.py`
- Create: `control/tests/test_model_resolution.py`
- Create: `control/tests/test_import_resolution.py`

**Interfaces:**
- Produces: `resolve_public_image(reference: str, transport: RegistryTransport) -> ResolvedImage`
- Produces: `resolve_huggingface_snapshot(repository: str, revision: str, transport: ModelTransport) -> ResolvedSnapshot`
- Produces: `resolve_import(result, overlays, transports) -> ResolutionResult`

- [ ] **Step 1: Write failing multi-architecture image test**

```python
def test_image_resolution_selects_linux_arm64_manifest(registry_transport) -> None:
    image = resolve_public_image("ghcr.io/acme/vllm:1.0", registry_transport)
    assert image.reference.endswith("@sha256:" + "a" * 64)
    assert image.platform == "linux/arm64"
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project control pytest control/tests/test_registry_resolution.py control/tests/test_model_resolution.py -v`

Expected: FAIL because resolvers are absent.

- [ ] **Step 3: Implement SSRF-safe OCI resolution**

Allow HTTPS registry origins only, resolve DNS and reject loopback/private/link-local/multicast/metadata ranges for global references, cap redirects at three within policy, cap manifest/index body at 2 MiB, validate media types, select exactly `linux/arm64`, and retain the registry-supplied digest only after canonical digest verification where bytes are available.

- [ ] **Step 4: Implement immutable model resolution**

Require a full immutable provider revision, enumerate bounded file metadata, record expected logical bytes, identify tokenizer/auxiliary files, and return retryable stable failures for rate limit/outage. Never download full weights in the control service.

- [ ] **Step 5: Complete overlay and runnable decision**

Overlays supply missing positive disk/memory envelope, declared topology/ranks/fabric, allowed privilege capability, and endpoint policy. Resolution is runnable only when no blocking, resolution-required, or overlay-required report item remains.

- [ ] **Step 6: Verify outage, mutation, and bounds**

Run: `uv run --project control pytest control/tests/test_registry_resolution.py control/tests/test_model_resolution.py control/tests/test_import_resolution.py -v`

Expected: PASS for manifest list, missing ARM64, moved tag detection, redirect rejection, oversize response, model revision mutation, provider outage, complete overlays, and unresolved blockers.

- [ ] **Step 7: Commit resolvers**

```bash
git add control/src/vonk_control/registry_resolution.py control/src/vonk_control/model_resolution.py control/src/vonk_control/import_resolution.py control/tests
git commit -m "feat: resolve imported runtime artifacts"
```

### Task 6: Persist import preview/apply and expose API

**Files:**
- Create: `control/src/vonk_control/workload_run_api.py`
- Create: `control/tests/test_workload_run_api.py`
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/src/vonk_control/catalog_service.py`

**Interfaces:**
- Produces: `POST /api/v1/catalog/imports/workload_run/preview`
- Produces: `POST /api/v1/catalog/imports/workload_run`
- Produces: `POST /api/v1/catalog/recipes/{recipe_id}/resolve-import`

- [ ] **Step 1: Write failing preview/apply separation test**

```python
def test_preview_does_not_persist_recipe(client, admin_headers, workload_run_yaml, session) -> None:
    response = client.post("/api/v1/catalog/imports/workload_run/preview", headers=admin_headers, content=workload_run_yaml)
    assert response.status_code == 200
    assert session.query(LocalRecipe).count() == 0
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project control pytest control/tests/test_workload_run_api.py -v`

Expected: FAIL with `404`.

- [ ] **Step 3: Implement bounded preview and idempotent apply**

Preview returns draft projection, source hash, exhaustive report, and `runnable: false/true`; it persists nothing. Apply requires the preview source hash and canonical report digest, creates or returns the same import by `(source_kind,source_sha256)`, stores only redacted source, and records every report item transactionally.

- [ ] **Step 4: Implement explicit resolution action**

Resolution request contains expected draft revision and typed overlays. It runs metadata-only image/model resolution, adds transformed report items, and creates an immutable resolved revision only when complete.

- [ ] **Step 5: Verify API security and idempotency**

Run: `uv run --project control pytest control/tests/test_workload_run_api.py -v`

Expected: PASS for preview non-persistence, apply idempotency, stale preview, secret redaction, admin authorization, payload limit, retryable external errors, blocked import, and successful resolution.

- [ ] **Step 6: Commit import API**

```bash
git add control/src/vonk_control/workload_run_api.py control/src/vonk_control/api.py control/src/vonk_control/catalog_service.py control/tests
git commit -m "feat: expose WorkloadRun import workflow"
```

### Task 7: Import and resolution interface

**Files:**
- Create: `control/web/src/pages/workload_run-import.tsx`
- Create: `control/web/src/pages/workload_run-import.test.tsx`
- Create: `control/web/src/components/import-report.tsx`
- Create: `control/web/src/components/import-report.test.tsx`
- Modify: `control/web/src/app.tsx`
- Modify: `control/web/src/styles.css`

**Interfaces:**
- Produces UI route: `/catalog/import/workload_run`
- Consumes Task 6 API operations

- [ ] **Step 1: Write failing report-language test**

```tsx
test("explains every omitted or blocked source field", async () => {
  render(<ImportReport items={importItemsFixture} />);
  expect(screen.getByText("Unsupported — blocks running")).toBeVisible();
  expect(screen.getByText(/mods cannot execute from a recipe/i)).toBeVisible();
  expect(screen.getByText("Resolution required")).toBeVisible();
});
```

- [ ] **Step 2: Verify RED**

Run: `npm --prefix control/web test -- --run src/components/import-report.test.tsx`

Expected: FAIL because `ImportReport` is absent.

- [ ] **Step 3: Implement paste/upload preview**

Accept one `.yaml`/`.yml` file or pasted text, display source hash and summary counts, and group report items by disposition without hiding successful transformations. Never render raw unredacted values as HTML.

- [ ] **Step 4: Implement overlays and apply**

Typed controls collect image resolution choice, resource envelope, topology, fabric, security capability acknowledgement, and model revision. Apply creates the draft; resolve remains a separate button and confirmation.

- [ ] **Step 5: Verify accessibility and build**

Run: `npm --prefix control/web test -- --run src/pages/workload_run-import.test.tsx src/components/import-report.test.tsx && npm --prefix control/web run build`

Expected: PASS for keyboard workflow, status labels not color-only, blocking summary, stale preview, provider outage, and successful resolved revision.

- [ ] **Step 6: Commit import UI**

```bash
git add control/web
git commit -m "feat: add WorkloadRun import UI"
```

## Plan acceptance

Run:

```bash
uv run --project control pytest \
  control/tests/test_workload_run_source.py \
  control/tests/test_import_report.py \
  control/tests/test_workload_run_importer.py \
  control/tests/test_vllm_compiler.py \
  control/tests/test_sglang_compiler.py \
  control/tests/test_llama_cpp_compiler.py \
  control/tests/test_registry_resolution.py \
  control/tests/test_model_resolution.py \
  control/tests/test_import_resolution.py \
  control/tests/test_workload_run_api.py -q
npm --prefix control/web test -- --run src/pages/workload_run-import.test.tsx src/components/import-report.test.tsx
npm --prefix control/web run build
```

Acceptance additionally imports a representative census from every configured WorkloadRun registry class and emits a machine-readable coverage report by runtime and disposition. No imported source may become runnable while any leaf is unreported or any blocking/resolution/overlay item remains.
