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

Runtime images live in one content-addressed OCI layout in
`image-cache/oci/` on the Controller. Images share layers there: a recipe
update adds only its changed layers (about 90 MB compressed for a GLM 1.7.x
update, against roughly 10 GB for the first copy of the image). A runtime
image is named by its manifest digest; the build row's `oci_layout_sha256` is
that digest's hex (its address in the layout), `image_bytes` is the size of
its layers and the compiled plan's `local_image_config_id` is its config
digest.

Build planning prefers the prebuilt image when the Controller derives the same
key for the revision. The plan then admits no Spark inventory, disk or memory,
and its `recipe.build.v1` job gets no Spark operation: the worker's prebuilt
importer copies the pinned digest into the layout with
`skopeo copy --preserve-digests`. Skopeo fetches only the blobs the layout
lacks and verifies the manifest and every blob against the digest; that is the
one place image bytes are verified. The importer records the same build
evidence a Spark build records, so a prebuilt image and a Spark build of the
same inputs are interchangeable reuse candidates. A copy runs off the worker
loop under a renewable lease, so a restarted worker resumes it; cancelling the
build discards a late copy. One image is written into the layout at a time; a
copy that finds the store busy hands its job back and the next tick retries it.

A Spark build remains the fallback: when the catalog pins no image, when the
image was built from other inputs (for example under a different platform
adapter), or when this Controller already failed to copy that digest. A failed
copy is visible on the build (`prebuilt_image_pull_failed`) and in the worker
log, and the next plan builds on a Spark. A newly published digest is tried
again. A Spark build uploads its Docker archive beside the layout; once the
build has succeeded, the worker converts that archive into the layout,
rewrites the build row to the stored image's identity and removes the archive.
Until then the image is not yet in the store, and a consumer waits for it as
for any missing image.

A Controller source build records its exact executable input identity on the
filesystem receipt beside the layout (`image-cache/<address>.receipt.json`).
That receipt owns availability together with the stored image: reuse is
decided by a complete image in the layout (every blob present at its manifest
size; nothing is re-hashed), not by the build row's status or digest fields,
and a missing image is ordinary cache loss that is rebuilt instead of reported
as a failure. The build row remains the index of candidate builder identities
and the audit record; it is not the availability gate.

After the exact model and image assets for a profile are ready, the Controller
distributes them to all selected Sparks in parallel. The distribution
assignment lists the model objects and names the runtime image by its manifest
and config digests. A Spark downloads the model objects over the authenticated,
resumable HTTP Range endpoint, then pulls the image with `docker pull`.

The agent site serves the layout read-only and by digest only (`GET`/`HEAD` of
`/v2/` and `/v2/vonk/runtime/{manifests,blobs}/sha256:<hex>`), to verified
agent identities. For one pull the agent serves those routes on a loopback port
through its authenticated client (Docker treats loopback registries as plain
HTTP); Docker fetches only the layers it lacks and verifies each against the
manifest, and the helper's `image-pull` action requires the pulled image to
carry the pinned identity before tagging it
`localhost/vonk/compiled-runtime-<manifest hex>` and writing its receipt. A
Spark that already holds the image skips the pull. On success the Controller
records the image as present on that Spark, so the next plan does not
distribute it again. Distribution progress and per-Spark readiness are reported
before workloads are stopped and replaced.

The worker reclaims image bytes nothing names any more, once an hour. An
image is kept while its receipt exists, while a live distribution assignment
names it, or while a build that produced it is unfinished or finished less than
24 hours ago (an image waiting for its first receipt). Every other blob that no
kept image refers to is removed once it is older than 24 hours; a copy marks
every blob of the image it stored as fresh, so a just-stored image always
survives that long. Uploaded Spark archives that no build still waits on go the
same way. Collection and copies share the store's single writer lock, so they
never interleave, and a busy store is simply collected next time. An image
needed again after collection is stored or built again, like any cache loss.

Agents advertise this as `recipe.image.pull.v1`. A Spark whose agent does not
is shown with an "upgrade the Spark agent" blocker
(`run-switch.agent-upgrade-required`, `install.agent_upgrade_required`) instead
of being sent work it cannot do.
