# Controller-owned rollout preparation

Explicit user decision, 2026-09-05: the Controller prepares quick rollouts; Spark agents download models and containers directly from it and run them. Required refinement of the interface implementation plan, NAS cache, profile preparation and run/switch contracts.

## Ownership and workflow

The Controller resolves exact model version, recipe revision, artifact set, compatible Linux/ARM64 runtime image and target placement. NAS-backed storage holds verified immutable model files and OCI content separately. The Controller orchestrates published-image retrieval or a compatible build worker; Controller ownership does not mean compiling CUDA workloads inside the API process or requiring NAS CPU compatibility.

Prepare is a durable, preview-bound operation: resolve identities and compatibility; fetch/build missing artifacts; verify their digests; make them available through authenticated Controller delivery; optionally stage to the complete selected Spark group; verify target files and import the runtime image. Profile Prepare stages both model and image artifacts for its explicit target scope. Show per-object and per-Spark progress, bytes remaining and actionable failures. Reuse exact verified objects across recipes/profiles wherever identities match.

Spark agents use Controller-issued authenticated artifact delivery, verify destination bytes and image identity, import/reuse the image, then launch the pinned recipe. Normal rollout does not compile source or download from internet origins on Sparks. Recipe-specific GPU compilation, engine generation or tuning is an explicit exception with a typed preparation requirement and compatibility key. Required exceptions prevent the UI from claiming fully prepared; never silently fall back. Existing prepared workloads remain usable when remote catalogs are unavailable.

## States and review

Keep these facts separate: available upstream; preparing on Controller; ready on Controller; staging to Spark; ready on selected Sparks; starting; running. Controller cache completeness alone is not target readiness. Ready on selected Sparks means both exact model files and the executable image are verified there, with unavoidable remaining launch steps disclosed. Running requires actual readiness and endpoint evidence. Historical hardware qualification remains separate from these operational states.

Library shows model-cache status and runtime-image readiness independently, then one clear Prepare or Run action. Review lists missing model/image bytes, copy versus build work, target disk capacity, compatibility requirements, existing workload stops and any cleanup. Default retention preserves NAS objects and reusable target artifacts. A fully staged profile switch performs only necessary stops, launch and readiness checks; it must not rebuild or recopy unchanged objects.

## Distribution contract

Controller serves only verified immutable objects to authorized enrolled agents for approved operations. Grants bind object digest, size, target identity and operation; credentials are not exposed in UI, logs or copied commands. Support bounded streaming and safe resume/ranges with identity validation and final destination digest verification. No arbitrary filesystem path or caller-selected upstream URL. Models remain outside container layers. OCI manifests/layers or a verified archive retain canonical image identity through import. Reuse existing verified image delivery where possible rather than adding a competing transfer stack.

Network failure leaves a resumable checkpoint. Corrupt/incomplete files are not promoted to ready. Cleanup cannot remove in-use or referenced model/image objects, and Spark-local cleanup cannot evict NAS cache. Controller build workers and transfers are bounded durable jobs, not long blocking API handlers. CLI exposes the same preview, prepare, status, retry, staging and switching with stable JSON and request-key/digest semantics.

## Acceptance

1. Prepare an exact model+recipe for two Sparks: fetch/build once on Controller, deliver verified artifacts to both through authenticated transport; no Spark internet fetch.
2. Repeat preparation: no redundant model download, image build, target copy or import when verified identities still match.
3. Profile A to B to A retains and reuses prepared model/image objects; explicit idle targets remain idle. Check actual production operation orchestration, not only synthetic database state.
4. Interrupted delivery resumes safely; modified bytes, mismatched digest, unauthorized agent and stale plan cannot become ready or run.
5. Cached model with missing runtime image is visibly incomplete. Controller-ready but unstaged Spark is visibly incomplete. GPU preparation exceptions are visible before apply.
6. Web and CLI report identical identities, missing bytes, phases and target readiness. Stop/start success is not inferred from enqueue success.

Sol owns cross-worker integration and contract ledger. This authorizes repository implementation and tests, not live NAS/Spark deployment, external image publication or hardware acceptance claims.
