# GPU node agent PKI operations

This runbook operates the recommended Smallstep `step-ca` provider for `vonk-forge`.
It is written for a small cluster, but contains no GPU node name, address, or count.
Certificates last exactly 24 hours. The offline root private key never enters the
NAS, Docker, Compose, a job payload, or Git.

The implementation and configuration were checked against `smallstep/certificates`
tag `v0.30.2`: `api/sign.go`, `api/revoke.go`, `api/crl.go`, and
`authority/provisioner/jwk.go`. The JWK provisioner consumes one-use JWT IDs,
validates the base `/1.0/sign` or `/1.0/revoke` audience with one minute of
leeway, and binds token subject/SANs to the signed CSR.

## Layout and preflight

Use two physically separate locations. `OFFLINE_PKI_DIR` is removable media on
an offline workstation. `PKI_SECRET_DIR` and `STEP_CA_DATA_DIR` are on the NAS.
Compose bind-backed secret uid/gid/mode behavior is not portable, so set and
verify host ownership explicitly. The Compose control-api runs as `10001:10001`
and the pinned step-ca image runs as `1000:1000` (`step`); do not apply a
blanket `root:root 0600` policy to files those services must read. Use the
consumer-specific ownership, mode, and ACL table in the authoritative
[NAS pull-only Compose deployment guide](../../deploy/compose/README.md),
including root-owned shared files with ACLs for `10001` and `1000`.

```sh
OFFLINE_PKI_DIR=/media/offline/vonk-forge-pki
PKI_SECRET_DIR=/srv/vonk-forge/secrets
STEP_CA_DATA_DIR=/srv/vonk-forge/step-ca
install -d -m 0700 "$OFFLINE_PKI_DIR" "$PKI_SECRET_DIR" "$STEP_CA_DATA_DIR"
umask 077
step version
docker version
```

Keep NTP healthy on the NAS and GPU nodes. Alert at 30 seconds of clock skew and
stop issuance before one minute; authorization tokens deliberately allow only
30 seconds and step-ca v0.30.2 allows at most one minute.

## Restricted LAN endpoint

Do not assume local DNS. Pick one NAS management address and use the same
management-LAN names on the NAS and on every GPU node by writing them to
`/etc/hosts`:

```text
<NAS_MANAGEMENT_IP> <ENROLLMENT_HOSTNAME> <CONTROLLER_HOSTNAME> <REGISTRY_HOSTNAME>
```

Caddy binds backend TLS only to `<NAS_MANAGEMENT_IP>:8443`. The NAS firewall
permits that port only from `<NODE_MANAGEMENT_CIDR>`, preferably narrowed to
the reserved GPU node leases. Enrollment exposes only `/agent/v1/enroll`; the
agent and registry names require the issued mTLS identity. Human control,
inference, Grafana, and Hermes routes are absent from this listener and remain
tailnet-only.

Install the Caddy backend trust anchor and stable DNS names during each manual
GPU node hardening/bootstrap. The installed agent initiates outbound long polling;
the manager does not scan the LAN. The certificate-bound `spk_` identity and a
fresh proxy-observed address within `VONK_MANAGEMENT_CIDRS` drive availability.
DHCP reservations improve operations but are not a correctness dependency.

## Create the offline root and online intermediate

Perform this block on the disconnected workstation. Store the root password in
a separate offline recovery medium. Generate an encrypted online intermediate
with path length zero and a one-year lifetime; rotate it before expiry.
Record the backup location in 1Password or the equivalent operator secret
inventory, but do not print the password values in shell transcripts or paste
them into issue trackers.

