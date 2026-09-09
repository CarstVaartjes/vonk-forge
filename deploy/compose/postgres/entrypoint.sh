#!/bin/sh
set -eu

source=${LITELLM_DATABASE_PASSWORD_FILE:-/run/secrets/litellm-database-password}
runtime_directory=/run/vonk-postgres-secrets
target=$runtime_directory/litellm-database-password

# Standalone Compose implements secrets as read-only bind mounts. Their host
# UID is not portable to the image's postgres UID, so stage the one secret
# needed by initdb before the official entrypoint drops privileges.
install -d -m 0700 -o postgres -g postgres "$runtime_directory"
install -m 0400 -o postgres -g postgres "$source" "$target"
export LITELLM_DATABASE_PASSWORD_FILE=$target

sentinel=${PGDATA:-/var/lib/postgresql/data}/.vonk-database-initialized
if [ -f "${PGDATA:-/var/lib/postgresql/data}/PG_VERSION" ]; then
  existing_cluster=1
else
  existing_cluster=0
  export VONK_POSTGRES_INIT_SENTINEL=$sentinel
fi

/usr/local/bin/docker-entrypoint.sh "$@" &
postgres_pid=$!

forward_signal() {
  if [ -n "${backup_pid:-}" ]; then kill -TERM "$backup_pid" 2>/dev/null || true; fi
  kill -TERM "$postgres_pid" 2>/dev/null || true
}
trap forward_signal TERM INT

ready=0
attempt=0
while [ "$attempt" -lt 240 ]; do
  if ! kill -0 "$postgres_pid" 2>/dev/null; then
    wait "$postgres_pid"
    exit $?
  fi
  if [ "$existing_cluster" -eq 1 ] || [ -f "$sentinel" ]; then
    if pg_isready --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" >/dev/null 2>&1; then
      ready=1
      break
    fi
  fi
  attempt=$((attempt + 1))
  sleep 1
done

if [ "$ready" -ne 1 ]; then
  printf '%s\n' 'PostgreSQL did not become ready for database reconciliation' >&2
  kill -TERM "$postgres_pid" 2>/dev/null || true
  wait "$postgres_pid" || true
  exit 1
fi

if ! /docker-entrypoint-initdb.d/10-vonk-forge-databases.sh; then
  printf '%s\n' 'PostgreSQL database reconciliation failed' >&2
  kill -TERM "$postgres_pid" 2>/dev/null || true
  wait "$postgres_pid" || true
  exit 1
fi

# Dump all databases and roles, including the configured Controller database and
# LiteLLM. Publish only complete dumps; failed attempts never prune good backups.
backup_loop() (
  umask 077
  interval=${VONK_BACKUP_INTERVAL_SECONDS:-86400}
  keep=${VONK_BACKUP_KEEP:-14}
  case "$interval:$keep" in *[!0-9:]*|:*|*:) echo "Invalid backup settings" >&2; exit 1;; esac
  if [ "$interval" -eq 0 ] || [ "$keep" -eq 0 ]; then
    echo "Backup interval and retention must be positive" >&2; exit 1
  fi
  mkdir -p /backups
  temporary=/backups/.postgres-backup.tmp
  trap 'rm -f "$temporary" "$temporary.gz"; exit 0' TERM INT
  while :; do
    if gosu postgres pg_dumpall --username "$POSTGRES_USER" --database postgres > "$temporary" &&
       gzip -c "$temporary" > "$temporary.gz" && gzip -t "$temporary.gz"; then
      destination=/backups/postgres-$(date -u +%Y%m%dT%H%M%SZ).sql.gz
      mv "$temporary.gz" "$destination"
      rm -f "$temporary"
      echo "PostgreSQL backup completed: $destination"
      # Filenames are generated above and contain neither spaces nor newlines.
      count=0
      for backup in $(ls -1 /backups/postgres-*.sql.gz | sort -r); do
        count=$((count + 1))
        if [ "$count" -gt "$keep" ]; then rm -f "$backup"; fi
      done
      sleep "$interval" & wait $!
    else
      echo "PostgreSQL backup failed; retrying in five minutes" >&2
      rm -f "$temporary" "$temporary.gz"
      sleep 300 & wait $!
    fi
  done
)
backup_loop &
backup_pid=$!
wait "$postgres_pid"
