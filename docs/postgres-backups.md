# PostgreSQL backups and recovery

The PostgreSQL container writes a compressed SQL dump to `./backups/` beside
`docker-compose.yaml` after startup, then every 24 hours. It retains the newest
14 successful dumps. Set `VONK_BACKUP_INTERVAL_SECONDS` and `VONK_BACKUP_KEEP` in
`.env` to change these positive values. Failures appear in the PostgreSQL logs
and retry after five minutes. An incomplete dump never replaces a completed
backup. Dumps contain all databases, roles and password hashes: keep the folder
private and include it in your NAS backup alongside `.env`, `secrets/`, and the
Compose file. Copy backups off the NAS too. Model and image caches are excluded.

A dump is consistent within each database; it is not an atomic snapshot across
all databases. For a coordinated maintenance snapshot, stop application writers
before restarting PostgreSQL to trigger its startup backup.

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

Check PostgreSQL health, Controller login, Fleet and Library records before
resuming workloads. Database dumps do not restore certificate-authority volumes
or other service state; retain those separately for complete system recovery.

## Verifying changes

Run the real isolated container test against OrbStack or another test Docker
engine (it creates and removes only uniquely named disposable containers):

```sh
VONK_RUN_BACKUP_CONTAINER_TEST=1 uv run --project control --with-editable . \
  pytest -q deploy/compose/tests/test_postgres_backup_container.py
```
