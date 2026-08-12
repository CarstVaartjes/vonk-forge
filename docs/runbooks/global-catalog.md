# Global catalog import and publication

Local PostgreSQL remains authoritative. The controller does not depend on
vonkforge.ai for startup, readiness, installation, running, or an already
imported recipe.

The global catalog is a future optional service. The initial release runs fully
on the NAS/GPU nodes; when enabled later, its frontend belongs on Cloudflare Pages
and its API/validation worker/PostgreSQL backend may run on Railway.

## Import an immutable public recipe

1. Copy the `vonk://catalog/PUBLISHER/SLUG@sha256:DIGEST` URI from
   vonkforge.ai.
2. In **Recipe catalog**, paste it under **Import from vonkforge.ai**.
3. Review the source-bundle digest, Dockerfile policy report, artifact
   revisions, disk, memory, and topology. A published recipe does not require
   a pre-built workload image or a community registry.
4. Choose **Import exact revision**. The controller fetches the immutable
   revision and its digest-bound source bundle again, verifies the schema,
   bundle digest, and canonical hash, then writes an independent resolved
   revision and provenance rows to local PostgreSQL.

The client uses a fixed HTTPS origin, no ambient proxy credentials, no
redirects, strict timeouts, and a 512 KiB response limit. Set
`VONK_GLOBAL_CATALOG_URL` only to an HTTPS origin; plain HTTP is accepted solely
for an explicit loopback development server.

## Publish without storing global credentials

1. Run the recipe locally on its declared node count. Vonk selects one builder,
   validates the source bundle, builds the Dockerfile once in the rootless
   isolated builder, exports the resulting OCI image as a Docker-loadable
   archive, and transfers that exact archive to the mapped nodes. Capture a v1
   JSON test report bound to the exact local recipe hash,
   source-bundle digest, and resulting image digest. It must record successful
   `container.started`, `endpoint.healthy`, and `inference.completed` checks.
2. Open the resolved local recipe and attach that JSON under **Publish through
   vonkforge.ai**. Vonk validates the schema and bindings but labels it
   publisher-submitted evidence, not Vonk certification.
3. Enter the exact publisher namespace you will choose after OAuth. Download
   the publication JSON. Export normalizes only that publisher field and binds
   the exported report to the resulting canonical hash.
4. Open `https://vonkforge.ai/publish`, sign in using a supported OAuth
   provider, choose the same namespace, and upload the JSON.
5. The global service creates a private draft, validates the recipe, source
   bundle, Dockerfile policy, and submitted evidence, and requires an explicit
   final publication confirmation. It does not build the image or store image
   layers.

The export contains exactly `recipe`, its source-bundle reference, and
`test_report`. It never contains container layers, model weights, registry
credentials, local hostnames, node inventory, prompts/responses, tailnet
details, or OAuth credentials.
