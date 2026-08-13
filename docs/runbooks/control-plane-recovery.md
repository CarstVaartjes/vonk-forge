# Back up and recover the control plane

Control-host backup and recovery are fixed operations of the root-owned host
updater. Production does not execute a repository backup script, accept an
encryption command, or run Compose from a checkout. `HostBackupBoundary` uses
fixed Docker, PostgreSQL, archive, and `/usr/bin/age` argument vectors with
bounded output, deadlines, disk reservation, and pre-opened file descriptors.

## Prepare recovery before an update

Keep these paths root-owned and outside every container-writable tree:

- `/srv/vonk-forge/control-host`, mode `0700`, for generations, operations,
  encrypted backups, and the single host-operation lock;
- `/srv/vonk-forge/control-identity`, mode `0755`, for bounded read-only active
  and candidate projections;
- `/srv/vonk-forge/site`, mode `0700`, for the allowlisted site configuration;
- the root-owned age recipients file named by
  `VONK_BACKUP_RECIPIENTS_FILE`; and
- the root-owned age identity file named by `VONK_BACKUP_IDENTITY_FILE`.

The age recipients and identity files must be regular, non-linked files with
the exact private modes enforced by the updater. Store the recovery identity
offline as well as on the host. Never place the identity, a TUF private key, or
a registry credential in the deployment bundle, admin repository, online API,
worker, or a GPU node.

Retain enough free space for the current and candidate generations, one new
encrypted backup, and transient OCI acquisition. Before every maintenance
window, confirm the exact predecessor platform target remains authorized by
current TUF metadata and that its content-addressed OCI objects remain
available.

## Development browser-access recovery

Development's mutable-image Pull then Redeploy path is deliberately separate
from the production recovery boundary below. Production remains digest-pinned
and host-updater mediated; never use development aliases or a NAS project UI to
select a production generation.
Use the following independent responses for OAuth compromise, Tailscale state
loss, administrator password loss, and break-glass loopback.

- **OAuth compromise:** revoke the client under Tailscale **Trust
  credentials**, revoke the affected gateway node and Service approval, create
  one replacement client with only `auth_keys` write and
  `tag:vonk-gateway`, then follow the authoritative
  [browser-access recovery instructions](development-nas-installation.md#rotation-and-recovery).
  Do not rotate an application credential unless that authority was also
  exposed.
- **Tailscale state loss:** preserve or restore the development
  `dev-tailscale-state` volume when possible. Otherwise allow the scoped OAuth
  client to create one replacement tagged gateway, verify only that gateway
  advertises `svc:vonk-forge`, revoke any orphan, and confirm the HTTPS-only
  Serve map. PostgreSQL and application sessions are separate authorities.
- **Administrator password loss:** recover the complete encrypted local secret
  generation, use its authorized password-rotation operation, update the
  **Vonk Forge NAS Development Administrator** 1Password item, republish, and
  redeploy with every named volume preserved. Rotation revokes existing
  browser sessions; there is no unauthenticated reset route or default
  password.
- **Break-glass loopback:** if private browser ingress is unavailable, use the
  bounded loopback-forwarding procedure only for diagnosis or recovery. Do not
  publish a LAN listener or enable Tailscale Funnel, and remove the temporary
  forwarding session when recovery is complete.

## Automatic upgrade backup

Every applied platform upgrade creates its backup after exact revalidation and
generation staging but before migration or selection. The fixed boundary:

1. obtains a PostgreSQL custom-format dump from the selected generation;
2. collects only the allowlisted site files, selected generation receipt, and
   verified release assets needed for compensation;
3. builds the canonical checksum-bound archive;
4. encrypts it with the root-owned age recipients file; and
5. fsyncs a new owner-only artifact and exact backup receipt under
   `/srv/vonk-forge/control-host/backups`.

The receipt binds the operation, selected generation, generation receipt,
encrypted byte count and SHA-256, archive-manifest SHA-256, and recipients
SHA-256. The same receipt is recorded in the operation's hash-chained journal.
Copy the encrypted artifact, its exact backup receipt, generation receipt, and
terminal journal evidence to authenticated encrypted off-host storage. Also
protect Tailscale state, scoped OAuth credentials, step-ca online state, and
the Hermes API key according to their own runbooks.

## Resume an interrupted operation

An unfinished operation blocks a new upgrade or rollback. Do not retry the
original command, edit journal files, manually start containers, or move the
active pointer. Recovery takes the same host-operation lock, validates the
contiguous hash-chained journal, probes each recorded effect, and only adopts
or repeats an exact idempotent action:

```bash
sudo vonk-control-offline --state-path /srv/vonk-forge/control-host \
  recover --apply
```

Recovery may finish the candidate or compensate to the exact predecessor. If
compensation needs data restoration, the updater reopens the journal's exact
backup receipt, verifies the encrypted file by descriptor, decrypts and parses
it through the trusted boundary, restores the exact database/site state, and
writes an immutable restore receipt. There is no command-line path for choosing
an arbitrary backup.

If the database reports neither the recorded predecessor revision nor the
recorded target revision, recovery fails closed. Preserve the complete
operation directory and contact a maintainer; do not force a migration or
delete the pending operation.

## Operator-requested rollback

Plan the exact retained generation first, then apply the same generation ID:

```bash
sudo vonk-control-offline --state-path /srv/vonk-forge/control-host \
  rollback --generation REPLACE_GENERATION_ID
sudo vonk-control-offline --state-path /srv/vonk-forge/control-host \
  rollback --generation REPLACE_GENERATION_ID --apply
```

Rollback refreshes current TUF metadata and verifies the active release's exact
predecessor target, manifest, bundle, generation receipt, site state, database
revision, and running identities. A stable-channel update to N+1 does not
change an N-to-N-1 rollback. Removing N-1 from current targets is explicit
rollback revocation and makes the command fail closed.

## Host-loss recovery and evidence

Do not improvise a full-host restore with ad-hoc `pg_restore` or Compose
commands. Rebuild the Docker-capable host, reinstall the same trusted bootstrap
updater, restore the root-owned trust/site/identity inputs from authenticated
off-host storage, and place the encrypted generation backup and exact receipt
at their recorded paths. Use the release-specific, reviewed disaster-recovery
procedure to reconstruct the journaled compensation operation; the normal CLI
intentionally cannot accept an arbitrary backup pathname.

Test recovery on a disposable Docker-capable Linux host before enabling release
publication and after any updater ABI change. Acceptance must prove timeout and
output bounds, exact backup verification, restoration through the fixed
boundary, recovery after every journal phase, generation-bound API/worker
readiness, and failure on a modified archive, receipt, generation, or database
revision. Retain only sanitized evidence: never include secret contents,
environment values, age identity material, tokens, or private TUF keys.

After recovery or rollback, verify database and CA health, selected generation
identity, worker heartbeat, the exact Tailscale Services, Hermes API-key
continuity, session visibility, workspace contents, and absence of cloud-model
configuration. If Tailscale state is unavailable, use the scoped OAuth
re-enrollment path and revoke the old node. If ingress authority is unavailable,
leave ingress closed.
