# Contract handoff audit — 8 September 2026

The Pydantic chain exists, but the cutover is incomplete. Several consumers
still interpret independently defined documents or reconstruct a narrower
document after successful canonical validation. Generated schema checks do
not establish that these consumers preserve runtime meaning.

Reviewed platform `dd3ded373780b79fe2b74b393ef2f89915f25b27` and recipe checkout
`d55389b5cc144d0483cf89b95770198c6125cb15`. Three parallel audits covered
Controller APIs/persistence, Rust agent/helper/protocol, and web/CLI/schema
generation. The parent checked acceptance tools and reproduced the adapter
failure. This was a source audit with targeted reproductions, not physical
Spark acceptance. Findings below remain open at the audited revision.

## Confirmed execution defects

### 1. Runtime telemetry reads the retired runtime document

`rust/crates/vonk-agent/src/oci.rs:1362` writes the shared
`CompiledExecutionPlan` to `run-metadata/<run>/runtime.json`.
`src/telemetry.rs:885` in the same crate reads it as a separately defined
`RuntimeTelemetryContract` (`:985`). That type requires a top-level `run_id`
which the current document does not contain. Deserialization fails and the
collector silently skips the run. Its placement and endpoint fields also
disagree with the current structure.

`rust/crates/vonk-agent/tests/telemetry.rs:136` creates the retired shape,
so the test and consumer agree with each other while disagreeing with START.
The collector also assumes a loopback scrape address; production publishes
the validated placement address and host port.

Fix: consume canonical retained runtime identity and explicitly define the
engine/metrics identity needed by telemetry in the shared contract. Merely
changing the parser cannot recover engine identity absent from the plan.
Prove production START writer → collector → correct scrape destination,
including distributed workers with no public endpoint.

### 2. Accepted job parameters never reach execution

`control/src/vonk_control/compiled_artifact_contract.py:448` exposes recipe
settings as job parameters. Initial compilation binds those settings into
argv (`harnesses/canonical.py:189`). Later job submission sends new parameter
values in `RecipeJobRunRequest` (`artifact_jobs.py:953`) without recompiling.

Rust loads the installed plan (`executor.rs:1046`), forwards the parameters
(`:1196`), and adjusts only timeout in `oci.rs::prepare_job_start`.
`oci.rs:1368` explicitly ignores `_parameters` while writing the runtime
document. `compiled_oci.rs:400` adds platform roots/timeout, not the changed
settings. A seed changed for a job cannot affect argv, environment, or a
runtime file.

Fix: bind each invocation through the authoritative Controller compiler and
carry its executable facts in the canonical signed job contract. Do not add
a second engine-argument compiler in Rust. Test a changed setting all the way
from API submission to what the actual adapter receives.

### 3. Input metadata breaks both LTX native adapters

The executor writes `/inputs/manifest.json` beside the declared input files
(`executor.rs:1154`, `oci.rs:265`). Both recipe adapters
`adapters/video/ltx23-sync-native-disk/run.py:261` and
`adapters/video/ltx2-sync-native/run.py:261` require exactly one directory entry.

Reproduced directly for both adapters: `prompt.txt` alone loads; adding the
agent's manifest filename raises “exactly one regular UTF-8 .txt prompt file
is required”. This fails before model execution.

Fix: give adapters one explicit, shared input-file/metadata contract. They
must consume declared user inputs rather than treating every directory entry
as an input. Test the real staged directory against both adapter readers.

## Confirmed failure-reporting defects

### 4. Activity discards valid agent failure evidence

`agent_protocol/src/vonk_agent_protocol/contracts.py:278` accepts reason-only
failures, summaries up to 1024 characters, and codes up to 128 characters.
`control/src/vonk_control/agent_jobs.py:1079` validates and persists them.

`operation_api.py:734` reconstructs `OperationFailureEvidence`, applies a
different code vocabulary and a 256-character summary limit, then silently
returns `None` on failure. Direct canonical-model → projection probes proved
that a reason-only failure, a 257-character summary, and
`runtime_image.transport_failed` all disappear. A short snake-case control
case retains evidence.

Fix: make the Activity contract preserve valid canonical failure meaning,
including reason-only failures. Keep sanitization, but do not erase evidence
because a second DTO imposes unrelated structural restrictions. Test actual
persisted agent results through Activity responses.

### 5. Model-cache Activity loses actionable availability failures

