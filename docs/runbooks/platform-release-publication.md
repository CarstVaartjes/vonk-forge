# Platform release publication

Vonk Forge publishes one immutable release from a signed tag. The release contains
digest-pinned API and worker images, the rendered production Compose file, a
content-addressed control deployment bundle, and native `arm64` and `amd64`
Spark agent packages with checksums, SBOMs, provenance, and Sigstore bundles.

The NAS runtime is upgraded through Docker Compose:

```sh
docker compose pull
docker compose up -d --wait --remove-orphans
```

Spark nodes use the architecture-specific `vonk-forge-agent` Debian package.
There is no controller-managed host updater, platform update API, generation
selector, rollback slot, or offline wheel bundle.

## CI authority boundary

The release workflow:

1. validates the signed release tag;
2. promotes the exact tested API and worker images;
3. builds and publishes the OCI deployment bundle;
4. builds and verifies both native agent packages;
5. creates an immutable GitHub Release containing the Compose and package assets;
6. advances image aliases only after verifying that release evidence.

The deployment bundle publisher has only OCI-registry authority. It validates the
canonical release manifest and bundle descriptors before uploading the exact
config, layer, and manifest blobs.

## Local verification

```sh
uv run --frozen scripts/build-control-deployment-bundle \
  --source-root deploy/compose \
  --output control-deployment.tar

uv run --frozen scripts/publish-control-deployment-bundle describe \
  --bundle control-deployment.tar \
  --repository ghcr.io/OWNER/vonk-forge-control-deployment
```

Publication itself is performed by CI with a trusted `oras` executable and
registry credentials. Operators should consume release assets rather than
rebuilding or selecting an alternate control generation on the NAS.
