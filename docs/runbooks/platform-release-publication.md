# Platform release publication

Vonk Forge publishes one immutable release from a signed tag. The release contains
digest-pinned API and worker images, the rendered production Compose file, a
digest-pinned optional Hermes image, and the native `arm64` Spark agent
package with checksums, SBOMs, provenance, and Sigstore bundles. A signed
installer-channel manifest binds those assets to the stable curl endpoints.

Operators prepare a NAS upgrade by rerunning the same stable installer from the
directory containing the existing bundle:

```sh
curl -fsSL https://install.vonkforge.ai/nas | sh
```

They then upload the refreshed three-entry directory and redeploy it with the
NAS Docker UI. This is the only supported NAS upgrade entry point.

Spark nodes use the architecture-specific `vonk-forge-agent` Debian package.
For an enrolled, online fleet, the authenticated Fleet upgrade action (or
`vonkctl fleet upgrade`) previews the current signed package and rolls it out
through the existing agent relay without SSH. The default one-at-a-time
strategy requires the exact new agent and helper activation evidence before it
queues the next Spark. Rerunning the Spark installer remains the package repair,
fresh-install, and explicit re-enrollment path; there is no A/B rollback slot or
offline wheel bundle.

## CI authority boundary

Normal main-branch publication updates only the development channel (`:dev`).
Production (`:latest`) advances only for an explicit signed version tag, after the
candidate passes NAS and ARM64 Spark installer acceptance. Public upstream
`:latest` tags are controlled by their publishers and may change independently
between acceptance and a later pull; our production release gate cannot freeze
those images.

The release path:

1. verifies signed tag authority and creates immutable image/package artifacts;
2. assembles and signs an immutable installer candidate;
3. runs NAS and Spark acceptance against the candidate Vonk image digests using
   a temporary Compose overlay, while checking that the installed Compose remains
   on floating channels;
4. verifies the complete signed acceptance receipt, published candidate objects,
   current source authority, and the existing channel pointer;
5. advances all four Vonk image aliases, rechecks authority, then publishes the
   signed installer pointer last.

`Installer publication` is the only image-channel promotion workflow. Its promotion
job records the channel, source commit, generation, and four image digests in the
Actions summary and uploads a receipt. Failed promotion attempts restore existing
image aliases where possible. Registry tags and the installer pointer are separate
writes, so promotion is not atomic; first publication cannot safely remove an alias
that had no prior value. Rerun a failed job to reconcile an already accepted set.

Builds and independent test suites remain parallel. Jobs that mutate the same
channel share a concurrency group and use `queue: max`, so they run one at a time
and retain up to 100 waiting jobs rather than replacing the previous waiter.
Installer workflow completion events cannot interrupt an active publication.
This queues Actions work; it does not block PR merges or serialize the entire
pipeline. Stale development sources are rejected before publication.

Development producers build and validate each image once. Producer-completion
events resolve exact successful runs, or successful ancestor runs whose build
inputs have not changed. Missing producers leave publication pending without a
runner polling loop; their completion events retry readiness. Daily manifest
refresh renews the existing accepted generation's expiry without changing images.

PRs expose one always-running `CI gate` that checks every selected suite result,
including selector failures. Deleted files participate in area selection.
Repository and control shards use recent successful CI timing artifacts; missing,
stale, or invalid timings fall back to collection counts. Timing data affects only
assignment, never whether a test is selected.

## Local verification

```sh
scripts/verify-supply-chain --json
uv run pytest -q tests/test_installer_publication_workflow.py
```

Publication is performed by CI. Operators consume the stable or development
curl endpoint; there is no second bundle registry, local build, or alternate
control generation to select.


Deployment Compose follows channels: Vonk images use `:dev` on development
and `:latest` on production; every upstream service uses `:latest`. All services
use `pull_policy: always`, so starting/redeploying the project checks the registry.
Already-running containers do not update themselves. Upstream major releases may
require operator migration, particularly PostgreSQL data directories.

The NAS curl bootstrap verifies and passes the published payload to the native
installer. Both fresh installs and reruns replace `docker-compose.yaml` with this
channel policy while preserving operator configuration, secrets, and named volumes.
Immutable image records and `docker-compose.pinned.yml` are publication evidence
inputs; the payload builder converts every image to the deployment channel before
embedding Compose. They are not the installed deployment configuration.
