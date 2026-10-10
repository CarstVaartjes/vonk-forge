# PostgreSQL backups and recovery

The PostgreSQL container writes a compressed SQL dump to `./backups/` beside
`docker-compose.yaml` after startup, then every 24 hours. Each dump is tested by
restoring it into a disposable PostgreSQL cluster before it is marked successful.
CA issuance and revocation journals are included in the PostgreSQL dump.
Failures appear in the PostgreSQL logs and retry after five minutes; a backup
problem never stops PostgreSQL itself. A failed restore check does not advance the success marker.

Set `VONK_BACKUP_OFFHOST_PATH` to an already mounted, private host directory to
copy PostgreSQL dumps off the NAS. The copy is pruned to
the same retention count; a failed off-host copy prevents the backup from being
marked successful. Keep the destination encrypted and access restricted. Dumps
contain all databases, roles and password hashes. Model and image caches are
excluded.
The NAS installer creates `backups/` before Docker starts with mode `0700` and
the invoking user's ownership. Completed dumps are returned to that owner with
mode `0600`; do not create the directory as root or replace it with a broader
shared-permission directory.

The Controller CA journals share PostgreSQL. Back up the installed authority
secrets separately and preserve the same intermediate when restoring.

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

Keep generated authority secrets and the offline root in protected backups.
Restore the matching secrets and PostgreSQL state, then verify Controller
health, login, Fleet, Library and enrollment before resuming workloads.
