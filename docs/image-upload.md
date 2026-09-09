# Recipe image archive uploads

Spark-to-Controller image transfer uses authenticated `HEAD` and `PUT` on
`/agent/v1/recipe-builds/{build_id}/image`. Both methods validate the same
Pydantic header contract: image digest, archive digest, complete image byte
length (`x-vonk-image-bytes`), and optional upload offset (defaults to zero).
`HEAD` returns `x-vonk-upload-offset` and `x-vonk-upload-complete`; their schema
is derived from the Pydantic status model in the full OpenAPI document.

A retry queries the Controller cursor, seeks the local archive, and sends only
its remaining bytes. `Content-Length` describes that remaining body, while
`x-vonk-image-bytes` describes the full archive. Partial files are isolated by
node, build, image identity, archive identity, and total length; an exclusive
file lock prevents concurrent writers. Interrupted transfers retain their
partial bytes. Completion validates the complete archive and commits build
metadata once. There are no database transactions or forced disk syncs per
network fragment. Upload progress is sampled into the existing typed operation
heartbeat once per second, independently of streaming the archive.

Deploy the Controller and Spark agent updates together. The current upload
contract requires the complete-image length header and supports resumable
HEAD/PUT; there is no legacy upload fallback. Upgrade agents through the
Controller-managed signed upgrade path before starting a new image build.
