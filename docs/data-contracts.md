# Data contracts: one definition per data class

Owner rule: every data class has **one** definition, a Pydantic contract model.
Python, Rust and TypeScript either use that model or code generated from it. A
hand-written copy in another language is a defect, not a convenience.

Three guards hold the rule (`tools/contract_scans.py`, tested by
`tests/test_data_contract_guards.py`). Each runs against a reviewed allowlist
under `tools/` that only shrinks: an unlisted copy fails, and a listed entry
that no longer occurs fails as stale.

| Guard | Scans | Allowlist |
| --- | --- | --- |
| Rust | `struct`/`enum` with a serde derive or impl outside `generated.rs` | `tools/serde-derive-allowlist.json` |
| TypeScript | `type X = {` and `interface X {` in `control/web/src` outside generated files and tests | `tools/ts-shapes-allowlist.json` |
| Python | Pydantic model classes outside the registered modules; two models sharing a name, or sharing bases and fields | `tools/python-model-registry.json` |

## Where each class lives

| Data class | Source model | Rust | TypeScript | Python consumers |
| --- | --- | --- | --- | --- |
| Controller to Spark messages (claims, directives, results, progress, inventory, telemetry, enrollment, recipe jobs and builds, host-helper grants) | `agent_protocol/src/vonk_agent_protocol/*.py` | `vonk-agent-protocol` `generated.rs`, from `schema/wire.json` (`scripts/generate-agent-wire`) | none | Controller (the models themselves) |
| Agent client decisions, transport classifications and OCI diagnostic vocabulary | `agent_words.py`, published by `LifecycleVocabulary` | `generated.rs` | vocabulary generator | diagnostic consumers |
| Compiled launch plan | `compiled_execution_plan.py` in `agent_protocol` | `generated.rs` | none | Controller, tests |
| Files the Spark agent and helper persist (identity pointers, readiness receipt, reconciliation checkpoints and receipts, installation metadata, run lifecycle, runtime image receipts, generation and installation intent fences, package rollback transaction) | `agent_protocol/.../agent_state.py` | `generated.rs` (`AgentGenerationPointer`, `AgentReadinessReceipt`, `RunLifecycleRecord`, `HostRuntimeImageReceipt`, ...) | none | none yet; the models document the files |
| Installer documents (NAS install template, Spark site ports, Spark setup apply frame) | `agent_protocol/.../installer_setup.py` | `generated.rs` (`NasInstallTemplate`, `SitePorts`, `SparkApplyEnvelope`, ...) | none | `scripts/build-nas-compose-bundle` validates its payload with the model; the Controller reads `site-ports.json` through `SitePorts` |
| CLI release projection | `installer_release.py` | `src/cluster_profiles/schemas/cli-release-projection.schema.json` | none | CLI self-updater |
| Installer release manifest | `installer_release.py` | `generated.rs`; `schemas/install-release-manifest.schema.json` | none | publisher scripts |
| Controller API requests and responses | `control/src/vonk_control/*_contract.py` and the registered API modules below | none (the agent does not call these) | `control/web/src/api/generated.d.ts` from `control/openapi.json` | `src/cluster_profiles/generated_control` (CLI client) |
| Prometheus metric catalogue, series with summaries, and alert attention | `control/src/vonk_control/metrics/contract.py`; external Prometheus replies in `metrics/prometheus_api.py` | none | generated Controller API types | generated Controller client |
| Lifecycle and reason-code vocabulary | `lifecycle_vocabulary.py`, `reason_codes/`, `state_machines.py` | `generated.rs` | `control/web/src/api/vocabulary.generated.ts` | the CLI words in `src/cluster_profiles/cli_states_generated.py` |
| Progress phase and wait words (plain words for a person) | `vonk_agent_protocol/phase_words.py` | none | `control/web/src/api/vocabulary.generated.ts` (`PHASE_WORDS`, `WAIT_WORDS`) | `src/cluster_profiles/cli_states_generated.py` |
| CLI state words | the vocabulary above | none | none | `src/cluster_profiles/cli_states_generated.py` (`scripts/generate-python-vocabulary`) |
| Route activation marker | `route_activation.py`; its state words from `GatewayRouteState` | none | none | `route_activation_words.py` (generated), loaded beside `route_activation.py` by the LiteLLM supervisor |
| CLI token download | `CliTokenDownload` in `auth_api.py` | none | `components["schemas"]["CliTokenDownload"]` | none |
| Published Model and Recipe | `vonk_forge_contracts` in the recipes checkout | none | `ModelDefinition`, `RecipeDefinition` in `generated.d.ts` | catalog, compiler |
| Database rows | SQLAlchemy models, `control/src/vonk_control/models/` | none | none | Controller |

