# Execution harness operations

An execution harness is the stable lifecycle contract between a recipe and the
Spark agent. It compiles declarative recipe inputs into the universal source,
build, distribute, install, run, health, route, stop, and uninstall operations.
Operators act through those operations and their preview digests; they do not
start a parallel container and then write Library state by hand.

See [Model and recipe identities](model-catalog.md) for model identity and revision concepts.

## Canonical recipe and compiler

A canonical **RecipeDefinition** names its engine, immutable image or build
inputs, model selections, topology, resources, lifecycle, security policy, and
serving checks. A separate **ModelDefinition** owns the exact model artifacts
and license facts. The exact reviewed repository snapshot and package SHA-256
close over those documents and all recipe-owned source, patches, wrappers, and
serving fixtures.

The platform compiler turns that package into a shell-free
`CompiledExecutionPlan`. Engine adapters enforce platform-owned invariants and
pass ordinary engine arguments unchanged after the executable. Several recipes
for the same model and Spark count remain independent revisions, including
variants from one creator with different engines or settings.

## The eight built-in harnesses

The built-in compiler adapters are `comfyui`, `diffusers`, `ds4`, `llama-cpp`,
`pytorch-pipeline`, `sglang`, `tensorrt-llm`, and `vllm`. Their names describe
execution behavior, not separate catalog entities or a promise that every
model supports every engine or topology. The selected Model, Recipe, package,
compiler, structural qualification, and Fleet admission must agree.

## Interface publication

An `openai` interface follows the serving path
`client → Tailscale → Caddy → LiteLLM → accepted entrypoint`. The controller
publishes the recipe alias to LiteLLM only after all required ranks report fresh,
matching evidence and the route-serving lease is valid. Caddy owns static path
and trust boundaries; LiteLLM neither discovers containers nor resolves catalog
documents. Rank loss, stale evidence, stop, or lease loss withdraws the alias.

Artifact-producing interfaces are jobs rather than OpenAI model routes. Their
submission, progress, cancellation, and result artifacts use the declared
controller/job interface directly; a result location is not placed in LiteLLM.
Health and cleanup still use the same harness lifecycle and exact node evidence.

## Acceptance evidence

`scripts/qualify-recipe --level structural` resolves the exact canonical Model,
Recipe, package closure, source bundle, compiler projection, and declared
serving checks before credentials or workload network access. The qualifier's
container level currently returns `environment-limited` because the production
`CompiledExecutionPlan` materializer is not linked; that result is not evidence
of container execution.

Physical lifecycle work uses Controller Run/Switch, including the normal
`vonkctl models run --input-file REQUEST.json --json` command. The Controller
binds the selected recipe revision and Spark group to fresh certificate-bound
inventory, capacity, fabric, model and image bytes, operation phases, per-rank
receipts, and route state. After the route is active, execute the Recipe's
declared HTTP serving checks with `scripts/qualify-recipe --serving-url URL
--evidence-ledger PATH` and retain that bounded result separately.

Keep structural, container, Controller operation, and physical Spark evidence
as separate gates. State names without an image digest, artifact-set digest,
serving result, cleanup operation, or changed post-restart host boot ID cannot
overstate acceptance. A changed recipe revision, package digest, Model digest,
Spark identity, topology, or operation plan cannot reuse older evidence.

At each physical restart checkpoint, retain the host boot ID from serialized
Fleet telemetry. Heartbeat timestamps and Fleet `generated_at` are not restart
proof. Every selected node must return with a different boot ID before route
and inference evidence can be bound to the new identity.

One-Spark acceptance includes canary serving and an offline restart.
Distributed acceptance additionally proves failure-rank loss, route withdrawal,
rank recovery, recovered serving, and a final offline restart. Only the full
single-node ladder, or the full distributed rank-loss/recovery ladder, cleanup,
and changed boot identities can support `spark-accepted`.
