# Refresh the pinned container images

Every upstream image this platform builds or runs is fixed by an immutable tag
**and** an OCI index digest. This runbook is the manual procedure for a human
refreshing those pins deliberately. It is not a CI job and there is no
scheduled refresh: a moving upstream tag must never change what runs without a
reviewed edit.

`deploy/compose/images.lock.json` is the single inventory. Its `images` mapping
holds the Compose runtime images and its `build_bases` mapping holds the images
the release Dockerfiles build `FROM`. `scripts/verify-supply-chain` rejects any
entry that is not of the form `NAME[:TAG]@sha256:<64 hex>` and binds the lock
into `inventory/sbom/manifest.json`.

## The pins and where they live

Update **every** file listed for a pin; several tests assert the exact
reference and will fail if only the lock changes.

| Lock key | Upstream | Files that carry the reference |
| --- | --- | --- |
| `images.caddy` | `caddy` | `deploy/compose/images.lock.json`, `deploy/compose/compose.yaml`, `deploy/compose/.env.example` |
| `images.grafana` | `grafana/grafana` | `deploy/compose/images.lock.json`, `deploy/compose/compose.yaml`, `deploy/compose/.env.example` |
| `images.postgres` | `postgres` | `deploy/compose/images.lock.json`, `deploy/compose/compose.yaml`, `deploy/compose/.env.example`, `.github/workflows/ci.yml` (integration `docker pull`) |
| `images.prometheus` | `prom/prometheus` | `deploy/compose/images.lock.json`, `deploy/compose/compose.yaml`, `deploy/compose/.env.example` |
| `images.registry` | `registry` | `deploy/compose/images.lock.json`, `deploy/compose/compose.yaml`, `deploy/compose/.env.example` |
| `images.step-ca` | `smallstep/step-ca` | `deploy/compose/images.lock.json`, `deploy/compose/compose.yaml`, `deploy/compose/.env.example`, `control/tests/test_step_ca.py` |
| `images.tailscale` | `tailscale/tailscale` | `deploy/compose/images.lock.json`, `deploy/compose/tailscale/compose.yaml`, `deploy/compose/.env.example`, `deploy/compose/tests/test_agent_ingress.py`, `deploy/compose/tests/test_networking.py` |
| `build_bases.hermes` | `nousresearch/hermes-agent` | `deploy/compose/images.lock.json`, `deploy/compose/hermes-agent/Dockerfile`, `tests/scripts/test_verify_supply_chain.py` |
| `build_bases.litellm` | `ghcr.io/berriai/litellm` | `deploy/compose/images.lock.json`, `deploy/compose/litellm/Dockerfile` (both `FROM` lines), `deploy/compose/.env.example` |
| `build_bases.node` | `node` | `deploy/compose/images.lock.json`, `control/Dockerfile` (`ARG NODE_IMAGE`) |
| `build_bases.python` | `python` | `deploy/compose/images.lock.json`, `control/Dockerfile` (`ARG PYTHON_IMAGE`) |
| `build_bases.skopeo` | `quay.io/skopeo/stable` | `deploy/compose/images.lock.json`, `control/Dockerfile` (`SKOPEO_IMAGE` plus the three `*_DIGEST` args and labels), `scripts/verify-controller-skopeo`, `control/tests/test_controller_image_packaging.py`, `tests/scripts/test_verify_controller_skopeo.py` |
| _(no lock key — developer lane)_ | `ubuntu` | `scripts/dev-agent-wire-linux.Dockerfile` (`FROM`, pinned by index digest) |