Regenerate everything with `scripts/generate-agent-wire` (wire, installer
release schema, `generated.rs`), `scripts/generate-control-clients` (OpenAPI,
Python and TypeScript clients, TypeScript vocabulary) and
`scripts/generate-python-vocabulary` (CLI words, route activation words).
Client and wire outputs are ignored and generated by their build/test entry
points. `scripts/check-generated-determinism` compares two client generation
runs; `--wire` checks wire generation. `tests/test_generated_contracts.py`
compares generated consumers and the remaining checked-in schemas with their
canonical producers. See [the fresh checkout command](testing-and-ci.md#fresh-checkout-build-and-contract-smoke-checks).

## Rust types that stay hand-written

Each is listed in `tools/serde-derive-allowlist.json` with its reason. The rest
of the 50 hand-written serde types of the baseline are generated now.

| File | Type | Reason |
| --- | --- | --- |
| `rust/crates/vonk-agent-protocol/examples/recipe_job_wire_probe.rs` | `BridgeInput` | test harness: stdin envelope of the Python wire-bridge test; the contract types inside it (AgentClaim, AgentResult) are the generated ones |
| `rust/crates/vonk-agent-protocol/tests/recipe_builds.rs` | `SharedVectorCase` | test harness: the shared-vector file layout of one integration test, not product data |
| `rust/crates/vonk-agent-protocol/tests/recipe_builds.rs` | `SharedVectorChange` | test harness: the shared-vector file layout of one integration test, not product data |
| `rust/crates/vonk-agent-protocol/tests/recipe_builds.rs` | `SharedVectors` | test harness: the shared-vector file layout of one integration test, not product data |
| `rust/crates/vonk-agent/examples/recipe_observation_wire_probe.rs` | `PersistBindingInput` | test harness: stdin envelope of the Python wire-bridge test; the contract types inside it are the generated ones |
| `rust/crates/vonk-agent/examples/recipe_observation_wire_probe.rs` | `SerializeInput` | test harness: stdin envelope of the Python wire-bridge test; the contract types inside it are the generated ones |
| `rust/crates/vonk-agent/examples/restart_recovery_probe.rs` | `Request` | test harness: stdin request of the restart-recovery probe, not product data |
| `rust/crates/vonk-agent/examples/acceptance_certificate_renewal.rs` | `Evidence` | test harness: stdout provenance of the separately built hosted certificate-renewal acceptance peer, never installed or read as a production wire document; records the real native rotation clock, exact agent binary/build identity, certificate/key changes and fixed thirty-day lifetimes |
| `rust/crates/vonk-agent/src/base_images.rs` | `Descriptor` | external format: the OCI image layout, manifest, index and config documents defined by the OCI image specification, read from registry content |
| `rust/crates/vonk-agent/src/base_images.rs` | `ImageConfig` | external format: the OCI image layout, manifest, index and config documents defined by the OCI image specification, read from registry content |
| `rust/crates/vonk-agent/src/base_images.rs` | `Index` | external format: the OCI image layout, manifest, index and config documents defined by the OCI image specification, read from registry content |
| `rust/crates/vonk-agent/src/base_images.rs` | `Manifest` | external format: the OCI image layout, manifest, index and config documents defined by the OCI image specification, read from registry content |
| `rust/crates/vonk-agent/src/base_images.rs` | `OciLayout` | external format: the OCI image layout, manifest, index and config documents defined by the OCI image specification, read from registry content |
| `rust/crates/vonk-agent/src/config.rs` | `AgentConfig` | operator-editable site TOML that must ignore keys a newer or older release dropped; the generated readers are JSON-only and reject unknown keys, so the file is not a wire document |
| `rust/crates/vonk-spark-setup/src/config_files.rs` | `WrittenConfig` | TOML agent configuration written by this setup program and read back for validation; TOML is not a generated JSON wire document, and the agent's tolerant reader of the same file is allowlisted for the same reason |
| `rust/crates/vonk-spark-setup/src/firewall.rs` | `AddressInfo` | iproute2/rdma JSON output parsed from a host command: a third-party tool's schema, not a Vonk wire contract |
| `rust/crates/vonk-spark-setup/src/firewall.rs` | `InterfaceEntry` | iproute2/rdma JSON output parsed from a host command: a third-party tool's schema, not a Vonk wire contract |
| `rust/crates/vonk-spark-setup/src/firewall.rs` | `NeighbourEntry` | iproute2/rdma JSON output parsed from a host command: a third-party tool's schema, not a Vonk wire contract |
| `rust/crates/vonk-spark-setup/src/firewall.rs` | `RdmaLink` | iproute2/rdma JSON output parsed from a host command: a third-party tool's schema, not a Vonk wire contract |
| `rust/crates/vonk-spark-setup/src/firewall.rs` | `RouteEntry` | iproute2/rdma JSON output parsed from a host command: a third-party tool's schema, not a Vonk wire contract |

## TypeScript shapes that stay hand-written

Every API data type is an alias of `components["schemas"][...]`. The shapes
below are UI state, component props, or client method signatures; each is
listed in `tools/ts-shapes-allowlist.json` with its reason.

| File | Type | Reason |
| --- | --- | --- |
| `control/web/src/api/fleet-event-connection.ts` | `FleetEventStream` | Browser-local event connection handle with listener/disposal methods and its route URL; never serialized as API data. Event payloads use the generated canonical route schema. |
| `control/web/src/api/types.ts` | `ActivityFilters` | UI-only state or form draft; never serialized to the API |
| `control/web/src/api/types.ts` | `ArtifactTransferProgress` | browser-local upload hashing progress and worker messages; not API data |
| `control/web/src/api/types.ts` | `ControlApi` | method-signature interface over the generated schema types (client seam for tests); declares no data fields |
| `control/web/src/api/types.ts` | `LibraryApi` | method-signature interface over the generated schema types (client seam for tests); declares no data fields |
| `control/web/src/api/types.ts` | `LibraryViewModel` | UI-only join of the independent Model and Recipe library responses, never sent to the API; its documents are typed by generated schemas |
| `control/web/src/api/types.ts` | `LibraryViewPlacementGroup` | UI-only join of the independent Model and Recipe library responses, never sent to the API; its documents are typed by generated schemas |
| `control/web/src/api/types.ts` | `LibraryViewRecipe` | UI-only join of the independent Model and Recipe library responses, never sent to the API; its documents are typed by generated schemas |
| `control/web/src/api/types.ts` | `LibraryViewRecipeDetail` | UI-only join of the independent Model and Recipe library responses, never sent to the API; its documents are typed by generated schemas |
| `control/web/src/api/types.ts` | `LibraryViewSnapshot` | UI-only join of the independent Model and Recipe library responses, never sent to the API; its documents are typed by generated schemas |
| `control/web/src/auth.tsx` | `AuthContextValue` | UI-only state or form draft; never serialized to the API |
| `control/web/src/auth.tsx` | `BrowserAuthApi` | method-signature interface over the generated schema types (client seam for tests); declares no data fields |
| `control/web/src/components/ui/admin-menu.tsx` | `AdminMenuProps` | component props: UI-only, not API data |
| `control/web/src/components/ui/app-shell.tsx` | `AppShellProps` | component props: UI-only, not API data |
| `control/web/src/components/ui/app-shell.tsx` | `Operator` | UI-only view model derived from generated API types for one screen; not an API document |
| `control/web/src/components/artifact-job/artifact-hash.ts` | `ArtifactHashProgress` | browser-local upload hashing progress and worker messages; not API data |
| `control/web/src/components/artifact-job/artifact-hash.ts` | `HashWorker` | browser-local upload hashing progress and worker messages; not API data |
| `control/web/src/components/artifact-job/artifact-hash.ts` | `WorkerMessage` | browser-local upload hashing progress and worker messages; not API data |
| `control/web/src/components/artifact-job/artifact-hash.worker.ts` | `HashRequest` | browser-local upload hashing progress and worker messages; not API data |
| `control/web/src/components/artifact-job/artifact-hash.worker.ts` | `WorkerScope` | browser-local upload hashing progress and worker messages; not API data |
| `control/web/src/components/artifact-job/artifact-job-workspace.tsx` | `ActiveRun` | UI-only view model derived from generated API types for one screen; not an API document |
| `control/web/src/components/artifact-job/artifact-job-submission.tsx` | `CancelRecovery` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/artifact-job/artifact-job-submission.tsx` | `InputPayload` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/artifact-job/artifact-job-form.tsx` | `RecipeParameter` | UI-only view model derived from generated API types for one screen; not an API document |
| `control/web/src/components/ui/confirm-dialog.tsx` | `ConfirmDialogProps` | component props: UI-only, not API data |
| `control/web/src/components/ui/empty-state.tsx` | `EmptyStateProps` | component props: UI-only, not API data |
| `control/web/src/components/library/library-availability-feedback.tsx` | `AvailabilityFailure` | UI-only view model parsed defensively from an untyped error or progress payload; not an API document |
| `control/web/src/components/library/library-availability-progress.tsx` | `AvailabilityProgress` | UI-only view model parsed defensively from an untyped error or progress payload; not an API document |
| `control/web/src/components/library/library-models-view.tsx` | `LibraryModelsViewProps` | component props: UI-only, not API data |
| `control/web/src/components/library/library-profile-drafts.tsx` | `AssignmentDraft` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/library/library-profile-drafts.tsx` | `FleetEntry` | UI-only view model derived from generated API types for one screen; not an API document |
| `control/web/src/components/library/library-profile-load.tsx` | `PendingProfileLoad` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/library/library-profile-drafts.tsx` | `ProfileDraft` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/library/library-recipe-files.ts` | `SelectedRecipeFiles` | UI-only view model parsed defensively from an untyped error or progress payload; not an API document |
| `control/web/src/components/library/library-technical-details.tsx` | `TechnicalValue` | UI-only view model parsed defensively from an untyped error or progress payload; not an API document |
| `control/web/src/components/library/library-workcell.tsx` | `LibraryRecipeRecord` | UI-only view model derived from generated API types for one screen; not an API document |
| `control/web/src/components/library/library-workcell.tsx` | `LibraryWorkcellFilters` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/ui/sort-header.tsx` | `Sort` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/ui/toast.tsx` | `ToastItem` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/ui/toast.tsx` | `Toasts` | UI-only state or form draft; never serialized to the API |
| `control/web/src/hooks/fleet-stream-state.ts` | `FleetStreamState` | UI-only state or form draft; never serialized to the API |
| `control/web/src/hooks/use-operation-observer.ts` | `Options` | UI-only state or form draft; never serialized to the API |
| `control/web/src/pages/activity-status.tsx` | `ActivitySummary` | UI-only view model derived from generated API types for one screen; not an API document |
| `control/web/src/pages/login.tsx` | `LoginFailure` | UI-only view model parsed defensively from an untyped error or progress payload; not an API document |

## Python contract modules

A Pydantic model for wire or persisted data lives in `agent_protocol` or in a
module of `tools/python-model-registry.json`. `kind` is `agent-protocol`,
`controller-contract` (a `*_contract.py` module), or `declared` (an API module
that colocates its route models with their owner, with its reason in the
registry). The controller base class is `StrictModel` in `strict_json.py`; no
module defines its own.

| Module | Kind | Models | Purpose |
| --- | --- | --- | --- |
| `agent_protocol/src/vonk_agent_protocol/agent_operations.py` | agent-protocol | 5 | Agent operation kinds and the result bodies the outcome envelope shares. |
| `agent_protocol/src/vonk_agent_protocol/agent_state.py` | agent-protocol | 20 | Records the Rust agent and its privileged helper persist beside their work. |
| `agent_protocol/src/vonk_agent_protocol/build_import.py` | agent-protocol | 13 | Typed recipe build request/result wire contracts. |
| `agent_protocol/src/vonk_agent_protocol/claims.py` | agent-protocol | 2 | Canonical current-agent identity and work-claim request. |
| `agent_protocol/src/vonk_agent_protocol/compiled_execution_plan.py` | agent-protocol | 18 | Typed compiled launch plan shared by Controller and agents. |
| `agent_protocol/src/vonk_agent_protocol/contracts.py` | agent-protocol | 7 | Agent claim, progress, directive and result envelopes and their operation registries. |
| `agent_protocol/src/vonk_agent_protocol/distribution.py` | agent-protocol | 2 | Pydantic contracts for authenticated Controller-to-Spark distribution. |
| `agent_protocol/src/vonk_agent_protocol/enrollment.py` | agent-protocol | 8 | Canonical enrollment and certificate-rotation JSON messages. |
| `agent_protocol/src/vonk_agent_protocol/failure_evidence.py` | agent-protocol | 5 | Current bounded failure diagnostics shared by agent and Controller. |
| `agent_protocol/src/vonk_agent_protocol/helper_response.py` | agent-protocol | 3 | Current framed Unix-socket response from the privileged host helper. |
| `agent_protocol/src/vonk_agent_protocol/host_helper.py` | agent-protocol | 11 | Canonical authorization protocol for the narrow root host helper. |
| `agent_protocol/src/vonk_agent_protocol/http_failure.py` | agent-protocol | 1 | Shared typed temporary HTTP response and bounded retry delay. |
| `agent_protocol/src/vonk_agent_protocol/installer_release.py` | agent-protocol | 14 | Complete installer publication graphs and the forward-compatible signed CLI updater projection. |
| `agent_protocol/src/vonk_agent_protocol/installer_setup.py` | agent-protocol | 22 | Documents the NAS and Spark setup programs read and exchange. |
| `agent_protocol/src/vonk_agent_protocol/inventory.py` | agent-protocol | 7 | Authenticated schema-1 inventory evidence reported by an agent. |
| `agent_protocol/src/vonk_agent_protocol/job_inputs.py` | agent-protocol | 3 | The exact input manifest shared by job staging and container adapters. |
| `agent_protocol/src/vonk_agent_protocol/job_wire.py` | agent-protocol | 1 | Constrained values and the base model the recipe-job wire models share. |
| `agent_protocol/src/vonk_agent_protocol/lifecycle_vocabulary.py` | agent-protocol | 1 | The lifecycle and outcome vocabulary shared by Python, Rust and TypeScript. |
| `agent_protocol/src/vonk_agent_protocol/optional_evidence.py` | agent-protocol | 2 | Mixin that drops invalid optional agent evidence and reports it as a typed warning instead of refusing the report. |
| `agent_protocol/src/vonk_agent_protocol/outcome.py` | agent-protocol | 9 | The one outcome envelope of an agent operation result. |
| `agent_protocol/src/vonk_agent_protocol/package_source.py` | agent-protocol | 1 | Immutable publication lookup for an exact installed agent binary. |
| `agent_protocol/src/vonk_agent_protocol/package_upgrade.py` | agent-protocol | 3 | Exact source-bound rollback authority for the current package transaction. |
| `agent_protocol/src/vonk_agent_protocol/reason_codes/vocabulary.py` | agent-protocol | 1 | Closed reason, blocker, warning and attention codes shared by Python, Rust and TypeScript. |
| `agent_protocol/src/vonk_agent_protocol/recipe_jobs.py` | agent-protocol | 9 | Closed Pydantic protocol for one-shot artifact-producing recipe jobs. |
| `agent_protocol/src/vonk_agent_protocol/recipe_observations.py` | agent-protocol | 2 | Recipe run observation report sent by the mTLS-authenticated agent. |
| `agent_protocol/src/vonk_agent_protocol/recipe_operations.py` | agent-protocol | 11 | Closed declarative protocol for digest-bound recipe lifecycle work. |
| `agent_protocol/src/vonk_agent_protocol/route_activation.py` | agent-protocol | 3 | Canonical schema-2 activation receipt shared with the LiteLLM supervisor. |
| `agent_protocol/src/vonk_agent_protocol/runtime_preflight.py` | agent-protocol | 4 | Current recipe runtime preflight wire contract; no raw host diagnostics. |
| `agent_protocol/src/vonk_agent_protocol/runtime_scan.py` | agent-protocol | 3 | Node-local durable traversal checkpoint for managed run observation; native filesystem witnesses remain authoritative. |
| `agent_protocol/src/vonk_agent_protocol/source_bundles.py` | agent-protocol | 3 | Canonical source-bundle digest document and verified storage metadata. |
| `agent_protocol/src/vonk_agent_protocol/telemetry.py` | agent-protocol | 8 | The authenticated agent telemetry wire contract: a flat sample of host scalars. |
| `agent_protocol/src/vonk_agent_protocol/wire_model.py` | agent-protocol | 6 | Shared strict JSON boundary helpers for Pydantic wire models. |
| `control/src/vonk_control/agent_api/common.py` | declared | 3 | mTLS-authenticated machine agent API routes. |
| `control/src/vonk_control/agent_upgrades/contract.py` | controller-contract | 5 | Package, request intent, plan and result documents of an agent upgrade rollout. |
| `control/src/vonk_control/jobs/agent_contract.py` | controller-contract | 1 | The bounded facts the agent job queue renders into a refused-claim note. |
| `control/src/vonk_control/artifact_jobs/blob_store.py` | declared | 2 | Usage and reconciliation reports of the artifact blob store. |
| `control/src/vonk_control/artifact_jobs/api.py` | declared | 3 | Authenticated controller and mTLS agent routes for artifact recipe jobs. |
| `control/src/vonk_control/artifact_jobs/evidence.py` | controller-contract | 2 | The one closed result-evidence contract of an artifact job. |
| `control/src/vonk_control/artifact_jobs/contracts.py` | declared | 10 | Durable, bounded, content-addressed artifact-producing recipe jobs. |
| `control/src/vonk_control/artifact_jobs/job_contract.py` | declared | 1 | The compiled job contract of a recipe revision, read before a job exists. |
| `control/src/vonk_control/artifact_jobs/maintenance.py` | declared | 1 | Typed reports of artifact maintenance sweeps. |
| `control/src/vonk_control/auth_api.py` | declared | 4 | Strict HTTP boundary for durable browser authentication. |
| `control/src/vonk_control/pki/issuance_contract.py` | controller-contract | 1 | Exact accepted CA issuance binding and the typed refusal reason. |
| `control/src/vonk_control/cache_removal_review.py` | declared | 6 | Canonical review contract shared by cache-removal owners and clients. |
| `control/src/vonk_control/catalog/api.py` | declared | 3 | Strict authenticated HTTP surface for the local database recipe catalog. |
| `control/src/vonk_control/catalog/revision_contract.py` | controller-contract | 9 | Typed readers and writers for immutable catalog revision JSON columns. |
| `control/src/vonk_control/catalog/sync_contract.py` | controller-contract | 5 | Canonical sync request identity, trigger and reviewed content plus durable synchronization evidence shared with the API. |
| `control/src/vonk_control/cli_update_contract.py` | controller-contract | 1 | Authenticated CLI compatibility observation; its canonical schema is generated into the installed updater package. |
| `control/src/vonk_control/cluster_mappings.py` | declared | 2 | The identity document whose digest binds a cluster mapping plan to its exact placement. |
| `control/src/vonk_control/compiled_artifact_contract.py` | controller-contract | 12 | Canonical compiled contract for artifact-producing recipe jobs. |
| `control/src/vonk_control/execution_plan/compiled.py` | declared | 7 | Verified Controller receipts for the compiled Spark execution plan. |
| `control/src/vonk_control/distribution/assignment_contract.py` | declared | 1 | The Controller's record of one node's artifact distribution grant. |
| `control/src/vonk_control/endpoint_contract.py` | controller-contract | 1 | Secret-free projection of one published recipe endpoint. |
| `control/src/vonk_control/enrollment/contract.py` | controller-contract | 7 | Enrollment status, grants, exact issuance/rotation claims and local/CA revocation confirmation. |
| `control/src/vonk_control/failures/evidence.py` | declared | 5 | Failure diagnostics rendered on request from durable failure rows. |
| `control/src/vonk_control/fleet_events/contract.py` | controller-contract | 9 | Strict payload contracts for the durable Fleet outbox. |
| `control/src/vonk_control/fleet_profile_contract/endpoints.py` | controller-contract | 3 | Fleet profile contract: endpoints. |
| `control/src/vonk_control/fleet_profile_contract/definitions.py` | controller-contract | 19 | Fleet profile contract: definitions. |
| `control/src/vonk_control/fleet_profile_contract/assessment.py` | controller-contract | 11 | Fleet profile contract: assessment. |
| `control/src/vonk_control/fleet_profile_contract/effects.py` | controller-contract | 8 | Fleet profile contract: effects. |
| `control/src/vonk_control/fleet_profile_contract/switch_state.py` | controller-contract | 9 | Fleet profile contract: switch state. |
| `control/src/vonk_control/fleet_profile_contract/applications.py` | controller-contract | 8 | Fleet profile contract: applications. |
| `control/src/vonk_control/fleet_profile_contract/review.py` | controller-contract | 9 | Fleet profile contract: review. |
| `control/src/vonk_control/fleet_profiles/contracts.py` | controller-contract | 1 | Saved profile content identity used by admission and projections. |
| `control/src/vonk_control/fleet_projection/common.py` | declared | 14 | Complete typed projection of PostgreSQL-authoritative Fleet state, transferred through bounded immutable observation records. |
| `control/src/vonk_control/fleet_events/stream_contract.py` | controller-contract | 14 | Typed JSON envelopes emitted by the Fleet Server-Sent Events stream. |
| `control/src/vonk_control/gateway/keys.py` | declared | 18 | Inference gateway client keys: LiteLLM virtual keys managed by the Controller. |
| `control/src/vonk_control/harnesses/canonical_metadata.py` | declared | 1 | Strict platform metadata for built-in canonical harnesses. |
| `control/src/vonk_control/jobs/documents.py` | controller-contract | 45 | Typed documents of the generic job queue bound to the job columns; the kind-agnostic remainder is a declared passthrough. |
| `control/src/vonk_control/library/contract.py` | controller-contract | 45 | Bounded typed contract and deterministic display helpers for Library reads. |
| `control/src/vonk_control/lifecycle_preflight_contract.py` | controller-contract | 1 | The durable checkpoint of a recipe-bound lifecycle admission probe round. |
| `control/src/vonk_control/gateway/litellm.py` | declared | 13 | The LiteLLM configuration the Controller renders from published routes (our document, LiteLLM's file format). |
| `control/src/vonk_control/model_cache/input_contracts.py` | controller-contract | 3 | Typed catalog artifact locators and explicit fixture ingress. |
| `control/src/vonk_control/model_cache/provider_contracts.py` | declared | 10 | Durable, content-addressed model artifacts stored on the Controller NAS. |
| `control/src/vonk_control/model_cache/contract.py` | controller-contract | 53 | Schema-2 contracts for the Controller-owned NAS model cache. |
| `control/src/vonk_control/observation/transfer.py` | controller-contract | 5 | Canonical immutable observation transfer records; sequence, identity, byte count and final digest bind the original complete Fleet or platform payload. |
| `control/src/vonk_control/operation_api/contracts.py` | declared | 17 | Strict, secret-free representations for routine administrative operations. |
| `control/src/vonk_control/operations/blockers.py` | declared | 8 | One typed answer to "what is this operation waiting for?". |
| `control/src/vonk_control/operations/contract.py` | controller-contract | 4 | Current nested contracts for durable Controller operations and progress. |
| `control/src/vonk_control/operations/item_contract.py` | controller-contract | 3 | One operation of any family as Activity projects it: the typed item, its owner and the failure facts of its stored result. |
| `control/src/vonk_control/operator_projection_api/contracts.py` | declared | 10 | Operator projection api: contracts. |
| `control/src/vonk_control/observation/platform.py` | controller-contract | 4 | API and worker process provenance observations; unavailable producer facts remain nullable. |
| `control/src/vonk_control/preparation_contract.py` | controller-contract | 10 | Shared schema-2 truth for Controller-owned rollout preparation. |
| `control/src/vonk_control/fleet_profiles/stop_contract.py` | controller-contract | 8 | Typed ownership for profile-authorized one-shot JobRun cleanup Stops. |
| `control/src/vonk_control/recipe_operations/availability_intent.py` | declared | 4 | Original requests, distinct from resolved preparation and worker effects. |
| `control/src/vonk_control/recipe_operations/execution_contract.py` | controller-contract | 10 | Strict persisted contracts for recipe execution and source builds. |
| `control/src/vonk_control/recipe_image_availability/api.py` | declared | 3 | Authenticated, typed HTTP routes for Recipe Make available. |
| `control/src/vonk_control/recipe_image_availability/availability_contract.py` | controller-contract | 2 | Shared recipe-image state, artifact and builder receipt identity. |
| `control/src/vonk_control/recipe_image_availability/clocks_contract.py` | controller-contract | 5 | Tolerant typed lifecycle clock projection independent of job-document imports. |
| `control/src/vonk_control/recipe_image_availability/reader_contract.py` | controller-contract | 4 | Typed adoption and exact-catalog recovery of damaged availability bookkeeping. |
| `control/src/vonk_control/recipe_image_availability/view_contract.py` | controller-contract | 2 | Typed availability and removal read projections with canonical JSON egress. |
| `control/src/vonk_control/recipe_image_availability/removal_contract.py` | controller-contract | 8 | The current persisted intent for one recipe cache removal request. |
| `control/src/vonk_control/recipe_image_availability/response_contract.py` | controller-contract | 5 | Wire documents of recipe image availability requests and their operator answers. |
| `control/src/vonk_control/recipe_operations/lifecycle_contract.py` | controller-contract | 9 | Typed response contracts for recipe lifecycle operations. |
| `control/src/vonk_control/recipe_operations/contracts.py` | controller-contract | 2 | Typed recipe operation context and installation disposal receipt. |
| `control/src/vonk_control/recipe_packages/contracts.py` | declared | 2 | Schema-2 recipe package reader for the Controller catalog sync. |
| `control/src/vonk_control/recipe_update_batches/contract.py` | controller-contract | 8 | One current contract for durable recipe-update intent and observation. |
| `control/src/vonk_control/recipe_update_batches/notice.py` | declared | 1 | One owner for "a newer revision of this recipe exists" (never restarts anything). |
| `control/src/vonk_control/resource_planning/contract.py` | controller-contract | 2 | Canonical nested recipe topology and resource settings read projections. |
| `control/src/vonk_control/gateway/route_bundle_contract.py` | controller-contract | 8 | The published route bundle (`routes.json`) and the identity document whose digest names a candidate bundle. |
| `control/src/vonk_control/gateway/keys_contract.py` | controller-contract | 3 | Wire documents of gateway client keys. |
| `control/src/vonk_control/gateway/external_contract.py` | controller-contract | 9 | An external model: the operator's input, the accepted non-secret spec, its destination, credential bindings and rendered gateway entry. |
| `control/src/vonk_control/gateway/external_api_contract.py` | controller-contract | 17 | The external model and credential binding routes: views, reviews, accept requests and what an accepted review digests. |
| `control/src/vonk_control/gateway/credential_custody.py` | controller-contract | 1 | The names of the secrets the LiteLLM supervisor loaded (`/supervisor/credentials.json`); never a value. |
| `control/src/vonk_control/gateway/litellm_surface.py` | controller-contract | 1 | What the pinned LiteLLM accepts (`deploy/compose/litellm/surface.json`), generated from the pinned image. |
| `control/src/vonk_control/run_switch_contract/requests.py` | controller-contract | 15 | Run switch contract: requests. |
| `control/src/vonk_control/run_switch_contract/evidence.py` | controller-contract | 16 | Run switch contract: evidence. |
| `control/src/vonk_control/run_switch_contract/plans.py` | controller-contract | 2 | Run switch contract: plans. |
| `control/src/vonk_control/run_switch_contract/progress.py` | controller-contract | 6 | Run switch contract: progress. |
| `control/src/vonk_control/run_switch_contract/phase_results.py` | controller-contract | 20 | Run switch contract: phase results. |
| `control/src/vonk_control/run_switch_contract/operations.py` | controller-contract | 6 | Run switch contract: operations. |
| `control/src/vonk_control/run_switch_contract/identity.py` | controller-contract | 1 | Shared Run/Switch request identity constraints and typed cancellation intent independent of ORM and workers. |
| `control/src/vonk_control/run_switch_contract/journal.py` | controller-contract | 4 | Typed run-switch journal repair evidence and audit records. |
| `control/src/vonk_control/run_switch_contract/observation.py` | controller-contract | 8 | Typed observed progress, retained lifecycle identity, artifact guards and build receipts. |
| `control/src/vonk_control/runtime_image_preparation/contracts.py` | declared | 3 | Controller-owned preparation of exact runtime image archives. |
| `control/src/vonk_control/runtime/spec_contract.py` | controller-contract | 18 | The compiled runtime specification the recipe compiler produces and the launch plan projects. |
| `control/src/vonk_control/stored/documents.py` | controller-contract | 2 | Typed documents stored in plan, run and installation JSON columns. |
| `control/src/vonk_control/strict_json.py` | declared | 1 | Controller JSON models and their field-presence serialization policy. |
| `control/src/vonk_control/worker/memory_contract.py` | controller-contract | 5 | Typed memory report the worker publishes and the Controller API exports. |
| `agent_protocol/src/vonk_agent_protocol/capability_reply.py` | agent-protocol | 1 | The reply a route gives while an independently recoverable capability is down. |
| `agent_protocol/src/vonk_agent_protocol/display_memory.py` | agent-protocol | 2 | Observed display reservation; unknown is never a memory estimate. |
| `agent_protocol/src/vonk_agent_protocol/error_response.py` | agent-protocol | 5 | The bounded error envelope every Controller route can answer with. |
| `agent_protocol/src/vonk_agent_protocol/gpu_clock.py` | agent-protocol | 7 | GPU clock intent, hardware observations and the owner's safety policy. |
| `agent_protocol/src/vonk_agent_protocol/grant_requests.py` | agent-protocol | 6 | The Controller's host-helper and package grant requests, and the grant reply. |
| `agent_protocol/src/vonk_agent_protocol/host_tuning.py` | agent-protocol | 5 | Spark host tuning as the agent measures it: the facts behind the host checks. |
| `agent_protocol/src/vonk_agent_protocol/page_cache.py` | agent-protocol | 4 | Page-cache drops the privileged helper performs while a workload loads. |
| `agent_protocol/src/vonk_agent_protocol/run_inspect_target.py` | agent-protocol | 7 | What one read-only run inspection asks the root helper about. |
| `agent_protocol/src/vonk_agent_protocol/store_cleanup.py` | agent-protocol | 3 | Local retention delegation; no arbitrary privileged filesystem paths. |
| `agent_protocol/src/vonk_agent_protocol/swap.py` | agent-protocol | 4 | Spark swap size: the owner's policy, the desired intent and what the helper did. |
| `tools/vonkctl_json_contract.py` | controller-contract | 13 | The `vonkctl --json` client contract: one model per published document. |
| `control/src/vonk_control/fleet_projection/host_checks.py` | declared | 1 | Spark host checks: plain advice derived from the agent's measured host tuning. |
| `control/src/vonk_control/metrics/contract.py` | controller-contract | 7 | Shared operator contracts for bounded Prometheus reads. |
| `control/src/vonk_control/metrics/prometheus_api.py` | declared | 9 | Read-only fixed-query adapter to the internal Prometheus service. |
| `control/src/vonk_control/run_switch_operations/contracts.py` | controller-contract | 1 | Typed records owned by Run/Switch orchestration. |
| `control/src/vonk_control/source_bundle_contract.py` | controller-contract | 1 | Durable source-ingress proof bound to an exact managed file copy. |
| `control/src/vonk_control/waits/contract.py` | controller-contract | 2 | One persisted wait observation; the owner's SQL due column owns its clock. |
| `control/src/vonk_control/capability_contract.py` | controller-contract | 1 | Availability of independently recoverable Controller capabilities. |
| `control/src/vonk_control/runtime/asset_contract.py` | controller-contract | 1 | Complete public-kit membership produced while assembling the Controller image. |
| `control/src/vonk_control/operator_acceptance/receipt_contract.py` | controller-contract | 4 | Canonical public operator acceptance references and existing-Job decisions. |
| `control/src/vonk_control/model_cache/fleet_removal_contract.py` | controller-contract | 6 | Reviewed, content-addressed effects of a request to remove a fleet model. |
| `control/src/vonk_control/failures/coordinator_contract.py` | controller-contract | 2 | Failures of worker coordinators that have not claimed an operation. |
| `tools/client_compatibility_report.py` | declared | 8 | Report what a published vonkctl would refuse from this Controller. |
| `tools/contract_scans.py` | declared | 3 | Static scans that keep every data class defined once, in its Pydantic contract. |
| `tools/single_source_values.py` | declared | 3 | Owner-adjacent registrations for handwritten literal families, without debt allowances. |
| `control/src/vonk_control/inventory/items_contract.py` | controller-contract | 2 | Typed current agent facts retained as inventory items, never command history. |
| `control/src/vonk_control/repeated_evidence_contract.py` | controller-contract | 1 | Repeated observations owned by contact or the operation they describe. |
| `control/src/vonk_control/inventory/repository.py` | controller-contract | 2 | Append-only authenticated GPU node inventory evidence. |
| `control/src/vonk_control/telemetry/samples.py` | controller-contract | 1 | Bounded, ordered persistence for authenticated node telemetry. |

`stored/json_values.py` and `stored/columns.py` hold no wire models (`stored/rows.py` loads both for row reads and the write guard): they bind every JSON column of `models.py` to its typed contract (36 of 36), and `tests/test_json_column_contracts.py` fails on an untyped level. The only open values are the declared `ExternalPassthrough` annotations (engine argument values, generic job queue documents), each with a reason.

## Counts

| | Before | After |
| --- | --- | --- |
| Rust hand-written serde types outside `generated.rs` (src) | 50 in 13 files | 11 in 6 files, all external formats or tolerant site files |
| Rust serde types in examples and tests (harness inputs) | 7 | 8, allowlisted |
| TypeScript hand-written API data shapes | 1 named by the owner (`CliTokenDownload`), plus an inline body type and a blocker shape | 0; 44 UI-only shapes allowlisted with reasons |
| Python duplicate model groups (same name or same bases and fields) | 12 | 0 |
| Python copies of contract words | 2 (`cli_states.py`, `route_activation.py`) | 0; both read generated modules |


### Narrow Run/Switch journal repair evidence

`run_switch_contract/journal.py` owns the typed append-only evidence in
`run_switch_journal_repairs.evidence`. `Job.result` remains the sole live
Run/Switch checkpoint. The evidence stores the original SQL document's explicit
canonical text (including nulls), original/corrected digests and exact native
first-attempt sample witnesses. It is never a plan, runnable state or scheduler.
Measurement repair requires a current accepted zero-transfer plan and an exactly
reproduced original native install sample. Cancellation and expired/terminal
owner observation require the same exact accepted child/phase/payload/fence
binding; they normalize only the proven zero distribution budget and leave
phase measurements to their native owner. Historical samples are never promoted
to fresh evidence by this normalization.
Later attempts or changed samples cannot authorize measurement repair. Foreign
children/scopes or ambiguous identity defer as unknown while the raw journal
remains untouched. The immutable `run_switch_journal_repair_pending.deadline_at` column owns the
two-minute observation deadline from first discovery. The independent typed
`cancellation` column owns acknowledged cancellation. Mutable typed `progress`
projects those facts and owns capped backoff; corrupt progress is reconstructed
without changing the deadline or losing cancellation. Cancellation commits
before proof or observation.
Immutable end evidence retains that intent when the pending record is removed.
Repair and its pending-state handoff commit atomically. Expiry ends with
`run-switch.journal-repair-exhausted`, fences the accepted installation's native
attempts, removes retry/queue holds and releases its preparation reservations.
Reusable files remain; no stale observation withdraws a working route. A fresh
request is admitted immediately. Failed owners are never reopened.

Schema impact: two additive tables with Job foreign keys: append-only typed
repair/end evidence (indexed separately for each original digest) and transient
typed observation/cancel intent. Startup schema
reconciliation creates them without reset or backfill; exact-integer storage
remains unchanged. Terminal history collection deletes both with their owning
Job. PostgreSQL regressions exercise historical production, restart, exact
native continuation, cancellation, contention, bounded missing/mismatched
proof, and fresh same-Spark admission through the shared non-blocking helper.

Run/Switch orchestration records are defined in `control/src/vonk_control/run_switch_operations/contracts.py`.

Controller capability availability and retryable refusals are owned by
`control/src/vonk_control/capability_contract.py`.

`operation_api/openapi.py` passes external OpenAPI and JSON Schema documents through `ExternalSchemaDocument`, annotated with `ExternalPassthrough`; application responses remain canonical registered models.

Native renewal acceptance uses `NativeRenewalEvidence` and `RenewalHelperManifest`
from the registered `vonk_agent_protocol.agent_state` module. The source journal
is persisted before rotation; an absent replacement is an observation miss.

Gateway mutation recovery uses `gateway.keys.GatewayMutationReceipt` to bind
a request identity and durable completion to the exact `_KeyGenerateRequest`.
Completed receipts remain independent of alias history; a fresh request ID
creates a new secret and a repeated ID reuses its original receipt. The proposed alias, scope, lifetime, and secret are persisted before
LiteLLM mutation in `/gateway-secrets/mutations`, the Controller's writable,
persistent gateway secret bind mount. Records and claim files are private
(mode 0600, directory mode 0700); normalized administrative secrets remain
read-only. These records do not grant authority: every API mutation retains
its role check and every remote request authenticates with the master key.
Malformed records are misses; current requests replace them before effects.

`recipe_operations.execution_contract.RouteWithdrawalFollowUp` constrains the generated
`OperatorActionName.STOP` and `OperatorActionName.RETRY` choices for stop and
recovery follow-ups after recipe route withdrawal.

`control/src/vonk_control/runtime/asset_contract.py` owns `RuntimeAssetInventory`, the complete public-kit membership assembled into the Controller image. Staging validates every member before removing retired projections.

`jobs.documents.AvailabilityUnknownEnd` records a preparation owner ended after unreadable intent; it grants no execution or publication authority. `runtime_image_preparation/contracts.RuntimeImageReceiptObservation` distinguishes a readable damaged receipt from an unavailable read, so scans never delete a file on an unknown observation.

The Activity observation contracts expose `continuation_unavailable` separately
from a completed page, and `observation_unavailable` for detail facts that do
not fit or cannot currently be read. Known operation state and exact identity
remain readable; missing timestamp evidence is omitted. These are observation
facts and confer no mutation authority.

Catalog sync request binding belongs to PostgreSQL's
`RecipeLibrarySyncRun.reviewed_content_sha256`, independently of its disposable
result projection. This adds a nullable digest column to the Controller schema;
startup applies it under the schema reconciliation lock. This source change
requires an explicit schema merge decision and performs no database reset.
