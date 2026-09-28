# PostgreSQL backups and recovery

The PostgreSQL container writes a compressed SQL dump to `./backups/` beside
`docker-compose.yaml` after startup, then every 24 hours. Each dump is tested by
restoring it into a disposable PostgreSQL cluster before it is marked successful.
The same run archives the `step-ca-data` volume beside the dump with a matching
timestamp. The newest 7 PostgreSQL dumps and CA archives are retained. Set
`VONK_BACKUP_INTERVAL_SECONDS` and `VONK_BACKUP_KEEP` in `.env` to change these
positive values. Failures appear in the PostgreSQL logs and retry after five
minutes. A failed restore check does not advance the success marker.

Set `VONK_BACKUP_OFFHOST_PATH` to an already mounted, private host directory to
copy PostgreSQL dumps and paired CA archives off the NAS. The copy is pruned to
the same retention count; a failed off-host copy prevents the backup from being
marked successful. Keep the destination encrypted and access restricted. Dumps
contain all databases, roles and password hashes. Model and image caches are
excluded.
The NAS installer creates `backups/` before Docker starts with mode `0700` and
the invoking user's ownership. Completed dumps are returned to that owner with
mode `0600`; do not create the directory as root or replace it with a broader
shared-permission directory.

The step-ca volume is Badger v2, which does not support a live database backup.
The scheduled archive includes its files but copying a live Badger directory is
not an application-consistent snapshot. PostgreSQL's `pg_dumpall` is consistent
within each database but is not an atomic snapshot across databases. For a
coordinated recovery point, stop issuance and `control-api`, stop `step-ca`, take
the volume snapshot and PostgreSQL dump, then restart services. Routine archives
do not replace this coordinated maintenance procedure before restoring CA data.

## Backup scope

PostgreSQL restores control intent, identities, permissions, and references.
It does not restore model/image bytes. The planned
[managed-storage ownership boundary](architecture-overview.md#state-ownership)
also keeps artifact manifests and local checkpoints outside database dumps.
Optional artifact backups must preserve those records together with their data
through a consistent snapshot or quiesced writers. A surviving artifact cannot
recreate a lost grant, profile, or approval. See
[artifact recovery](architecture-overview.md#artifact-recovery).

## Replacing a container

A normal Compose redeployment reuses `postgres-data`; no restore is needed.
Do not remove that volume. Container replacement is different from data loss.

## Restoring after data loss

Restore configuration and secrets first. Keep the original data volume untouched
until recovery is verified. On the NAS, from the project directory, select a
completed dump and create a separate recovery volume. Use the same PostgreSQL
major version as the backup (currently 18). These commands require Docker access.

```sh
docker compose stop
docker volume create vonk-forge-postgres-recovered
docker run -d --name vonk-postgres-restore \
  -e POSTGRES_USER=vonk_restore_admin \
  -e POSTGRES_PASSWORD_FILE=/run/secrets/postgres-password \
  -v "$PWD/secrets/postgres-password:/run/secrets/postgres-password:ro" \
  --network none \
  -v vonk-forge-postgres-recovered:/var/lib/postgresql postgres:18
# Wait until this reports accepting connections:
docker exec vonk-postgres-restore pg_isready -U vonk_restore_admin -d postgres
# Substitute your actual backup filename. Check gzip before piping it.
gzip -t backups/postgres-TIMESTAMP.sql.gz
gzip -dc backups/postgres-TIMESTAMP.sql.gz | \
  docker exec -i vonk-postgres-restore \
  psql -X -v ON_ERROR_STOP=1 -U vonk_restore_admin -d postgres
```

Proceed only if restore exits successfully. The temporary bootstrap role avoids
colliding with the backed-up `control` role. No ports are exposed and networking
is disabled during recovery. Disable login for the bootstrap role after restoring (it owns system objects):

```sh
docker exec vonk-postgres-restore psql -U control -d postgres \
  -v ON_ERROR_STOP=1 -c 'DROP DATABASE vonk_restore_admin;' \
  -c 'ALTER ROLE vonk_restore_admin NOLOGIN;'
docker stop vonk-postgres-restore
docker rm vonk-postgres-restore
```

Create `compose.recovery.yaml` beside the main Compose file:

```yaml
volumes:
  postgres-data:
    external: true
    name: vonk-forge-postgres-recovered
```

Start with both files and retain this override for subsequent redeployments:

```sh
docker compose -f docker-compose.yaml -f compose.recovery.yaml up -d
```

Restore the matching `step-ca-postgres-TIMESTAMP.tar.gz` archive to the
`step-ca-data` volume only while `step-ca` is stopped. Verify CA health and key
fingerprints as described in the [agent PKI runbook](runbooks/agent-pki.md).
The online CA and PostgreSQL archives are not a transactional cross-service
snapshot; use the coordinated maintenance procedure for a consistent recovery
point. Keep generated secrets and the offline root in their separate protected
backups. Then check PostgreSQL health, Controller login, Fleet and Library
records before resuming workloads.

## Verifying changes

The running backup loop performs an isolated restore verification after each
scheduled dump. The API exports `vonk_control_backup_successful` (including
zero before the first success), `vonk_control_backup_age_seconds`, and
`vonk_control_backup_restore_verification_age_seconds`. Alerts cover a missing
first backup, a stale backup, and a restore verification older than 48 hours.

Run the disposable end-to-end container test against OrbStack or another test
Docker engine (it creates and removes only uniquely named disposable containers):

```sh
VONK_RUN_BACKUP_CONTAINER_TEST=1 uv run --project control --frozen \
  pytest -q deploy/compose/tests/test_postgres_backup_container.py
```
