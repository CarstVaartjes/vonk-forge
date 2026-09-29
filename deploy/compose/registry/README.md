# Operator publication boundary

The standing registry has no host port and agents receive only digest-read
routes through Caddy. Publication requires Docker-daemon administrator access
and a separately reviewed, digest-pinned ORAS publisher image:

Run `deploy/compose/bin/publish-release /absolute/path/to/release` with
`ORAS_PUBLISHER_IMAGE` and `RELEASE_TAG` set. It pushes to the fixed
`vonk/releases` repository, validates the tag and the publisher image's exact
SHA-256 digest, and joins `${COMPOSE_PROJECT_NAME}_registry-publisher`
(`vonk-forge-control` unless set).

`ORAS_PUBLISHER_IMAGE` must contain `@sha256:<64 lowercase hex>` and be
approved through the deployment image process. The publisher network is
internal, is not joined by Caddy, and has no standing publisher service or
credential. Docker-daemon authorization is the operator authentication gate.
After publication, TUF metadata—not the tag—authorizes the exact manifest
digest agents may pull.
