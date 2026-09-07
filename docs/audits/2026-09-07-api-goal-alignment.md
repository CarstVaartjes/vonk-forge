# API and project-goal review

Reviewed on 7 September 2026 against the contract-gap-closure integration.
This is a source review; it does not establish deployment or physical Spark acceptance.

## What fits the product

- Model and Recipe are the only public authoring documents, owned by the recipe repository's Pydantic package. Operations, progress, telemetry and execution messages have their own platform contracts.
- Model download is independent from recipe image preparation. Controller cache services expose asynchronous operations, progress, provider-access failures, retry and refresh/repair.
- Profiles define their complete Spark scope. A covered Spark with no assignment has an Idle outcome. Model/recipe placements and cache retention are separate concerns.
- API requests and responses produce the OpenAPI graph used by the Python and TypeScript clients. Rust uses handwritten types with connected Python/Rust producer and consumer tests.
- The full nested serialization graph has seven intentional extension objects: engine parameter values, engine output measurements and a path-selected authority document. No other opaque object or array holes were found.
- Retired catalog parsers, harness registry/configuration, old graph execution modules and old recipe schema assets are absent from active runtime paths. Current wire document version numbers are independent of the Model/Recipe authoring version; a numeric version 1 alone is not evidence of a legacy reader.

## Corrections exposed by the review

The review found incomplete persisted Fleet reads, one Run/Switch retry read bypassing the canonical parser, a distribution phase mismatch check, undocumented browser-auth errors, an image-progress fallback, profile CLI route/body mismatches, and handwritten browser proposal types. These are tracked in the integration changes; final test and publication results belong in the launch consolidation record.

It also found unused RecipeDraftInput and custom-recipe preset assets, now removed. Historical reviews are kept as evidence, not executable compatibility paths.

## Five findings and their resolution

1. **CLI Library placement commands — implemented.** `vonkctl library placement preview/apply/get/retry` exposes the current placement API. `profiles retry` covers profile applications. Requests use the same generated OpenAPI validation as other CLI calls; replies retain the Controller's operation identity and progress. The [CLI runbook](../runbooks/vonkctl.md) includes executable command forms.
2. **Profile/placement application recovery — implemented.** Both application families expose an authenticated `POST .../{id}/retry` with a UUID `request_key`. The Controller stores the intended configuration and creates a linked new attempt against current fleet state. It retains the original receipt and successful installation/run state. Repeated requests return the same attempt, including after restart. Changed saved profiles, superseded attempts, and active or uninspectable children prevent stale work from being replayed. The profile editor and Activity follow the returned ID; a fresh preview is not labelled a retry.
3. **Terminal-state consistency — implemented.** Canonical cache, Run/Switch, image availability, recipe lifecycle, artifact job, profile, and placement responses enforce state-specific evidence. Persisted service reads use the same validation. Success requires its real receipt, failure requires its explanation/evidence, and legitimate partial receipts survive recovery. Eviction now persists a typed failure; completed Run/Switch attempts clear stale failure and retry markers.
4. **Profile form validity — implemented.** Empty required scope or assignment ranks prevent submission and explain the repair inline. An all-idle profile remains valid. Completion refreshes server-reported status, without assuming a successful application proves that the fleet has not subsequently drifted. Chromium covers invalid input, repair, idle saving, and linked retry at 1280px and 360px.
5. **Activity failure visibility — implemented.** Activity reads canonical operations as well as jobs and audit records. List/detail responses preserve a redacted profile failure, attempt lineage, and currently supported recovery actions. The browser displays these failures and can retry both profiles and placements. Independent source failures and pagination remain visible. The unused placement dialog and its isolated tests/CSS were removed; the existing Library Run/Switch interface remains the primary model-running flow.

The connected recovery test drives an actual Controller route through CLI request validation, a generated Python response model, and canonical Pydantic revalidation. It also verifies Activity before and after retry. Coordinator tests prove that an installation which succeeded before a start failure is retained, and that only the remaining start is planned after restart. These establish repository behavior; deployment and physical Spark acceptance remain separate evidence boundaries.
