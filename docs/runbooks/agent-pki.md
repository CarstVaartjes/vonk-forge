# Agent PKI

The Controller signs agent enrollment and renewal certificates locally using
`local_ca.py`. Certificates retain the existing root and intermediate and the
fixed thirty-day client certificate policy. Enrollment and renewal issue in the
API; the worker also needs the signer for issuance and revocation recovery.
Both run as UID/GID 10001 after the API's secret-staging pre-exec.

The NAS installer creates and preserves these secret files:

- `step-ca/root-certificate`
- `step-ca/intermediate-certificate`
- `step-ca/intermediate-key` (encrypted Ed25519 PKCS#8)
- `step-ca-password`
- `controller-server-certificate` and `controller-server-key`
- `agent-ca-provisioner-public-jwk` (the public issuer identity used in accepted certificate bindings)

The names under `step-ca/` are retained to reuse the installed authority. The
root private key is never deployed. The installer no longer generates a JWT
signing credential or a CA server configuration. Existing unused files may stay.
Compose mounts the intermediate and password as secrets into the API's staging
process and exposes private normalized copies to the API and worker. They are
never copied into an image or exposed through an HTTP endpoint.

## Upgrade from the CA service

Re-run the signed NAS installer in the directory containing the existing bundle,
then follow the [NAS redeployment runbook](operator-cli-access.md#nas-compose-redeployment).
Preserve `.env`, `secrets/`, PostgreSQL and other named volumes. Regenerated
Compose has no `step-ca` service; remove the stopped orphan service during
redeployment. The old `step-ca-data` volume may remain unused. Already-issued
certificates remain valid because the intermediate is unchanged.

Before serving the first local CRL, signer construction idempotently imports
Controller revocation records into `local_certificate_revocations`. Enrollment
persists revocation intent before invoking the CA: `agent_certificates` retains
accepted certificate revocations, and `agent_issued_certificate_revocations`
retains node-independent late effects, including lost CA replies. Pending
revocation intent is also carried forward. Database failure blocks signer
construction; it cannot produce an empty replacement CRL.

The Controller's identity validator immediately rejects revoked credentials.
The local signer publishes a signed, bounded CRL from PostgreSQL. Replaying an
issuance uses its durable exact binding; a revoked serial cannot be adopted or
used as a renewal source. Inspect Controller API and worker logs for certificate
capability failures and PostgreSQL availability.

## Backup and recovery

Back up `secrets/` securely and retain the offline root separately. The local
issuance journal and revocations are in the ordinary PostgreSQL backup, so no
Badger volume archive is needed. Restore PostgreSQL and the matching authority
secrets together; see [PostgreSQL backups](../postgres-backups.md). Verify
Controller health and enrollment before resuming workloads. Never generate a
replacement intermediate as a workaround for missing secrets.
