# Image transfer and cache

Registry images are prepared independently from model downloads. The Controller
uses Skopeo to fetch up to six image layers concurrently, with three retries
and exponential backoff for transient errors. Completed OCI blobs are shared
between images and retained for a retry; an interrupted individual layer may
need to restart. Skopeo validates and publishes complete blobs atomically.

The cache lives in `image-cache/registry-layers/` beneath the existing artifact
volume, beside the runnable archives. It survives worker container replacement
and is disposable cache, not PostgreSQL backup data. Different image preparations
run concurrently; an OS file lock serializes writers to the same image index.
Abandoned temporary blobs for that image are removed when its lock is acquired.
Completed shared layers remain until this cache is explicitly cleared, so they
consume disk space in addition to exported archives. Clear the registry-layer
cache only with image preparation stopped; clearing it does not remove final
runnable archives.

A separate observer samples native OCI file sizes once per second while Skopeo
continues transferring. The download count means layer bytes available locally,
including reused blobs; it is not a claim about bytes received over the network.
Only this image's layers and in-progress files are counted. Local archive
creation is a separate `prepare` phase, without an invented download percentage.
Slow progress persistence does not block Skopeo's receive loop.

Completed layers are converted locally into the same Docker archive consumed
by Sparks. No registry push is involved. The NAS-to-Spark distribution endpoint
already supports authenticated HTTP Range, partial files and cache reuse;
archive transfers synchronize storage at completion rather than per fragment.