```sh
openssl rand -base64 32 > "$OFFLINE_PKI_DIR/root-password"
openssl rand -base64 32 > "$OFFLINE_PKI_DIR/intermediate-password"
step certificate create "Vonk Forge Offline Root" \
  "$OFFLINE_PKI_DIR/root_ca.crt" "$OFFLINE_PKI_DIR/root_ca.key" \
  --profile root-ca --kty OKP --curve Ed25519 --not-after 87600h \
  --password-file "$OFFLINE_PKI_DIR/root-password"
step certificate create "Vonk Forge Agent Intermediate" \
  "$OFFLINE_PKI_DIR/intermediate_ca.crt" "$OFFLINE_PKI_DIR/intermediate_ca_key" \
  --profile intermediate-ca --kty OKP --curve Ed25519 --not-after 8760h \
  --ca "$OFFLINE_PKI_DIR/root_ca.crt" --ca-key "$OFFLINE_PKI_DIR/root_ca.key" \
  --ca-password-file "$OFFLINE_PKI_DIR/root-password" \
  --password-file "$OFFLINE_PKI_DIR/intermediate-password"
chmod 600 "$OFFLINE_PKI_DIR/root_ca.key" "$OFFLINE_PKI_DIR/intermediate_ca_key" \
  "$OFFLINE_PKI_DIR/root-password" "$OFFLINE_PKI_DIR/intermediate-password"
chmod 644 "$OFFLINE_PKI_DIR/root_ca.crt" "$OFFLINE_PKI_DIR/intermediate_ca.crt"
step certificate inspect "$OFFLINE_PKI_DIR/intermediate_ca.crt" --short
```

Transfer only `root_ca.crt`, `intermediate_ca.crt`, the encrypted
`intermediate_ca_key`, and its password file to the NAS. Do not transfer the
offline root private key. The root certificate becomes both
`step-ca-root-certificate` and the Caddy `agent-client-ca` trust anchor.

## Create the narrow JWK provisioner

Generate a deployment-specific ES256 JWK pair on the NAS. The private JWK is
mounted only into control-api. step-ca receives only the public JWK in its
generated configuration; it receives no `encryptedKey` for this provisioner.

```sh
step crypto jwk create \
  "$PKI_SECRET_DIR/agent-ca-public.jwk" "$PKI_SECRET_DIR/agent-ca-credential" \
  --kty EC --crv P-256 --no-password --insecure
AGENT_CA_PROVISIONER_KID="$(step crypto jwk thumbprint < "$PKI_SECRET_DIR/agent-ca-public.jwk")"
jq --arg kid "$AGENT_CA_PROVISIONER_KID" '.kid=$kid | .alg="ES256" | .use="sig"' \
  "$PKI_SECRET_DIR/agent-ca-public.jwk" > "$PKI_SECRET_DIR/agent-ca-public.with-kid.jwk"
jq --arg kid "$AGENT_CA_PROVISIONER_KID" '.kid=$kid | .alg="ES256" | .use="sig"' \
  "$PKI_SECRET_DIR/agent-ca-credential" > "$PKI_SECRET_DIR/agent-ca-credential.with-kid"
mv "$PKI_SECRET_DIR/agent-ca-public.with-kid.jwk" "$PKI_SECRET_DIR/agent-ca-public.jwk"
mv "$PKI_SECRET_DIR/agent-ca-credential.with-kid" "$PKI_SECRET_DIR/agent-ca-credential"
jq --slurpfile key "$PKI_SECRET_DIR/agent-ca-public.jwk" \
  '.authority.provisioners[0].key=$key[0]' deploy/compose/step-ca/ca.json \
  > "$STEP_CA_DATA_DIR/ca.json"
chown 10001:10001 "$PKI_SECRET_DIR/agent-ca-credential"
chmod 0400 "$PKI_SECRET_DIR/agent-ca-credential"
chown 1000:1000 "$STEP_CA_DATA_DIR/ca.json"
chmod 0400 "$STEP_CA_DATA_DIR/ca.json"
test "$(jq -r '.authority.provisioners[0].key.kid' "$STEP_CA_DATA_DIR/ca.json")" = "$AGENT_CA_PROVISIONER_KID"
```

The tracked template fixes the JWK provisioner to 24 hours, disables direct CA
renewal and Smallstep extensions, and uses a client-auth-only template. Normal
renewal is a new `/1.0/sign` request: `vonk-forge` first authenticates the existing
mTLS identity, then submits the new node-signed CSR under fixed policy.
CRL generation is enabled with `generateOnRevoke`, a one-hour cache duration,
and a 30-minute renewal period. The control provider accepts only a correctly
signed CRL whose update window is current and bounded to that configured hour.

## Start and verify the production provider

Set `STEP_CA_CONFIG_FILE`, `AGENT_CA_PROVISIONER_KID`, and all file variables in
the root-owned site environment before selecting the generation. The installed
updater owns start/stop and Compose rendering. Verify the active immutable
generation through its fixed diagnostics:

```sh
sudo vonk-control-offline maintenance status
sudo vonk-control-offline maintenance step-ca-health
```