`build_bases.litellm` and `build_bases.hermes` are the upstream bases of the
patched `vonk-forge-litellm` and `vonk-forge-hermes` release images; see
[Patched release images](#patched-release-images-are-a-different-flow).

### Skopeo pin details that bite

Docker accepts `NAME:TAG@sha256:…` for `FROM` and `docker pull`, but skopeo
refuses it with `Docker references with both a tag and digest are currently not
supported`. Keep the tagged-plus-digested form as the pin (`build_bases.skopeo`
and `ARG SKOPEO_IMAGE`) and use the bare repository plus index digest whenever a
reference is handed to skopeo:

- `control/Dockerfile` resolves `docker://quay.io/skopeo/stable@${SKOPEO_INDEX_DIGEST}`.
- `scripts/verify-controller-skopeo` takes the tagged-plus-digested source
  reference, asserts it equals the reviewed pin, and reduces it to
  `quay.io/skopeo/stable@<index digest>` before any `skopeo` call.

Do not "helpfully" add the tag to those skopeo invocations.

The upstream filesystem layout can also differ between rebuilds of the same
version. Every `-immutable` tag ships `/etc/ssl/certs` as a symlink into
`/etc/pki/tls`, while the superseded `stable` rebuild shipped a real directory
there. `control/Dockerfile` therefore removes the Python base's
`/etc/ssl/certs` directory before copying the skopeo `/etc/ssl` tree; `COPY`
cannot replace a directory with a symlink. If a future refresh changes that
layout again, the control image build in verification step 4 fails — adjust the
`rm -rf` line, do not drop the `COPY`.

### Known pins outside the lock

These are deliberately not in `images.lock.json` and the checker does not scan
them, but a refresh that touches their upstream must update them too:

- `docker.io/tonistiigi/binfmt@sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0`
  — the QEMU emulation image in `.github/workflows/ci.yml`; a CI tool input,
  not a deployment or build base.
- `deploy/compose/tests/test.env` is a test fixture. Most of its image values
  are obvious fakes (`@sha256:aaaa…`); only its `TAILSCALE_IMAGE` mirrors the
  real pin, and no test compares it to the lock.

## The rule: tag plus digest, never a moving tag

A tag is a mutable pointer owned by the publisher; a digest is a
content-addressed identity owned by the content. Pin `NAME:TAG@sha256:<index>`
for both properties:

- **Keep the tag** so the reference is human-readable and the provenance is
  obvious in diffs and SBOMs.
- **Keep the digest** so the build is exact and reproducible even if the tag
  moves.
- **Never pin a floating tag alone** (`stable`, `latest`, `main`, `edge`).
  Compose defaults must carry a digest; `scripts/verify-supply-chain` rejects
  Compose `:latest`, `:main`, and `:edge` defaults without one.

Digest pinning is not enough on its own if the tag is floating and later
advances: registries garbage-collect superseded indexes, and the recorded digest
then stops resolving even though the tag still exists. A digest only resolves as
long as the registry still holds the manifest blob; a digest recorded against a
tag the publisher subsequently moves is not durable. That is why the controller
no longer follows `quay.io/skopeo/stable`: it now pins the publisher's
`v1.22.2-immutable` tag together with the digest. Treat `-immutable` and full
SemVer release tags as safe; treat rolling tags (`24-bookworm-slim`,
`3.12-slim-bookworm`) and even some version tags that publishers re-push as
mutable and re-check them when you refresh.

## Find the current candidate

Public images need no credentials; `skopeo list-tags` and the registry HTTP APIs
below are read-only. If `skopeo` is not on the host, run the pinned copy:

```bash
SKOPEO="quay.io/skopeo/stable:v1.22.2-immutable"
docker run --rm "$SKOPEO" list-tags docker://docker.io/library/node
```

List tags per registry:

```bash
# Docker Hub (library/* and namespaced), including older pages
curl -fsSL 'https://hub.docker.com/v2/repositories/library/node/tags?page_size=100&name=24-bookworm' \
  | python3 -m json.tool
# Quay
curl -fsSL 'https://quay.io/api/v1/repository/skopeo/stable/tag/?onlyActiveTags=true&limit=100' \
  | python3 -m json.tool
# GHCR has no anonymous tag-list HTTP API; use skopeo list-tags
docker run --rm "$SKOPEO" list-tags docker://ghcr.io/berriai/litellm
```

Read the index digest and every per-architecture child. `--raw` prints the exact
manifest bytes, so their SHA-256 **is** the recorded index digest:

```bash
reference=quay.io/skopeo/stable:v1.22.2-immutable
docker buildx imagetools inspect "$reference"                 # human summary
docker buildx imagetools inspect --raw "$reference" | sha256sum   # must equal the digest below
docker buildx imagetools inspect --raw "$reference" | python3 -m json.tool
docker buildx imagetools inspect --raw "$reference" \
  | python3 -c 'import json,sys
for m in json.load(sys.stdin)["manifests"]:
    print(m["platform"]["os"] + "/" + m["platform"]["architecture"], m["digest"])'
```

Use the **index** digest (the digest of the whole manifest list) for the lock
and `SKOPEO_IMAGE`; use the per-architecture child digests only where the
Dockerfile and `scripts/verify-controller-skopeo` check them.

## Refresh procedure

1. Pick the candidate tag with the tag list above. Reject any floating tag.
2. Record the index digest and, for Skopeo, the `amd64` and `arm64` child
   digests with the `--raw` commands above.
3. Update the `images.lock.json` entry to the full
   `NAME:TAG@sha256:<index>` reference.
4. Update every deployment input listed in the table above. Do not change any
   surrounding Compose service, environment variable name, health check, or
   build stage.
5. Update the tests listed in the table, then the annotations in
   `scripts/verify-controller-skopeo` (pinned source reference, both expected
   child digests, and the index label check).
6. Regenerate the bound evidence:
   `uv run --frozen python scripts/verify-supply-chain --write-manifest`.
7. Run the verification below and review `git diff`.

## Verify

```bash
# 0. The lock and the deployment inputs still agree, and every recorded digest
#    still resolves. `moved` and `unverified` findings are printed but do not
#    fail; `missing` and `unlisted` do.
scripts/check-image-pins

# 1. Re-derive the digest you recorded.
docker buildx imagetools inspect --raw "quay.io/skopeo/stable:v1.22.2-immutable" | sha256sum

# 2. Offline supply-chain gate, and confirm the regenerated manifest is current.
scripts/verify-supply-chain --json

# 3. Pin-assertion tests (fast).
pytest -q tests/scripts/test_verify_controller_skopeo.py
pytest -q control/tests/test_controller_image_packaging.py

# 4. A digest that resolves is not proof the image works. Build the control
#    image and run the two lane tests that pull and execute the packaged
#    Skopeo. These are the tests that failed when the old pin rotted.
UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache \
  uv run --project control --frozen --with-editable . pytest -q \
  "control/tests/security/test_no_routine_ssh.py::test_built_worker_image_contains_no_direct_transport_executable" \
  "control/tests/security/test_agent_protocol.py::test_root_context_image_installs_contracts_and_protocol_from_build_inputs" \
  -m lane
```

`scripts/check-image-pins` is read-only. It resolves every lock entry
(`name:tag` and `name:tag@digest`), compares the tag's current digest with the
record and verifies the recorded digest still resolves, then scans
`control/Dockerfile`, `deploy/compose/.env.example`, the Compose files,
`deploy/compose/hermes-agent/Dockerfile`, `deploy/compose/litellm/Dockerfile`,
and `scripts/verify-controller-skopeo` for digest-pinned references that are
absent from the lock. It reads registries and nothing else and needs
`docker buildx`.

It runs in two places, and neither one refreshes anything:

- the Compose integration job, when a pull request changes the deployment
  inputs, so a pin change is verified in the change that makes it;
- the weekly **Image pins** workflow (`.github/workflows/image-pins.yml`, also
  dispatchable by hand), because a tag can move or a digest can be garbage
  collected upstream without any commit here.

Findings decide the exit status by kind. `missing` means the recorded digest is
gone and every image build will fail until the pin is refreshed. `unlisted`
means a pinned image is used but absent from the inventory; add it to the lock
in the matching section and add its file to the checker's `SCAN_PATHS`. Both
exit non-zero. `moved` means the recorded digest is still valid but the tag now
points elsewhere — expected for a rolling tag, so decide whether to refresh it
rather than treating it as a failure. `unverified` means the registry could not
be read at all, which is a network condition and never a reason to block a
merge.

## Patched release images are a different flow

Bumping `build_bases.litellm` or `build_bases.hermes` changes only the upstream
**base** of the patched image:

- `deploy/compose/litellm/Dockerfile` builds a Prisma- and route-activation-
  patched image from `ghcr.io/berriai/litellm`.
- `deploy/compose/hermes-agent/Dockerfile` builds a NAS-identity-patched image
  from `nousresearch/hermes-agent`.

The result is published as `ghcr.io/carstvaartjes/vonk-forge-litellm` and
`ghcr.io/carstvaartjes/vonk-forge-hermes`. Publishing those patched images is a
separate release flow from bumping an upstream pin: it builds the patched
Dockerfile targets from an exact release commit, resolves and verifies the
digests of all four release artifacts together, signs the LiteLLM image against
the checked-in key `deploy/compose/trust/litellm-cosign.pub` from an exact
upstream commit, and exposes the resulting digest-pinned references to
operators. Never treat a new upstream `litellm` or `hermes-agent` tag as a
published Vonk image, and never point `LITELLM_IMAGE` or `HERMES_AGENT_IMAGE` at
an upstream tag. See [Verify the platform and workload supply
chains](supply-chain.md) and [Platform release
publication](platform-release-publication.md).
