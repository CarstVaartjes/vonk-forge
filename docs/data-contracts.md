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
| Files the Spark agent and helper persist (identity pointers, readiness receipt, reconciliation checkpoints and receipts, installation metadata, run lifecycle, runtime image receipts, generation fence, package rollback transaction) | `agent_protocol/.../agent_state.py` | `generated.rs` (`AgentGenerationPointer`, `AgentReadinessReceipt`, `RunLifecycleRecord`, `HostRuntimeImageReceipt`, ...) | none | none yet; the models document the files |
| Installer documents (NAS install template, Spark site ports, Spark setup apply frame) | `agent_protocol/.../installer_setup.py` | `generated.rs` (`NasInstallTemplate`, `SitePorts`, `SparkApplyEnvelope`, ...) | none | `scripts/build-nas-compose-bundle` validates its payload with the model; the Controller reads `site-ports.json` through `SitePorts` |
| CLI release projection | `installer_release.py` | `src/cluster_profiles/schemas/cli-release-projection.schema.json` | none | CLI self-updater |
| Installer release manifest | `installer_release.py` | `generated.rs`; `schemas/install-release-manifest.schema.json` | none | publisher scripts |
| Controller API requests and responses | `control/src/vonk_control/*_contract.py` and the registered API modules below | none (the agent does not call these) | `control/web/src/api/generated.d.ts` from `control/openapi.json` | `src/cluster_profiles/generated_control` (CLI client) |
| Lifecycle and reason-code vocabulary | `lifecycle_vocabulary.py`, `reason_codes.py`, `state_machines.py` | `generated.rs` | `control/web/src/api/vocabulary.generated.ts` | the CLI words in `src/cluster_profiles/cli_states_generated.py` |
| CLI state words | the vocabulary above | none | none | `src/cluster_profiles/cli_states_generated.py` (`scripts/generate-python-vocabulary`) |
| Route activation marker | `route_activation.py`; its state words from `GatewayRouteState` | none | none | `route_activation_words.py` (generated), loaded beside `route_activation.py` by the LiteLLM supervisor |
| CLI token download | `CliTokenDownload` in `auth_api.py` | none | `components["schemas"]["CliTokenDownload"]` | none |
| Published Model and Recipe | `vonk_forge_contracts` in the recipes checkout | none | `ModelDefinition`, `RecipeDefinition` in `generated.d.ts` | catalog, compiler |
| Database rows | SQLAlchemy models, `control/src/vonk_control/models/` | none | none | Controller |
| Global container-runtime policy | `vonk-forge-web` `schemas/`, copied to `schemas/global/` | allowlisted below (read as published) | none | none |