Only Caddy publishes a port. step-ca and control-api share the internal `ca`
network. The worker has `VONK_AGENT_RUNTIME=disabled` and loads no CA, proxy, or
agent credential. Inspect the rendered mounts and confirm no root private key.

## Revocation and uncertain remote results

Use the administrator API/CLI node-revoke operation. `vonk-forge` commits local
node retirement and certificate revocation first, so Caddy-forwarded identities
are rejected immediately. It then requests passive step-ca revocation, which
prevents provider renewal. Confirmed serials receive `ca_revoked_at`; retries
send only unconfirmed serials.

If the API reports `local revocation complete; remote CA revocation is
uncertain`, do not undo local state. Restore CA reachability and repeat the same
node-revoke command. Repetition is idempotent in effect. If step-ca accepted a
request but its response was lost, inspect the CA database/audit log for that
decimal serial; retain the local denial and record manual reconciliation.

An enrollment stuck in `issuing` is also deliberately never retried. Search the
step-ca audit trail by node subject and issuance time, revoke any possibly issued
serial, then clear/reject the enrollment only through an audited operator
procedure. Never automatically resubmit its authorization token.

```sh
sudo vonk-control-offline maintenance logs --service step-ca --since-minutes 30
sudo vonk-control-offline maintenance logs --service control-api --since-minutes 30
```

## Expiry and identity-loss recovery

An active node renews before expiry using its existing mTLS identity and a new
node-signed CSR. After expiry, private-key loss, disk replacement, or full GPU node
replacement, renewal is unavailable. Certificate loss is treated the same way.
An administrator must verify fresh hardware evidence and create a fresh
enrollment grant that is short-lived, explicit, and node-bound. The GPU node
generates a new key locally and goes through normal
approval. You must not copy another GPU node's certificate or private identity.

## Intermediate rotation with overlap

Create a new encrypted path-length-zero intermediate under the same offline
root. Stage its certificate/key/password, stop issuance briefly, update both
step-ca and control-api mounts atomically, and start them together. Caddy trusts
the offline root, so certificates from the old and new intermediates overlap for
the old leaf's remaining 24 hours. Verify new issuance, then retain the old
intermediate certificate for audit until every old leaf has expired. Never run
two active issuers with the same provisioner private credential.

Do not perform this transition with ad hoc Compose stop/start commands. Stage
the new material under the root-owned site boundary, record its SHA-256 values,
and execute the transition as a reviewed platform generation update. If the
candidate cannot prove CA health and new issuance, let the updater preserve or
restore the recorded predecessor; do not mutate the active generation in place.

For root rotation, distribute an overlap trust bundle containing old and new
root certificates to Caddy first, rotate intermediates and all leaves, wait at
least 24 hours, then remove the old root.

## Backup and restore consistency

Back up the generated public config, encrypted intermediate material and
password, provisioner private JWK, root certificate, step-ca database, and the
PostgreSQL control database. The offline root stays in its own offline backup.
To obtain a consistent CA snapshot, stop issuance/control-api, stop step-ca,
snapshot its data, and dump PostgreSQL before restarting. Encrypt the archive
with the operator backup system and test restoration on an isolated network.

Use the root-owned `HostBackupBoundary` described in
[Control-plane recovery](control-plane-recovery.md). It stops the fixed service
set, captures PostgreSQL and the configured CA state in one authenticated,
encrypted generation, and records the exact receipt. There is no supported
operator-supplied `pg_dump`, tar, decrypt, or Compose restore command.

Restore the step-ca data/config/secrets and PostgreSQL state only from that same
verified backup generation through `vonk-control-offline recover --apply`.
Afterward run `maintenance step-ca-health`, compare intermediate and
provisioner public-key fingerprints, and test one disposable enrollment before
restoring ingress.

## Built-in-to-step-ca migration

Built-in mode is an explicit bootstrap/development overlay, not a second active
issuer. Under the same offline root, prepare step-ca and its deployment-specific
provisioner, validate it on an isolated network, stop control-api, and replace
`compose.builtin-ca.yaml` with `compose.step-ca.yaml`. Existing leaves continue
through the root trust anchor; all new issuance uses Smallstep. Do not merge both
overlays—the settings guard rejects mixed provider material.

Publish and select a signed platform generation whose reviewed site selector is
`step-ca`; the updater validates the overlay, renders it from the immutable
generation, and performs the fixed service transition. There is no supported
in-place Compose overlay switch.
