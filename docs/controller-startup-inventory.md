# Controller startup failure boundaries

This inventory covers API composition, agent composition, worker composition,
and API pre-exec. It describes source behavior, not deployment acceptance.

The API now guards 18 capabilities independently. Worker composition also
isolates management policy and image collection, for 20 guarded capability
identities in total (previously none had the shared recovery mechanism).
`/api/platform` reports the API process's capability observations; worker
provenance remains a separate observation.

| Startup owner | Potential failure | Current boundary |
| --- | --- | --- |
| `api.production_app`: token codec, cursor codec, browser authentication | Missing, malformed or unreadable signing secret | Independent guarded authentication services; verification cannot proceed without the real key |
| `api.production_app`: metrics and agent proxy authentication | Missing or malformed secret | Guarded secret providers; unrelated HTTP requests never read the agent proxy secret |
| `agent_services.build_agent_services`: bootstrap configuration | Missing controller root, malformed hostname or origin | Guarded bootstrap; configuration loader defers hostname validation to this edge |
| `agent_services.build_ca` | Invalid CA policy, root/intermediate/key/JWK files, TLS/provider failure | Guarded enrollment construction and bounded health probe, before enrollment opens any SQL transaction |
| `agent_services.build_host_authority` | Missing or invalid grant signing key | Guarded issuer; no fallback signing material |
| Agent presence, fabric policy and worker management policy | Malformed address policy | Guarded parser/service; fleet projection has no dependency on the parser |
| API and worker runtime image storage | Directory creation, permissions, malformed storage root | Guarded storage; image availability reuses that same storage rather than eagerly constructing another owner |
| API and worker model cache | Directory and checkpoint access, SQL resume, executor construction | Guarded constructor and resume; normal cache methods resolve the same facade |
| API and worker distribution | Nested runtime storage construction | Guarded source construction; no independent Spark availability authority |
| API and worker route publisher | Directory permissions, symlinks, retained marker/state | Guarded publisher; existing routes are untouched by construction failure |
| API recipe route service | Invalid management policy | Guarded service; no route withdrawal during initialization |
| API artifact jobs and worker blob store | Blob directories, reconciliation, SQL references | Guarded construction/reconciliation; other worker lanes continue |
| API recipe package client | Local package directory, URL configuration, client construction | Guarded package reader; automatic catalog sync still owns refresh |
| API gateway key client | Missing master key, unavailable LiteLLM, readiness refusal | Guarded client/readiness check; retained client is probed again after backoff |
| Worker image collector | Nested runtime image directory construction | Guarded collector; references are still required before any deletion |
| API/worker SQL repositories, projections, admissions, operation services, profile builder, metrics registry, transport, upgrade client and scheduler wiring | Fixed budgets and in-memory wiring | Reviewed direct constructors, without startup I/O; effects remain in existing request/worker methods |
| Worker prebuilt importer, telemetry/history collectors, storage demands/collector, maintenance cadence, heartbeat and memory monitor | Fixed budgets and retained paths | Reviewed wiring; existing per-source worker containment handles tick/sample failures |
| `Settings.from_env_and_secrets`, engine/session creation | Missing/invalid database URL or inaccessible database authority | Required core dependency; database identity is never substituted |
| `api_preexec.prepare_owned_state` and `db.initialize_database` | Privilege, shared-volume/secret normalization, migration/schema conflict, administrator bootstrap | Existing privileged/core startup boundary; bookkeeping adoption already uses its own savepoint and logs/degrades without undoing schema reconciliation |

Construction attempts use a nonblocking per-capability owner. Failure produces
one canonical `CapabilityReason`, availability and next-attempt timestamp.
Backoff starts at one second and caps at 60 seconds. Requests trigger their
needed construction; API timers independently retry unavailable capabilities.
CA and gateway health are re-observed every 30 seconds after successful startup.
Observing capability status never starts, retries or blocks construction.

A request stopped by unavailable construction receives a canonical typed 503.
The unavailable request has no effects to replay; existing service methods keep
ownership of their own operation identity, reconciliation and cancellation.
Factory retries do not replay a method that already performed effects.

The new startup AST guard inventories reviewed direct construction. A new
constructor in these four entry boundaries fails the always-on repository guard
unless it is deferred to a factory or reviewed as core/pure wiring. It also
contains a deliberate new-constructor rejection test. The registry is compared
against working-tree sources without Git access.

The privileged pre-exec and required database configuration boundaries still
can prevent process startup. This change does not claim that an unavailable
PostgreSQL authority can serve authenticated fleet data, or that all pre-exec
failures have been converted to runtime capability observations.
