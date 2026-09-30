# Image transfer and cache

This page describes the current transfer implementation. The target
[ownership and coordination boundary](architecture-overview.md#state-ownership)
places artifact bookkeeping in managed storage. Existing native OCI caches
and transfer behavior remain the starting point for that work.

Recipe images are normally prebuilt. When a recipe is published or updated,
the recipe library builds each distinct image on a GitHub-hosted ARM64 runner
with the same build flags and platform adaptation stage a Spark uses, pushes
it to GHCR (public), and pins the pushed manifest digest beside the recipe in
the signed catalog index (`prebuilt_image: {reference, build_key}`). The
`build_key` names the executable build inputs (source bundle, Dockerfile,
pinned base images, build options and capabilities, runtime adapter); recipes
that build the same adapter directory the same way share one image.

Build planning prefers that image when the Controller derives the same key for
the revision. The plan then admits no Spark inventory, disk or memory, and its
`recipe.build.v1` job gets no Spark operation: the worker's prebuilt importer
pulls the pinned digest with Skopeo into a Docker archive in `image-cache/`.
Skopeo verifies the manifest and every blob against the digest; the archive
digest is computed once at that point and names the file. The importer records
the same build evidence a Spark upload records, so everything after it
(receipt, authorization, distribution to Sparks) is unchanged, and a prebuilt
image and a Spark build of the same inputs are interchangeable reuse
candidates. A pull runs off the worker loop under a renewable lease, so a
restarted worker resumes it; cancelling the build discards a late pull.

A Spark build remains the fallback: when the catalog pins no image, when the
image was built from other inputs (for example under a different platform
adapter), or when this Controller already failed to pull that digest. A failed
pull is visible on the build (`prebuilt_image_pull_failed`) and in the worker
log, and the next plan builds on a Spark. A newly published digest is tried
again.

A Controller source build records its exact executable input identity on the
filesystem receipt beside the archive. That receipt owns availability: reuse is
decided by the verified bytes on disk, not by the build row's status or digest
fields, and a receipt whose archive is missing is ordinary cache loss that is
rebuilt instead of reported as a failure. The build row remains the index of
candidate builder identities and the audit record; it is not the availability
gate.

After the exact model and image assets for a profile are ready in the cache, the
Controller distributes them to all selected Sparks in parallel. The transfer
grant binds the image identity, size, destination and operation. A Spark
verifies the destination before import, and an already verified local copy is
skipped. The NAS-to-Spark distribution endpoint already supports authenticated
HTTP Range and resumable partial files; archive transfers synchronize storage
at completion rather than per fragment. Distribution progress and per-Spark
readiness are reported before workloads are stopped and replaced.