Regenerate everything with `scripts/generate-agent-wire` (wire, installer
release schema, `generated.rs`), `scripts/generate-control-clients` (OpenAPI,
Python and TypeScript clients, TypeScript vocabulary) and
`scripts/generate-python-vocabulary` (CLI words, route activation words).
`tests/test_generated_contracts.py` fails while any committed copy is stale.

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
| `rust/crates/vonk-agent/src/oci/mod.rs` | `RuntimePolicy` | external format: the global container-runtime policy document owned by vonk-forge-web (schemas/global, pinned by contract.lock.json); Rust reads it as published, it is not defined here |
| `rust/crates/vonk-agent/src/oci/mod.rs` | `RuntimePolicyLabel` | external format: the global container-runtime policy document owned by vonk-forge-web (schemas/global, pinned by contract.lock.json); Rust reads it as published, it is not defined here |
| `rust/crates/vonk-nas-setup/src/pki.rs` | `PrivateJwk` | external format: RFC 7517 JSON Web Key as step-ca consumes it |
| `rust/crates/vonk-nas-setup/src/pki.rs` | `PublicJwk` | external format: RFC 7517 JSON Web Key as step-ca consumes it |
| `rust/crates/vonk-spark-setup/src/config_files.rs` | `WrittenConfig` | TOML agent configuration written by this setup program and read back for validation; TOML is not a generated JSON wire document, and the agent's tolerant reader of the same file is allowlisted for the same reason |
| `rust/crates/vonk-nas-setup/src/pki.rs` | `StepCaAuthority` | step-ca's own ca.json document: a third-party tool's file layout the NAS setup reads and rewrites, not a Vonk wire contract |
| `rust/crates/vonk-nas-setup/src/pki.rs` | `StepCaAuthorityIdentity` | step-ca's own ca.json document: a third-party tool's file layout the NAS setup reads and rewrites, not a Vonk wire contract |
| `rust/crates/vonk-nas-setup/src/pki.rs` | `StepCaClaims` | step-ca's own ca.json document: a third-party tool's file layout the NAS setup reads and rewrites, not a Vonk wire contract |
| `rust/crates/vonk-nas-setup/src/pki.rs` | `StepCaConfig` | step-ca's own ca.json document: a third-party tool's file layout the NAS setup reads and rewrites, not a Vonk wire contract |
| `rust/crates/vonk-nas-setup/src/pki.rs` | `StepCaConfigIdentity` | step-ca's own ca.json document: a third-party tool's file layout the NAS setup reads and rewrites, not a Vonk wire contract |
| `rust/crates/vonk-nas-setup/src/pki.rs` | `StepCaDatabase` | step-ca's own ca.json document: a third-party tool's file layout the NAS setup reads and rewrites, not a Vonk wire contract |
| `rust/crates/vonk-nas-setup/src/pki.rs` | `StepCaLogger` | step-ca's own ca.json document: a third-party tool's file layout the NAS setup reads and rewrites, not a Vonk wire contract |
| `rust/crates/vonk-nas-setup/src/pki.rs` | `StepCaOptions` | step-ca's own ca.json document: a third-party tool's file layout the NAS setup reads and rewrites, not a Vonk wire contract |
| `rust/crates/vonk-nas-setup/src/pki.rs` | `StepCaProvisioner` | step-ca's own ca.json document: a third-party tool's file layout the NAS setup reads and rewrites, not a Vonk wire contract |
| `rust/crates/vonk-nas-setup/src/pki.rs` | `StepCaProvisionerIdentity` | step-ca's own ca.json document: a third-party tool's file layout the NAS setup reads and rewrites, not a Vonk wire contract |
| `rust/crates/vonk-nas-setup/src/pki.rs` | `StepCaRevocationList` | step-ca's own ca.json document: a third-party tool's file layout the NAS setup reads and rewrites, not a Vonk wire contract |
| `rust/crates/vonk-nas-setup/src/pki.rs` | `StepCaX509Options` | step-ca's own ca.json document: a third-party tool's file layout the NAS setup reads and rewrites, not a Vonk wire contract |
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
| `control/web/src/components/admin-menu.tsx` | `AdminMenuProps` | component props: UI-only, not API data |
| `control/web/src/components/app-shell.tsx` | `AppShellProps` | component props: UI-only, not API data |
| `control/web/src/components/app-shell.tsx` | `Operator` | UI-only view model derived from generated API types for one screen; not an API document |
| `control/web/src/components/artifact-hash.ts` | `ArtifactHashProgress` | browser-local upload hashing progress and worker messages; not API data |
| `control/web/src/components/artifact-hash.ts` | `HashWorker` | browser-local upload hashing progress and worker messages; not API data |
| `control/web/src/components/artifact-hash.ts` | `WorkerMessage` | browser-local upload hashing progress and worker messages; not API data |
| `control/web/src/components/artifact-hash.worker.ts` | `HashRequest` | browser-local upload hashing progress and worker messages; not API data |
| `control/web/src/components/artifact-hash.worker.ts` | `WorkerScope` | browser-local upload hashing progress and worker messages; not API data |
| `control/web/src/components/artifact-job-workspace.tsx` | `ActiveRun` | UI-only view model derived from generated API types for one screen; not an API document |
| `control/web/src/components/artifact-job-workspace.tsx` | `CancelRecovery` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/artifact-job-workspace.tsx` | `InputPayload` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/artifact-job-workspace.tsx` | `RecipeParameter` | UI-only view model derived from generated API types for one screen; not an API document |
| `control/web/src/components/confirm-dialog.tsx` | `ConfirmDialogProps` | component props: UI-only, not API data |
| `control/web/src/components/empty-state.tsx` | `EmptyStateProps` | component props: UI-only, not API data |
| `control/web/src/components/library-availability-feedback.tsx` | `AvailabilityFailure` | UI-only view model parsed defensively from an untyped error or progress payload; not an API document |
| `control/web/src/components/library-availability-progress.tsx` | `AvailabilityProgress` | UI-only view model parsed defensively from an untyped error or progress payload; not an API document |
| `control/web/src/components/library-models-view.tsx` | `LibraryModelsViewProps` | component props: UI-only, not API data |
| `control/web/src/components/library-profiles-view.tsx` | `AssignmentDraft` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/library-profiles-view.tsx` | `FleetEntry` | UI-only view model derived from generated API types for one screen; not an API document |
| `control/web/src/components/library-profiles-view.tsx` | `PendingProfileLoad` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/library-profiles-view.tsx` | `ProfileDraft` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/library-recipe-files.ts` | `SelectedRecipeFiles` | UI-only view model parsed defensively from an untyped error or progress payload; not an API document |
| `control/web/src/components/library-technical-details.tsx` | `TechnicalValue` | UI-only view model parsed defensively from an untyped error or progress payload; not an API document |
| `control/web/src/components/library-workcell.tsx` | `LibraryRecipeRecord` | UI-only view model derived from generated API types for one screen; not an API document |
| `control/web/src/components/library-workcell.tsx` | `LibraryWorkcellFilters` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/sort-header.tsx` | `Sort` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/toast.tsx` | `ToastItem` | UI-only state or form draft; never serialized to the API |
| `control/web/src/components/toast.tsx` | `Toasts` | UI-only state or form draft; never serialized to the API |
| `control/web/src/hooks/fleet-stream-state.ts` | `FleetStreamState` | UI-only state or form draft; never serialized to the API |
| `control/web/src/hooks/use-operation-observer.ts` | `Options` | UI-only state or form draft; never serialized to the API |
| `control/web/src/pages/activity.tsx` | `ActivitySummary` | UI-only view model derived from generated API types for one screen; not an API document |
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
| `agent_protocol/src/vonk_agent_protocol/agent_state.py` | agent-protocol | 14 | Records the Rust agent and its privileged helper persist beside their work. |
| `agent_protocol/src/vonk_agent_protocol/build_import.py` | agent-protocol | 13 | Typed recipe build request/result wire contracts. |
| `agent_protocol/src/vonk_agent_protocol/claims.py` | agent-protocol | 2 | Canonical current-agent identity and work-claim request. |
| `agent_protocol/src/vonk_agent_protocol/compiled_execution_plan.py` | agent-protocol | 18 | Typed compiled launch plan shared by Controller and agents. |
| `agent_protocol/src/vonk_agent_protocol/contracts.py` | agent-protocol | 12 |  |
| `agent_protocol/src/vonk_agent_protocol/distribution.py` | agent-protocol | 2 | Pydantic contracts for authenticated Controller-to-Spark distribution. |
| `agent_protocol/src/vonk_agent_protocol/enrollment.py` | agent-protocol | 6 | Canonical enrollment and certificate-rotation JSON messages. |
| `agent_protocol/src/vonk_agent_protocol/failure_evidence.py` | agent-protocol | 3 | Current bounded failure diagnostics shared by agent and Controller. |
| `agent_protocol/src/vonk_agent_protocol/helper_response.py` | agent-protocol | 2 | Current framed Unix-socket response from the privileged host helper. |
| `agent_protocol/src/vonk_agent_protocol/host_helper.py` | agent-protocol | 10 | Canonical authorization protocol for the narrow root host helper. |
| `agent_protocol/src/vonk_agent_protocol/installer_release.py` | agent-protocol | 14 | Complete installer publication graphs and the forward-compatible signed CLI updater projection. |
| `agent_protocol/src/vonk_agent_protocol/installer_setup.py` | agent-protocol | 22 | Documents the NAS and Spark setup programs read and exchange. |
| `agent_protocol/src/vonk_agent_protocol/inventory.py` | agent-protocol | 2 | Authenticated schema-1 inventory evidence reported by an agent. |
| `agent_protocol/src/vonk_agent_protocol/job_inputs.py` | agent-protocol | 1 | The exact input manifest shared by job staging and container adapters. |
| `agent_protocol/src/vonk_agent_protocol/lifecycle_vocabulary.py` | agent-protocol | 1 | The lifecycle and outcome vocabulary shared by Python, Rust and TypeScript. |
| `agent_protocol/src/vonk_agent_protocol/optional_evidence.py` | agent-protocol | 1 | Mixin that drops invalid optional agent evidence and reports it as a typed warning instead of refusing the report. |
| `agent_protocol/src/vonk_agent_protocol/outcome.py` | agent-protocol | 9 | The one outcome envelope of an agent operation result. |
| `agent_protocol/src/vonk_agent_protocol/package_source.py` | agent-protocol | 1 | Immutable publication lookup for an exact installed agent binary. |
| `agent_protocol/src/vonk_agent_protocol/package_upgrade.py` | agent-protocol | 3 | Exact source-bound rollback authority for the current package transaction. |
| `agent_protocol/src/vonk_agent_protocol/reason_codes.py` | agent-protocol | 1 | Closed reason, blocker, warning and attention codes shared by Python, Rust and TypeScript. |
| `agent_protocol/src/vonk_agent_protocol/recipe_jobs.py` | agent-protocol | 10 | Closed Pydantic protocol for one-shot artifact-producing recipe jobs. |
| `agent_protocol/src/vonk_agent_protocol/recipe_observations.py` | agent-protocol | 2 | Recipe run observation report sent by the mTLS-authenticated agent. |
| `agent_protocol/src/vonk_agent_protocol/recipe_operations.py` | agent-protocol | 10 | Closed declarative protocol for digest-bound recipe lifecycle work. |
| `agent_protocol/src/vonk_agent_protocol/route_activation.py` | agent-protocol | 2 | Canonical schema-2 activation receipt shared with the LiteLLM supervisor. |
| `agent_protocol/src/vonk_agent_protocol/runtime_preflight.py` | agent-protocol | 3 | Current recipe runtime preflight wire contract; no raw host diagnostics. |
| `agent_protocol/src/vonk_agent_protocol/runtime_scan.py` | agent-protocol | 3 | Node-local durable traversal checkpoint for managed run observation; native filesystem witnesses remain authoritative. |
| `agent_protocol/src/vonk_agent_protocol/source_bundles.py` | agent-protocol | 3 | Canonical source-bundle digest document and verified storage metadata. |
| `agent_protocol/src/vonk_agent_protocol/telemetry.py` | agent-protocol | 3 | The authenticated agent telemetry wire contract: a flat sample of host scalars. |
| `agent_protocol/src/vonk_agent_protocol/wire_model.py` | agent-protocol | 5 | Shared strict JSON boundary helpers for Pydantic wire models. |
| `control/src/vonk_control/agent_api.py` | declared | 8 | mTLS-authenticated machine agent API routes. |
| `control/src/vonk_control/agent_upgrade_contract.py` | controller-contract | 5 | Package, repair manifest, request intent, plan and result documents of an agent upgrade rollout. |
| `control/src/vonk_control/agent_job_contract.py` | controller-contract | 1 | The bounded facts the agent job queue renders into a refused-claim note. |
| `control/src/vonk_control/artifact_blob_store.py` | declared | 2 | Usage and reconciliation reports of the artifact blob store. |
| `control/src/vonk_control/artifact_job_api.py` | declared | 3 | Authenticated controller and mTLS agent routes for artifact recipe jobs. |
| `control/src/vonk_control/artifact_job_evidence.py` | controller-contract | 1 | The one closed result-evidence contract of an artifact job. |
| `control/src/vonk_control/artifact_jobs/contracts.py` | declared | 10 | Durable, bounded, content-addressed artifact-producing recipe jobs. |
| `control/src/vonk_control/artifact_maintenance.py` | declared | 1 | Typed reports of artifact maintenance sweeps. |
| `control/src/vonk_control/auth_api.py` | declared | 4 | Strict HTTP boundary for durable browser authentication. |
| `control/src/vonk_control/ca_issuance_contract.py` | controller-contract | 7 | Exact accepted CA issuance binding, authenticated sign/observe request, durable receipt states and typed refusals. |
| `control/src/vonk_control/cache_removal_review.py` | declared | 6 | Canonical review contract shared by cache-removal owners and clients. |
| `control/src/vonk_control/catalog_api.py` | declared | 3 | Strict authenticated HTTP surface for the local database recipe catalog. |
| `control/src/vonk_control/catalog_revision_contract.py` | controller-contract | 9 | Typed readers and writers for immutable catalog revision JSON columns. |
| `control/src/vonk_control/catalog_sync_contract.py` | controller-contract | 4 | Canonical durable catalog synchronization evidence shared with the API. |
| `control/src/vonk_control/cli_update_contract.py` | controller-contract | 1 | Authenticated CLI compatibility observation; its canonical schema is generated into the installed updater package. |
| `control/src/vonk_control/cluster_mappings.py` | declared | 2 | The identity document whose digest binds a cluster mapping plan to its exact placement. |
| `control/src/vonk_control/compiled_artifact_contract.py` | controller-contract | 12 | Canonical compiled contract for artifact-producing recipe jobs. |
| `control/src/vonk_control/compiled_execution_plan.py` | declared | 7 | Verified Controller receipts for the compiled Spark execution plan. |
| `control/src/vonk_control/distribution_assignment.py` | declared | 1 | The Controller's record of one node's artifact distribution grant. |
| `control/src/vonk_control/endpoint_contract.py` | controller-contract | 1 | Secret-free projection of one published recipe endpoint. |
| `control/src/vonk_control/enrollment_contract.py` | controller-contract | 6 | Enrollment status, grants and exact issuance/rotation claims. |
| `control/src/vonk_control/failure_evidence.py` | declared | 5 | Failure diagnostics rendered on request from durable failure rows. |
| `control/src/vonk_control/fleet_event_contract.py` | controller-contract | 9 | Strict payload contracts for the durable Fleet outbox. |
| `control/src/vonk_control/fleet_profile_adapter_conversion_contract.py` | controller-contract | 3 | Private one-time retained journal proof inputs and typed conversion outcome; never execution authority. |
| `control/src/vonk_control/fleet_profile_contract.py` | controller-contract | 50 | Strict public contracts for saved Fleet profiles and their applications. |
| `control/src/vonk_control/fleet_profiles/contracts.py` | controller-contract | 1 | Saved profile content identity used by admission and projections. |
| `control/src/vonk_control/fleet_projection.py` | declared | 12 | Complete typed projection of PostgreSQL-authoritative Fleet state, transferred through bounded immutable observation records. |
| `control/src/vonk_control/fleet_stream_contract.py` | controller-contract | 13 | Typed JSON envelopes emitted by the Fleet Server-Sent Events stream. |
| `control/src/vonk_control/gateway_keys.py` | declared | 5 | Inference gateway client keys: LiteLLM virtual keys managed by the Controller. |
| `control/src/vonk_control/harnesses/canonical_metadata.py` | declared | 1 | Strict platform metadata for built-in canonical harnesses. |
| `control/src/vonk_control/job_documents.py` | controller-contract | 41 | Typed documents of the generic job queue bound to the job columns; the kind-agnostic remainder is a declared passthrough. |
| `control/src/vonk_control/library_contract.py` | controller-contract | 45 | Bounded typed contract and deterministic display helpers for Library reads. |
| `control/src/vonk_control/lifecycle_preflight.py` | declared | 1 | Durable, recipe-bound admission probes without replaying expensive phases. |
| `control/src/vonk_control/litellm.py` | declared | 7 | The LiteLLM configuration the Controller renders from published routes (our document, LiteLLM's file format). |
| `control/src/vonk_control/model_cache/input_contracts.py` | controller-contract | 3 | Typed catalog artifact locators and explicit fixture ingress. |
| `control/src/vonk_control/model_cache/provider_contracts.py` | declared | 3 | Durable, content-addressed model artifacts stored on the Controller NAS. |
| `control/src/vonk_control/model_cache_contract.py` | controller-contract | 40 | Schema-2 contracts for the Controller-owned NAS model cache. |
| `control/src/vonk_control/observation_transfer.py` | controller-contract | 5 | Canonical immutable observation transfer records; sequence, identity, byte count and final digest bind the original complete Fleet or platform payload. |
| `control/src/vonk_control/operation_api/contracts.py` | declared | 20 | Strict, secret-free representations for routine administrative operations. |
| `control/src/vonk_control/operation_blockers.py` | declared | 1 | One typed answer to "what is this operation waiting for?". |
| `control/src/vonk_control/operation_contract.py` | controller-contract | 4 | Current nested contracts for durable Controller operations and progress. |
| `control/src/vonk_control/operation_item_contract.py` | controller-contract | 3 | One operation of any family as Activity projects it: the typed item, its owner and the failure facts of its stored result. |
| `control/src/vonk_control/operator_projection_api.py` | declared | 10 | Singular operator API for Fleet, Model and Recipe projections. |
| `control/src/vonk_control/platform_observation.py` | controller-contract | 3 | API and worker process provenance observations; unavailable producer facts remain nullable. |
| `control/src/vonk_control/preparation_contract.py` | controller-contract | 10 | Shared schema-2 truth for Controller-owned rollout preparation. |
| `control/src/vonk_control/profile_stop_authority.py` | declared | 4 | Typed ownership for profile-authorized one-shot JobRun cleanup Stops. |
| `control/src/vonk_control/recipe_availability_intent.py` | declared | 4 | Original requests, distinct from resolved preparation and worker effects. |
| `control/src/vonk_control/recipe_build_cancellation.py` | declared | 1 | The lifecycle job owns cancellation of an exact build attempt. |
| `control/src/vonk_control/recipe_execution_contract.py` | controller-contract | 10 | Strict persisted contracts for recipe execution and source builds. |
| `control/src/vonk_control/recipe_image_availability_api.py` | declared | 9 | Authenticated, typed HTTP routes for Recipe Make available. |
| `control/src/vonk_control/recipe_image_availability_contract.py` | controller-contract | 2 | Shared recipe-image state, artifact and builder receipt identity. |
| `control/src/vonk_control/recipe_image_availability_clocks_contract.py` | controller-contract | 5 | Tolerant typed lifecycle clock projection independent of job-document imports. |
| `control/src/vonk_control/recipe_image_availability_reader_contract.py` | controller-contract | 4 | Typed adoption and exact-catalog recovery of damaged availability bookkeeping. |
| `control/src/vonk_control/recipe_image_availability_view_contract.py` | controller-contract | 2 | Typed availability and removal read projections with canonical JSON egress. |
| `control/src/vonk_control/recipe_image_removal_contract.py` | controller-contract | 6 | The current persisted intent for one recipe cache removal request. |
| `control/src/vonk_control/recipe_lifecycle_contract.py` | controller-contract | 9 | Typed response contracts for recipe lifecycle operations. |
| `control/src/vonk_control/recipe_operations/contracts.py` | controller-contract | 2 | Typed recipe operation context and installation disposal receipt. |
| `control/src/vonk_control/recipe_packages/contracts.py` | declared | 2 | Schema-2 recipe package reader for the Controller catalog sync. |
| `control/src/vonk_control/recipe_update_contract.py` | controller-contract | 8 | One current contract for durable recipe-update intent and observation. |
| `control/src/vonk_control/recipe_update_notice.py` | declared | 1 | One owner for "a newer revision of this recipe exists" (never restarts anything). |
| `control/src/vonk_control/resource_planning_contract.py` | controller-contract | 2 | Canonical nested recipe topology and resource settings read projections. |
| `control/src/vonk_control/route_bundle_contract.py` | controller-contract | 6 | The published route bundle (`routes.json`) and the identity document whose digest names a candidate bundle. |
| `control/src/vonk_control/run_switch_contract.py` | controller-contract | 66 | Strict, transport-neutral contracts for high-level Run and Switch work. |
| `control/src/vonk_control/run_switch_identity_contract.py` | controller-contract | 1 | Shared Run/Switch request identity constraints and typed cancellation intent independent of ORM and workers. |
| `control/src/vonk_control/run_switch_journal_contract.py` | controller-contract | 4 | Typed run-switch journal repair evidence and audit records. |
| `control/src/vonk_control/run_switch_observation_contract.py` | controller-contract | 7 | Typed observed progress, retained lifecycle identity, artifact guards and build receipts. |
| `control/src/vonk_control/runtime_image_preparation/contracts.py` | declared | 2 | Controller-owned preparation of exact runtime image archives. |
| `control/src/vonk_control/runtime_spec_contract.py` | controller-contract | 18 | The compiled runtime specification the recipe compiler produces and the launch plan projects. |
| `control/src/vonk_control/step_ca.py` | declared | 8 | step-ca provisioning documents the Controller reads and writes. |
| `control/src/vonk_control/stored_documents.py` | controller-contract | 2 | Typed documents stored in plan, run and installation JSON columns. |
| `control/src/vonk_control/strict_json.py` | declared | 1 | Controller JSON models and their field-presence serialization policy. |
| `control/src/vonk_control/worker_memory_contract.py` | controller-contract | 5 | Typed memory report the worker publishes and the Controller API exports. |

`stored_json.py` and `stored_columns.py` hold no wire models: they bind every JSON column of `models.py` to its typed contract (36 of 36), and `tests/test_json_column_contracts.py` fails on an untyped level. The only open values are the declared `ExternalPassthrough` annotations (engine argument values, generic job queue documents), each with a reason.

## Counts

| | Before | After |
| --- | --- | --- |
| Rust hand-written serde types outside `generated.rs` (src) | 50 in 13 files | 11 in 6 files, all external formats or tolerant site files |
| Rust serde types in examples and tests (harness inputs) | 7 | 8, allowlisted |
| TypeScript hand-written API data shapes | 1 named by the owner (`CliTokenDownload`), plus an inline body type and a blocker shape | 0; 44 UI-only shapes allowlisted with reasons |
| Python duplicate model groups (same name or same bases and fields) | 12 | 0 |
| Python copies of contract words | 2 (`cli_states.py`, `route_activation.py`) | 0; both read generated modules |


### Narrow Run/Switch journal repair evidence

`run_switch_journal_contract.py` owns the typed append-only evidence in
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
remains untouched. A typed `run_switch_journal_repair_pending.progress` record
owns a fixed two-minute observation deadline, capped exponential backoff, and
monotonic cancellation intent. Cancellation commits before proof or observation.
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
