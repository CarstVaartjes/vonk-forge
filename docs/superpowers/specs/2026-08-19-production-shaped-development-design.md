# Production-Shaped Development Deployment

## Goal

Make the development deployment use the same Compose service topology and
runtime contracts as production. Development and production are mutually
exclusive release channels for a deployment, not concurrent environments on
one network. The only runtime selection is the published GitHub Actions image
and version; credentials, PKI, data, endpoints, volumes, and service behavior
follow the same deployment contract.

## Requirements

1. Development must not build local images, use `dev-local` image tags, inject a
   local source-origin repository, or bypass GitHub Actions image publication.
2. Development and production must share one production-shaped Compose service
   graph and the same runtime inputs. They must not use separate secret/state
   roots in the deployed channel.
3. Development must include the production control bootstrap, signers,
   registry, Prometheus, Grafana, LiteLLM, Caddy, and Tailscale service
   boundaries. Development-only cohort/bootstrap preparation may remain as
   initialization stages, but cannot replace production runtime services.
4. Both modes must use the same Step CA-backed agent PKI contract, including
   server trust, client trust, certificate issuance, renewal, and health
   checks. The selected channel uses the deployment's configured Step CA
   authority and state.
5. The development wrapper must pull the immutable image references selected by
   the GitHub Actions development channel. It must not clone the source into a
   runtime repository volume or substitute local images.
6. Development must use the same credential, PKI, data, endpoint, and volume
   contract as production. Development and production are never concurrent on
   one network; switching channels is an operator-selected deployment change.
7. Existing production security boundaries must remain intact: network
   segmentation, secret projection, non-root service identities, firewall
   behavior, and fail-closed TLS/mTLS checks.
8. Tests must assert topology parity and that the development wrapper consumes
   published image references without local build/source-origin behavior.
9. Documentation must describe one deployment model with development and
   production as release channels. The selected published image/version is the
   only runtime input that differs.

## Non-goals

- Running development and production concurrently on one network.
- Changing the production release or image-publication workflow beyond the
  inputs required to make the development deployment consume its published
  channel.
- Changing the selected image/version release workflow.

## Acceptance criteria

- The development Compose configuration contains the production runtime
  services and production service names/configuration contracts.
- `scripts/dev-compose` starts the published development channel without local
  image builds or source-origin injection.
- A development node can enroll and renew through the same Step CA protocol
  exercised by production.
- Compose/configuration contract tests pass for both modes.
- The development and production documentation identifies the selected
  published image/version as the only deployment input that differs.
