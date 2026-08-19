# Production-Shaped Development Deployment

## Goal

Make the development deployment use the same Compose service topology and
runtime contracts as production. Development may use separate synthetic
credentials, PKI state, and disposable data, but it must consume the same
published GitHub Actions images and exercise the same service boundaries,
health checks, certificate provider, and renewal behavior.

## Requirements

1. Development must not build local images, use `dev-local` image tags, inject a
   local source-origin repository, or bypass GitHub Actions image publication.
2. Development and production must share one production-shaped Compose service
   graph. Mode-specific values must come from environment/configuration and
   separate secret/state roots, not from a second runtime topology.
3. Development must include the production control bootstrap, signers,
   registry, Prometheus, Grafana, LiteLLM, Caddy, and Tailscale service
   boundaries. Development-only cohort/bootstrap preparation may remain as
   initialization stages, but cannot replace production runtime services.
4. Both modes must use the same Step CA-backed agent PKI contract, including
   server trust, client trust, certificate issuance, renewal, and health
   checks. Development uses a separate disposable Step CA authority.
5. The development wrapper must pull the immutable image references selected by
   the GitHub Actions development channel. It must not clone the source into a
   runtime repository volume or substitute local images.
6. Development data and credentials must remain isolated from production. A
   development reset may remove development volumes without affecting
   production state.
7. Existing production security boundaries must remain intact: network
   segmentation, secret projection, non-root service identities, firewall
   behavior, and fail-closed TLS/mTLS checks.
8. Tests must assert topology parity and that the development wrapper consumes
   published image references without local build/source-origin behavior.
9. Documentation must describe one deployment model with development and
   production inputs, instead of presenting development as a separate runtime
   architecture. Hostnames, management CIDRs, published ports, service names,
   network mappings, and secret projection contracts are shared; only the
   selected published image/version and isolated credential, PKI, and data
   values differ.

## Non-goals

- Sharing production credentials, PKI private keys, databases, or persistent
  volumes with development.
- Changing the production release or image-publication workflow beyond the
  inputs required to make the development deployment consume its published
  channel.
- Replacing the existing development data reset and synthetic-credential
  conveniences.

## Acceptance criteria

- The development Compose configuration contains the production runtime
  services and production service names/configuration contracts.
- `scripts/dev-compose` starts the published development channel without local
  image builds or source-origin injection.
- A development node can enroll and renew through the same Step CA protocol
  exercised by production.
- Compose/configuration contract tests pass for both modes.
- The development and production documentation identifies the selected
  published image/version and isolated credential, PKI, and data values as the
  only deployment inputs that differ; endpoint and network mappings are shared.
