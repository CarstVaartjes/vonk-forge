# Exact CA issuance

This executable replaces the existing step-ca executable inside the managed CA
image. It uses the same configured root, intermediate key, provisioner, address,
and Badger directory. It does not create another CA or reset existing data.
Existing valid agent certificates remain valid during rollout; rotation adopts
the verified replacement before changing the active Controller certificate.

The Controller persists the full accepted request before calling the CA. The
request binds the node, complete CSR DER digest, reserved serial, fixed validity,
issuer, provisioner, policy, purpose, source certificate, and generation. A fresh
authentication JWT authorizes every issue or observation call through Smallstep's
normal authorization path. Its one-use JWT ID is independent of the durable
provider request ID.

The CA adds `vonk_issuance_requests` and `vonk_issuance_serials` tables to the
existing Badger directory. A short transaction reserves the request and serial.
Signing happens outside database locks. A second short transaction checks the
attempt epoch and stores the signed leaf, provisioner metadata, and exact receipt
atomically. The pinned Smallstep `x509_certs` and `x509_certs_data` tables retain
their existing format. Failed persistence returns no certificate. Responses read
only the committed receipt and independently verify the attempt identity.

Committed retries return the same certificate DER. An unfinished request resumes
with a new epoch after its lease expires or the exclusive Badger owner restarts.
A displaced worker cannot commit or respond with its locally computed leaf.
Source revocation and commit use the same local lock, so a revoked rotation
source cannot publish a replacement that was still being signed.

Pre-journal Controller rows retain a null request binding. A historically unknown
issuance cannot acquire a fabricated binding, infer absence from an empty journal,
or receive a replacement certificate automatically. Already known valid
certificates are preserved. Only requests accepted and persisted with this exact
contract participate in journal observation and automatic resumption.

The upstream patch preserves context through Authority signing and separates its
internally generated CA TLS certificate from agent issuance. Native unbound sign,
renew, and rekey paths do not bypass the journal. The managed configuration permits
the existing single JWK provisioner and local SoftCAS backend; a different provider
requires a separately verified receipt implementation.

Hosted proof runs the real pinned Authority, SoftCAS, Badger, HTTP handler, and
Controller provider. It checks storage failure, lost responses, restart, concurrent
attempts, authentication reuse, binding changes, and the internal TLS path. Local
development does not need a Go build or a second running CA.
