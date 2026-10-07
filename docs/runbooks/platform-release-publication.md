# Platform release publication

Vonk Forge publishes one immutable release from a signed tag. The release contains
digest-pinned API, worker, Hermes, and LiteLLM images, the rendered production
Compose file, and the native `arm64` Spark agent
package with checksums, SBOMs, provenance, and Sigstore bundles. A signed
installer-channel manifest binds those assets to the stable curl endpoints.
The same signed candidate includes a source-stamped `vonkctl` wheel; operators
[check and install it explicitly](../operators/cli-updates.md).

Operators prepare a NAS upgrade by rerunning the same stable installer from the
directory containing the existing bundle:

```sh
curl -fsSL https://install.vonkforge.ai/nas | sudo sh
```

Until the first stable release exists, a development promotion also writes the
unqualified `/nas`, `/spark` and `/vonkctl` endpoints (bound to the dev channel)
so those URLs do not 404. The stable promotion replaces them, and a later dev
promotion never touches them once `artifacts/stable/current.manifest` exists.

They then transfer the refreshed three-entry directory and redeploy the existing
Compose project through approved host access. Prefer the configured headless CLI
and follow the [NAS redeployment procedure](operator-cli-access.md#nas-compose-redeployment);
the NAS Docker UI is not a required authentication or deployment step. The signed
installer remains the supported NAS upgrade entry point.

Spark nodes use the architecture-specific `vonk-forge-agent` Debian package.
For an enrolled, online fleet, the authenticated Fleet upgrade action (or
`vonkctl fleet upgrade`) previews the current signed package and rolls it out
through the existing agent relay without SSH. The default one-at-a-time
strategy requires the exact new agent and helper activation evidence before it
queues the next Spark. Rerunning the Spark installer remains the package repair,
fresh-install, and explicit re-enrollment path; there is no A/B rollback slot or
offline Spark rollback bundle.

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

The `Release` workflow is the only image-channel promotion workflow. Its promotion
job records the channel, source commit, generation, and four image digests in the
Actions summary and uploads a receipt. Failed promotion attempts restore existing
image aliases where possible. Registry tags and the installer pointer are separate
writes, so promotion is not atomic; first publication cannot safely remove an alias
that had no prior value. Rerun a failed job to reconcile an already accepted set.

Builds and independent test suites remain parallel. Jobs that mutate the same
channel share a concurrency group and use `queue: max`, so they run one at a time
and retain up to 100 waiting jobs rather than replacing the previous waiter.
A newer `main` push never interrupts an active publication. This queues
Actions work; it does not block PR merges or serialize the entire pipeline.
Stale development sources are rejected before publication.

Development producers build and validate each image once. A release run builds
a producer only when no ancestor release produced it from unchanged inputs, and
otherwise reuses that release's artifacts. Publication starts after every
producer in the run has settled. Daily manifest refresh renews the existing
accepted generation's expiry without changing images. The
[release workflow](../operations/agent-package-release.md#the-release-workflow)
describes the run in full.

PRs expose one always-running `CI gate` that checks every selected suite result,
including selector failures. Deleted files participate in area selection.
Repository and control shards use recent successful CI timing artifacts; missing,
stale, or invalid timings fall back to collection counts. Timing data affects only
assignment, never whether a test is selected.

## Local verification

```sh
scripts/verify-supply-chain --json
uv run --project control --frozen \
  pytest -q tests/scripts/test_install_release_publication.py
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

## Deployed runtime authority and client compatibility

`vonkctl --json platform` reads the authenticated `/api/platform` contract. Its
API source and Control contract fingerprint come from the API's installed
package. Each worker reports its own installed source and worker contract in a
process-bound heartbeat; only completed loops within the last 30 seconds appear
as fresh observations. A restarted process must complete a loop before it is
observable. Missing, damaged, or old package metadata remains nullable. No fresh
worker yields `worker-observation-unavailable`; fresh workers with unknown
package provenance yield `worker-provenance-unavailable`. Different fresh worker
sources remain separate observations, rather than one guessed Controller version.

The existing packaged build-identity record also binds the CLI's shipped Control
schema and the worker wire schema. `vonkctl --json --version` reports its own
source and Control fingerprint. Signed client updates verify the wheel identity,
shipped contract, artifact digest, and signed release descriptor. Installation
requires a current authenticated API observation with known source and the same
Control fingerprint; an unavailable or different deployed contract retains the
installed client and reports the unresolved compatibility condition. Publication
acceptance and actual API deployment are separate authorities. An unchanged image
reused by a new publication retains its own producer source.

The heartbeat schema change adds two nullable telemetry columns (`source_sha`
and `worker_contract_sha256`) through the existing startup schema reconciliation
and advisory lock. It does not reset process rows, operation state, claims,
requests, or volumes. Existing rows are unknown until their owning upgraded worker
records its package identity and completes a loop. NAS acceptance verifies the
running API package against its authenticated observation and each fresh worker
against the worker's installed package, separately from the publication envelope.

Recovery consumers must retain the request identity, attempt history, cause,
evidence, and cooldown when publication changes. An early retry requires a typed
fault owner and a proven relevant change in that owner's deployed fingerprint.
A new accepted client publication is not evidence that the Controller, worker,
agent, resource condition, or recipe artifact changed. Unknown ownership and
unknown or stale provenance continue through normal scheduled recovery.

### Managed CA image closure

The current installer graph requires the managed `ca` image alongside API,
worker, Hermes and LiteLLM. The CA build uses the reviewed Smallstep source
archive and signing-context patch, pinned Go toolchain, service module locks,
and existing Smallstep runtime base. Its isolated build-input fingerprint binds
those files and the acceptance policy. Reuse requires unchanged inputs on an
ancestor source, signed hosted build provenance, and verified runnable manifests
for both Linux architectures.

The development producer deep-scans the OCI archive and binds its runnable
manifests to the published registry digest before producing a CA receipt.
Installer assembly requires that fifth image identity; the signed release and
NAS payload bind its exact digest. Compose channel rendering preserves that
CA digest, including during NAS installer preparation. No third-party CA image
or mutable CA alias is a replacement for missing accepted evidence.

An upgrade changes the executable of the existing `step-ca` service, with the
canonical `--config` and `--password-file` arguments. It preserves the TLS
listener, root/intermediate trust, secret mounts, keys and `step-ca-data` volume.
Keep the existing bundle `.env`, secrets and named volumes during deployment.
Repository or image acceptance does not itself redeploy a running NAS.

Controller enrollment and rotation commit their exact issuance claims before
provider HTTP, then conditionally persist the observed bound result in a short
transaction. PostgreSQL row/advisory locks own concurrent claims; unrelated
node requests share no process lock during provider waits. The local SQLite
fixture guard covers SQL transactions only. Lost responses and process death
retain the same provider journal binding, serial and generation for adoption.