The cache service already validates `ModelCacheOperationResponse.failure`
as `AvailabilityOperationFailure` (`model_cache.py:3034`). The Activity
provider ignores it and reconstructs a generic failure from `last_error`
(`model_cache_api.py:623`). Semantic access/capacity codes, retry timing and
capacity evidence are lost.

Direct reproduction: `access_denied` with `open_model_access` and
`check_access_and_resume` becomes `model_cache_operation_failed` with
inspect-only recovery. Queued retry/cooldown evidence can disappear too.

Fix: compose the canonical availability failure into Activity, retaining its
recovery actions and details. Verify failed and queued-retry parity between
the cache endpoint and Activity.

## Confirmed structural drift and coverage gaps

### 6. Persisted cache failures are normalized before validation

`model_cache.py:2498` writes a separate failure dictionary;
`_canonical_failure` (`:3063`) invents missing values and clears invalid
groups before constructing the public model. Reproductions show that `{}`
becomes valid generic evidence and malformed boolean/timing/capacity fields
become false/null values. This violates strict persisted-document structure.

No normal producer of those malformed values was established. This is a
demonstrated invalid-state handling gap, not a demonstrated ordinary download
failure. Fix the persisted producer/reader to share a typed contract; reject
malformed stored structure instead of manufacturing defaults.

### 7. Active web DTOs duplicate correct generated types

`control/web/src/api/types.ts:31` excludes nullable `repair_manifest` allowed
by `AgentUpgradePreviewResponse` (`agent_api.py:428`). `types.ts:50` excludes
nullable audit `authority_revision` allowed by `AuditEventResponse`
(`operation_api.py:219`). Upgrade and audit methods bypass generated calls
(`client.ts:745`, `:803`). Current UI checks are safe, so this establishes type
drift rather than a reproduced crash.

Fix: use generated aliases and generated request methods. Remove the six
unused old DTO declarations at `types.ts:55–60`; they have no consumers.

### 8. Stream and download boundaries are outside parts of generation coverage

Fleet SSE envelopes are separately handwritten Controller dictionaries
(`fleet_stream.py:75`, `:217`) and browser types
(`control/web/src/hooks/use-fleet-stream.ts:13`). Their nested models are
canonical and present fields agree. Define shared typed event envelopes and
generate their browser types while retaining the SSE transport.

The actual OpenAPI has six empty success schemas for source-bundle, artifact,
distribution-object, TUF metadata, TUF target and job-log downloads. Five
advertise JSON despite returning tar/binary/text. TUF metadata is intentionally
a signed JSON passthrough. The component-only graph test
(`control/tests/test_api_contract_graph.py:27`) cannot detect these response
declarations. Declare their actual media types and binary/text schemas and
check path responses as well as components. Do not turn signed passthrough
bytes into a reconstructed JSON document.

## Boundaries not classified as defects

- At audit time, consistent handwritten Rust serde definitions were allowed
  by an earlier instruction. The user subsequently clarified the target:
  typify-generated wire structures from Pydantic-derived schemas. Their
  replacement is included in the implementation plan. The removed duplicate
  `Placement` has no remaining active counterpart.
- Install/start raw JSON is immediately parsed and validated through the
  canonical compiled model before runtime use; that alone is not a bypass.
- No additional unconstrained fixed JSON component properties or untyped array
  items were found in the live application schema scan.
- No concrete field loss was found in the audited Python CLI mappings. The
  generator's recursive-JSON accommodation preserves values and the CLI still
  validates against the exact bundled schema.
- Controller receipt plans and executable launch plans serve different
  purposes despite sharing a class name. A conversion between distinct
  documents is legitimate; it needs connected behavioral coverage.
- Publication proof validation in `scripts/spark_lifecycle_contract.py` is
  still handwritten Python shared by emitter and signer, not Pydantic. This
  is outside HTTP API generation and no mismatch was established here.

## Completion criteria

Fix the three execution defects first, then failure reporting, then remove
client duplication and close stream/download coverage. Each regression must
consume the actual producer's output, including persisted files and staged
directories. Two matching handwritten fixtures do not prove the handoff.
Retain strict structure and security validation; do not introduce legacy
readers, fabricated defaults or competing engine argument implementations.

The existing integrated Linux agent suite passed 260 tests before this audit.
That result does not invalidate these findings: the missing producer-consumer
tests are precisely why the defects survived. The connected engineering
canary is separately under investigation; signed publication and physical
Spark acceptance have not been established by this audit.
