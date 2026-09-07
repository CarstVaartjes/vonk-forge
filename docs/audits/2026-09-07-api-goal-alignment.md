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

## Remaining product gaps

1. **CLI Library placement commands.** The API and generated clients provide placement preview/apply/get, but the human-facing CLI command tree does not expose the full workflow. Generated coverage alone is not CLI parity.
2. **Profile/placement application recovery.** These applications have no durable retry endpoint. The browser's recheck action creates a fresh preview; it must not be described as resuming a failed application. Design recovery around the persisted application and current fleet state, with observable partial completion.
3. **Terminal-state consistency.** Some operation response models allow a terminal state without its expected result or failure. Add state-specific invariants with actual producer/restart cases; do not infer valid completion from the status string alone.
4. **Profile form validity.** Scope and assignment editors should prevent submitting empty required node lists and explain what must be selected. Backend structural rejection is insufficient UX.
5. **Activity failure visibility.** Profile status reasons need to reach the canonical failure projection, not only a family-specific status string.

The architecture fits the intended workflow. Full user/agent parity and recovery still require the concrete gaps above; neither Pydantic adoption nor a green schema check alone proves those product outcomes.
