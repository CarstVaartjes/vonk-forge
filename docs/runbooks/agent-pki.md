# Agent PKI

The Controller signs agent enrollment and renewal certificates locally using
`pki/local_ca.py`. Certificates retain the existing root and intermediate and the
fixed thirty-day client certificate policy. Enrollment and renewal issue in the
API; the worker also needs the signer for issuance and revocation recovery.
Both run as UID/GID 10001 and read the secrets the `runtime-init` helper
staged for them.

The NAS installer creates and preserves these secret files:

- `step-ca/root-certificate`
- `step-ca/intermediate-certificate`
- `step-ca/intermediate-key` (encrypted Ed25519 PKCS#8)
- `step-ca/root-key` (the root key, encrypted the same way; empty on installs
  created before it was kept)
- `step-ca-password`
- `controller-server-certificate` and `controller-server-key`
- `agent-ca-provisioner-public-jwk` (the public issuer identity used in accepted certificate bindings)

The installer issues the controller server leaf (`controller-server-*`, 397
days) and names its hosts. `runtime-init` renews it on every start into the
`controller-tls` volume that Caddy serves from (`controller-server.pem`, the
chain and its key in one file), once 60 days or fewer remain, keeping the
installer leaf's names (see
[Runtime staging](../../deploy/compose/README.md#runtime-staging-runtime-init)).
Sparks and Lab browsers trust the root, so a renewed leaf needs no
re-enrollment. `vonk_controller_certificate_expiry_timestamp_seconds` reports
when the served leaf, the intermediate and the root expire, and
`ControllerCertificateExpiring` (60 days) and
`ControllerCertificateExpiringSoon` (30 days) alert on each; see
[Controller certificate expiring](observability.md#controller-certificate-expiring).

The names under `step-ca/` are retained to reuse the installed authority. The
root key never enters Compose or a container; only the installer reads it, to
renew the intermediate. The installer no longer generates a JWT
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

The installer normalizes older encrypted Ed25519 keys to PKCS#8 v1 during
upgrade, preserving the private key and password. It validates the key against
the intermediate certificate before atomic publication and leaves canonical
keys unchanged. The Controller only loads keys and never rewrites these secrets.
If certificate signing reports
`capability.ca_key_encoding_unsupported_rerun_nas_installer`, re-run the signed
NAS installer and redeploy. Other Controller capabilities remain available;
certificate signing recovers through its normal capability retry.

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

## Intermediate renewal

The intermediate is valid for five years and the root for ten. When fewer than
365 days remain, an installer run re-signs the same intermediate key with the
root key: subject, key and subject key identifier stay, so every certificate it
already issued keeps verifying. An expired intermediate heals the same way. The
new intermediate is capped at the root's expiry, and the controller certificate
bundle is rebuilt with it. Redeploy after the run. Agents present the
intermediate in their chain and replace that chain at their next 30-day
renewal, well before the old intermediate expires.

Installs created before the root key was kept have an empty `step-ca/root-key`
and cannot renew. From 365 days before the intermediate expires, the installer
says so on every run. Plan a new PKI and re-enroll the Sparks before it
expires. Root renewal is not automated; the root outlives the first
intermediate by five years.

## Backup and recovery

Back up `secrets/` securely: it holds the encrypted root and intermediate keys
and their password. A suspected compromise of `secrets/` means a new PKI and
re-enrollment of every Spark; renewal re-signs the same key and is no
compromise recovery. The local
issuance journal and revocations are in the ordinary PostgreSQL backup, so no
Badger volume archive is needed. Restore PostgreSQL and the matching authority
secrets together; see [PostgreSQL backups](../postgres-backups.md). Verify
Controller health and enrollment before resuming workloads. Never generate a
replacement intermediate as a workaround for missing secrets.
