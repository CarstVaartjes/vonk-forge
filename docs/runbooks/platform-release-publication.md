# Platform release publication

Vonk Forge publishes one immutable release from a signed tag. The release contains
digest-pinned API and worker images, the rendered production Compose file, a
digest-pinned optional Hermes image, and native `arm64` and `amd64` Spark agent
packages with checksums, SBOMs, provenance, and Sigstore bundles. A signed
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

The release workflow:

1. validates the signed release tag;
2. promotes the exact tested API and worker images;
3. renders production Compose with `:latest` images and `pull_policy: always`;
4. builds and verifies both native agent packages;
5. creates an immutable GitHub Release containing the Compose and package assets;
6. publishes the signed installer-channel manifest and setup binaries; and
7. advances image aliases only after verifying the release assets.

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
