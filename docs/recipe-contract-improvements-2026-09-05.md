# Five approved recipe contract improvements

User explicitly requested all five implemented immediately, 2026-09-05. Required addition to the agreed delivery scope. Preserve the simple user workflow and all existing recipe behavior while converting platform and complete recipe-library producer together. Sol supervises bounded Luna owners and integration; no unrelated broad wire-version migration or new user-facing qualification ceremony.

## 1. One canonical model-file authority

Expected upstream source identity, exact file digest and byte length come from the pinned model-version manifest. Recipe intent selects primary/dependency model identities, included artifact IDs/paths, topology roles and mount destinations. Do not keep two editable definitions of source/revision/size whose equality depends on convention. Package compiler resolves the exact complete file inventory once, including tokenizer/config/auxiliary dependencies, and verifies every selection is valid. Download/cache/distribution/install use the same resolved set digest. Derive totals and capacity from this inventory with deduplication by content identity. Verify observed target file/set identity equals expected, not merely a syntactically valid hash.

Convert every existing recipe and its packages with an automated deterministic producer; report unconvertible cases by name. Snapshot current source/file behavior before conversion and compare resolved artifacts after conversion. Changes must not silently broaden selected snapshot files or omit auxiliary weights. No Controller dependency on another recipe package: canonical metadata is copied into each self-contained package.

Acceptance: all current recipes resolve the same intended artifacts; mismatched/unknown selected files and dependency substitution reject; changing canonical file bytes/digest changes affected package/set identities; unchanged recipes retain stable hashes; real NAS and distribution consumer tests use that resolved inventory.

## 2. Direct image execution; build only when necessary

Support the explicit mutually exclusive executable choices: consume a verified pinned compatible runtime image, or build from exact source inputs to produce such an image. Choose precise DTO syntax with existing runtime-distribution authority; avoid another competing image reference. Image-only recipes do not require dummy Dockerfiles/build contexts. Build recipes retain reviewed source/context/patch closure and produce persisted exact output receipts. Image planning verifies ARM64, runtime interface, required wrapper/entrypoint and dependency compatibility; a base image that still needs adapter composition is not a ready executable image.

Normal Run resolves/reuses/downloads/imports image; no build job or source retrieval for a genuinely executable image-only recipe. Builds occur only for missing required output or changed relevant input identity. Share image bytes across targets; verify real imported identity.

Acceptance: image-only recipe survives catalog import/Library/Run on two targets without invoking builder; source-built recipe executes one build and reuses receipt on restart/replay; invalid both/neither choices reject; incompatible image rejects before interruption. Existing recipe conversion must distinguish executable final images from base images honestly.

## 3. Engine invariants belong in harnesses

Centralize runtime-owned cache paths, writable mounts, baseline launch conventions and telemetry adapter configuration in the exact harness/runtime contract. Existing vLLM cache path rule is mandatory. Cover all supported engine families using upstream evidence. Engine-specific flags can differ by pinned version/distribution; do not force a generic vLLM/SGLang assumption across forks. Recipes keep legitimate model/topology/tuning options and explicit reviewed adapter patches.

Create one effective launch plan merging runtime/harness facts with allowed recipe parameters. Reject reserved conflicts/repetition where platform owns the invariant. Validate writable containment and preserve non-root/capability-free/read-only-root restrictions. A mount cannot shadow packaged runtime libraries or silently discard compiled image artifacts. Do not hide required model-specific preparation inside an unconstrained script or claim support from the name alone.

Acceptance: actual effective launch plans for all harness families; reserved/conflicting overrides reject; source-backed exceptions explicit; representative container starts under read-only root; exact telemetry adapter settings consistent with launched engine. GPU-only tests run in matching designated lane and remain separately reported.

## 4. Effective settings drive resource planning

Resolve typed settings once (including context, concurrency/batch, parallelism and recipe-specific knobs), validate bounds and interdependencies, and use the same values for launch, memory fit, placement and preparation reuse identity. Account for distributed ranks and shared GB10 memory once. Resource demand must distinguish weights, runtime overhead and context/KV-sensitive terms when evidence supports that breakdown. Use measured profiles or declared supported bounds; no universal fabricated memory formula for every engine/quantization.

Missing evidence must be explicit. Use safe defaults/known supported configurations; do not silently accept arbitrary settings that exceed the validated range. Current occupied memory and capacity after planned stops are separate inputs. Determine required work by affected inputs: runtime restart, artifact staging, model-specific preparation or image build; change context alone must not blindly rebuild an unchanged image. Preview/digest remains internal to normal one-click Run.

Acceptance: larger context/concurrency changes computed demand or exits declared supported envelope; invalid combinations reject; fit uses exact submitted values and per-rank topology; after-stop capacity is applied only when that stop is actually planned; cache/preparation reuse invalidates when compatibility-relevant settings change. Tests assert observable plan/launch agreement, not duplicate arithmetic only.

## 5. Maintainer tests prove serving, not just process health

Define recipe-level representative serving checks consumed by the actual qualification harness. Match interface: text completion/chat; vision input if advertised; tools if advertised; embedding dimensions if supported; image/audio/video/job submission and valid result for those recipe types. Check semantic contract properties without brittle verbatim model answers. Readiness must include successful representative serving evidence where the runtime supports it; distinguish maintenance qualification test from lightweight runtime health checks to avoid expensive generation on every UI refresh.

Test start, first request, restart and reuse of existing verified model/image assets; prove unchanged inputs do not redownload/rebuild. Distributed serving verifies complete ranks and actual endpoint. Store evidence binding exact recipe/model/image/settings/hardware/validator version. Failure identifies what failed and does not become a user-facing certification task. CI may use disposable real runtimes where possible; simulated model replies prove orchestration only. Full physical testing of all large models remains a separately executed campaign, never inferred from contract fixtures.

Acceptance: actual test executor runs the declared checks; recipe claims are matched by relevant checks; failed request fails qualification even with HTTP health 200; restart/reuse test observes no redundant download/build; result evidence is exact and durable. Inventory every current recipe's interface and required tests, report unsupported execution lanes explicitly.

## Integration and delivery

Agree canonical schema/resolver inputs before simultaneous edits. Use isolated ownership for artifact/image contract, effective launch/resource planning, test executor and recipe conversion. Shared generated clients/schemas/package fixtures have one integration owner. If a persisted wire change is necessary, design producer, consumer, constraints and migration together with retained previous-good catalog semantics; do not repeat rejected partial schema flips.

Run full catalog conversion against exact platform commit, real HTTP package importer and automatic Run/Switch composition, generated drift and relevant Python/Rust/browser/Linux checks. Existing deployments keep working until coordinated compatible release. Publishing/merges/deployments are authorized through existing paths; preserve live data. Maintain the same user product: select model/recipe/Sparks, Run or Switch, see actual progress.
